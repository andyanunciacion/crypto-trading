"""The backtest report: every metric against numbers worked out by hand."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from engine.backtest.models import BacktestConfig, BacktestResult, EquityPoint, Fill, Trade
from engine.backtest.report import describe, drawdowns, equity_csv, report, sharpe, trades
from engine.backtest.simulator import run_backtest
from engine.exchange.models import Candle, MarketRules, OrderSide, Timeframe
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
D = Decimal

RULES = MarketRules(
    symbol="BTC/USDT",
    amount_step=D("0.0001"),
    price_step=D("0.01"),
    min_amount=D("0.0001"),
    min_cost=D("1"),
)


def at(hour: int) -> datetime:
    return T0 + hour * HOUR


def config(**overrides: Any) -> BacktestConfig:
    values: dict[str, Any] = {
        "strategy": "script",
        "strategy_version": 1,
        "params": {},
        "exchange": "mexc",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "start": at(0),
        "end": at(6),
        "initial_cash": D(1000),
        "fee_rate": D("0.001"),
        "slippage_rate": D(0),
        "market_rules": RULES,
    }
    return BacktestConfig(**(values | overrides))


def fill(hour: int, side: OrderSide, price: str, amount: str, fee: str, cash_after: str) -> Fill:
    return Fill(
        ts=at(hour),
        side=side,
        price=D(price),
        amount=D(amount),
        fee=D(fee),
        cash_after=D(cash_after),
    )


def curve(*equities: str) -> list[EquityPoint]:
    return [EquityPoint(ts=at(h), equity=D(e)) for h, e in enumerate(equities)]


def result(
    fills: list[Fill], equities: list[EquityPoint], final_amount: str = "0"
) -> BacktestResult:
    return BacktestResult(
        signals=[],
        fills=fills,
        skipped=[],
        equity_curve=equities,
        final_cash=fills[-1].cash_after if fills else D(1000),
        final_amount=D(final_amount),
        final_equity=equities[-1].equity,
        total_fees=sum((f.fee for f in fills), D(0)),
    )


# Two round trips from 1000 USDT, fee 0.1% of each fill's value:
#   buy  9 @ 100 on candle 1: cost 900, fee 0.90, cash 1000 - 900.90 = 99.10
#   sell 9 @ 120 on candle 2: proceeds 1080, fee 1.08, cash 99.10 + 1078.92 = 1178.02
#     -> pnl 1078.92 - 900.90 = +178.02, on 900.90 put in
#   buy 10 @ 110 on candle 3: cost 1100, fee 1.10, cash 1178.02 - 1101.10 = 76.92
#   sell 10 @ 100 on candle 4: proceeds 1000, fee 1.00, cash 76.92 + 999 = 1075.92
#     -> pnl 999 - 1101.10 = -102.10, on 1101.10 put in
WIN = [fill(1, "buy", "100", "9", "0.9", "99.1"), fill(2, "sell", "120", "9", "1.08", "1178.02")]
LOSS = [fill(3, "buy", "110", "10", "1.1", "76.92"), fill(4, "sell", "100", "10", "1", "1075.92")]
# Equity at each close: candle 1 holds 9 coins closing at 110 (99.10 + 990), candle 3
# holds 10 closing at 105 (76.92 + 1050).
EQUITY = curve("1000", "1089.1", "1178.02", "1126.92", "1075.92", "1075.92")
TWO_TRADES = result(WIN + LOSS, EQUITY)


# --- Trades -----------------------------------------------------------------------------


def test_trades_pair_each_buy_with_the_next_sell_net_of_both_fees() -> None:
    assert trades(WIN + LOSS) == [
        Trade(
            entry_ts=at(1),
            exit_ts=at(2),
            amount=D(9),
            entry_price=D(100),
            exit_price=D(120),
            fees=D("1.98"),
            pnl=D("178.02"),
            return_=D("178.02") / D("900.9"),
        ),
        Trade(
            entry_ts=at(3),
            exit_ts=at(4),
            amount=D(10),
            entry_price=D(110),
            exit_price=D(100),
            fees=D("2.1"),
            pnl=D("-102.1"),
            return_=D("-102.1") / D("1101.1"),
        ),
    ]


def test_an_open_buy_is_not_a_trade_yet() -> None:
    assert len(trades([*WIN, LOSS[0]])) == 1


def test_fills_out_of_order_are_an_error() -> None:
    with pytest.raises(ValueError, match="sell"):
        trades([WIN[1]])
    with pytest.raises(ValueError, match="buy"):
        trades([WIN[0], LOSS[0]])


# --- Drawdown ---------------------------------------------------------------------------


def test_drawdown_is_the_fall_from_the_running_peak() -> None:
    # Peak 1178.02 on candle 2; 1126.92 is 51.10 below it, 1075.92 is 102.10 below.
    assert drawdowns(D(1000), EQUITY) == [
        D(0),
        D(0),
        D(0),
        D("51.1") / D("1178.02"),
        D("102.1") / D("1178.02"),
        D("102.1") / D("1178.02"),
    ]


def test_drawdown_counts_from_the_starting_cash() -> None:
    # Losing from the first candle is a drawdown too: 900 is 10% below the 1000 we began with.
    assert drawdowns(D(1000), curve("900", "950")) == [D("0.1"), D("0.05")]


# --- Sharpe -----------------------------------------------------------------------------


def test_sharpe_matches_a_hand_calculation() -> None:
    # From 1000: 1100, 990, 1089, so per-candle returns +10%, -10%, +10%.
    #   mean = 0.1 / 3 = 1/30
    #   sample variance = ((2/30)^2 + (4/30)^2 + (2/30)^2) / (3 - 1) = 1/75, std = 1/(5 sqrt 3)
    #   mean / std = 5 sqrt 3 / 30 = sqrt 3 / 6
    #   annualized over 8760 hourly candles a year: sqrt 3 / 6 x sqrt 8760 = sqrt 26280 / 6
    value = sharpe(D(1000), curve("1100", "990", "1089"), "1h")

    assert value is not None
    assert round(value, 10) == round(D(26280).sqrt() / 6, 10)  # 27.0185...


@pytest.mark.parametrize(
    ("timeframe", "factor"), [("4h", D(2)), ("1d", D(24).sqrt()), ("15m", D("0.5"))]
)
def test_sharpe_annualizes_by_the_square_root_of_candles_per_year(
    timeframe: Timeframe, factor: Decimal
) -> None:
    # The same per-candle returns are worth more Sharpe the more candles a year holds:
    # 4x as many hourly as 4h candles, so 2x the Sharpe.
    equities = curve("1100", "990", "1089")
    hourly, other = sharpe(D(1000), equities, "1h"), sharpe(D(1000), equities, timeframe)

    assert hourly is not None
    assert other is not None
    assert round(hourly, 10) == round(other * factor, 10)


def test_sharpe_needs_two_returns_and_some_variation() -> None:
    assert sharpe(D(1000), curve("1100"), "1h") is None
    assert sharpe(D(1000), curve("1000", "1000", "1000"), "1h") is None


# --- The whole report -------------------------------------------------------------------


def test_report_matches_a_hand_calculation() -> None:
    r = report(config(), TWO_TRADES)

    assert r.total_return == D("0.07592")  # 1075.92 / 1000 - 1
    assert r.max_drawdown == D("102.1") / D("1178.02")  # 8.67%
    assert r.sharpe == sharpe(D(1000), EQUITY, "1h")
    assert r.trades == trades(WIN + LOSS)
    assert r.trade_count == 2
    assert r.win_rate == D("0.5")
    assert r.profit_factor == D("178.02") / D("102.1")  # 1.74: 1.74 won per 1 lost
    assert r.open_trade is False
    assert r.final_equity == D("1075.92")
    assert r.total_fees == D("4.08")
    # Flat at the end, so the trades account for every cent of the change.
    assert sum(t.pnl for t in r.trades) == r.final_equity - D(1000)


def test_no_trades_leave_win_rate_and_profit_factor_empty() -> None:
    r = report(config(), result([], curve("1000", "1000")))

    assert r.trade_count == 0
    assert r.win_rate is None
    assert r.profit_factor is None
    assert r.total_return == 0
    assert r.max_drawdown == 0


def test_without_a_losing_trade_profit_factor_is_empty() -> None:
    r = report(config(), result(WIN, curve("1000", "1089.1", "1178.02")))

    assert r.win_rate == 1
    assert r.profit_factor is None  # nothing to divide by: infinite


def test_a_break_even_trade_is_not_a_win() -> None:
    even = [
        fill(1, "buy", "100", "9", "0", "100"),
        fill(2, "sell", "100", "9", "0", "1000"),
    ]
    r = report(config(fee_rate=D(0)), result(even, curve("1000", "1000", "1000")))

    assert r.win_rate == 0
    assert r.profit_factor is None


def test_an_open_position_counts_in_the_return_but_not_the_trades() -> None:
    # The second buy is still held at the end: 10 coins closing at 105.
    equities = curve("1000", "1089.1", "1178.02", "1126.92")
    r = report(config(), result([*WIN, LOSS[0]], equities, final_amount="10"))

    assert r.open_trade is True
    assert r.trade_count == 1
    assert r.win_rate == 1
    assert r.total_return == D("0.12692")


def test_report_agrees_with_a_real_run() -> None:
    # The EMA step series from test_backtest_simulator: one buy, one sell, flat at the end.
    strategy = EmaCrossover(EmaCrossoverParams(fast=3, slow=5))
    warmup = strategy.warmup_candles
    up, down = warmup + 5, warmup + 5 + 15
    closes = ["100"] * up + ["110"] * (down - up) + ["90"] * 15
    candles = [
        Candle(ts=at(h), open=D(c), high=D(c), low=D(c), close=D(c), volume=D(1))
        for h, c in enumerate(closes)
    ]
    cfg = config(
        strategy=strategy.name,
        strategy_version=strategy.version,
        params=strategy.params.model_dump(mode="json"),
        start=at(warmup),
        end=at(len(closes)),
        slippage_rate=D("0.0005"),
    )
    run = run_backtest(strategy, candles, cfg)

    r = report(cfg, run)

    assert r.trade_count == 1
    assert (r.trades[0].entry_ts, r.trades[0].exit_ts) == (at(up + 1), at(down + 1))
    assert r.trades[0].pnl == run.final_equity - cfg.initial_cash  # bought 110, sold 90
    assert r.trades[0].pnl < 0
    assert r.win_rate == 0


# --- Output -----------------------------------------------------------------------------


def test_equity_csv_has_a_row_per_candle() -> None:
    assert equity_csv(D(1000), curve("900", "1100.50", "990.45")) == (
        "ts,equity,drawdown\n"
        "2026-01-01T00:00:00+00:00,900,0.100000\n"
        "2026-01-01T01:00:00+00:00,1100.50,0.000000\n"
        "2026-01-01T02:00:00+00:00,990.45,0.100000\n"
    )


def test_describe_shows_the_metrics() -> None:
    text = describe(7, config(), TWO_TRADES)

    assert "run 7: script v1" in text
    assert "1000 USDT -> 1075.92 USDT (+7.59%), fees 4.08 USDT" in text
    assert "max drawdown 8.67%" in text
    assert "2 trades, 50.0% won, profit factor 1.74" in text
    assert "still holding" not in text


@pytest.mark.parametrize(
    ("fills", "equities", "expected"),
    [
        ([], curve("1000"), "no closed trades"),
        ([], curve("1000"), "Sharpe n/a"),
        (WIN, curve("1000", "1089.1", "1178.02"), "profit factor n/a (no losing trades)"),
    ],
    ids=["no-trades", "no-sharpe", "no-losses"],
)
def test_describe_explains_missing_metrics(
    fills: list[Fill], equities: list[EquityPoint], expected: str
) -> None:
    assert expected in describe(7, config(), result(fills, equities))
