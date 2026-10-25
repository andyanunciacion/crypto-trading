"""The backtest simulator: fills, fees, slippage and minimums, against hand-worked numbers."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from engine.backtest.models import (
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    Fill,
    default_slippage,
)
from engine.backtest.simulator import (
    BacktestError,
    fee,
    fill_price,
    run_backtest,
    warmup_needed,
)
from engine.exchange.models import Candle, MarketRules
from engine.risk.sizing import AtrRisk, FixedAmount, PercentOfEquity
from engine.risk.stops import AtrMultiple, Percent, Stops
from engine.strategies.base import Position, Signal, Strategy, StrategyParams
from engine.strategies.bollinger_breakout import BollingerBreakout, BollingerBreakoutParams
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from engine.strategies.rsi_mean_reversion import RsiMeanReversion, RsiMeanReversionParams

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


def candle(hour: int, open_: str, close: str | None = None) -> Candle:
    o, c = D(open_), D(close or open_)
    return Candle(ts=at(hour), open=o, high=max(o, c), low=min(o, c), close=c, volume=D(1))


def flat(hours: range, price: str = "100") -> list[Candle]:
    return [candle(h, price) for h in hours]


class ScriptParams(StrategyParams):
    buys: tuple[int, ...] = ()  # hours (from T0) of the candles to signal BUY on
    sells: tuple[int, ...] = ()
    size_hint: Decimal | None = None
    warmup: int = 1


class Script(Strategy[ScriptParams]):
    """Signals on given candles whatever the position, so the simulator's own checks show."""

    name = "script"
    version = 1
    params_model = ScriptParams

    @property
    def warmup_candles(self) -> int:
        return self.params.warmup

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        ts = window[-1].ts
        hour = (ts - T0) // HOUR
        if hour in self.params.buys:
            return Signal(action="buy", ts=ts, reason="scripted", size_hint=self.params.size_hint)
        if hour in self.params.sells:
            return Signal(action="sell", ts=ts, reason="scripted")
        return Signal(action="hold", ts=ts, reason="scripted")


def script(**params: Any) -> Script:
    return Script(ScriptParams(**params))


def config[P: StrategyParams](
    strategy: Strategy[P], start: int, end: int, **overrides: Any
) -> BacktestConfig:
    """Trade from candle `start` up to, not including, candle `end` (hours from T0)."""
    values: dict[str, Any] = {
        "strategy": strategy.name,
        "strategy_version": strategy.version,
        "params": strategy.params.model_dump(mode="json"),
        "exchange": "mexc",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "start": at(start),
        "end": at(end),
        "initial_cash": D(1000),
        "fee_rate": D(0),
        "slippage_rate": D(0),
        "market_rules": RULES,
    }
    return BacktestConfig(**(values | overrides))


# --- The building blocks ----------------------------------------------------------------


def test_slippage_works_against_both_sides() -> None:
    assert fill_price("buy", D(100), D("0.01")) == D(101)
    assert fill_price("sell", D(100), D("0.01")) == D(99)
    assert fill_price("buy", D(100), D(0)) == D(100)


def test_fee_is_a_rate_on_the_notional() -> None:
    assert fee(D(100), D("9.99"), D("0.001")) == D("0.999")


def test_small_caps_get_more_slippage_by_default() -> None:
    assert default_slippage("BTC/USDT") == D("0.0005")
    assert default_slippage("ETH/USDT") == D("0.0005")
    assert default_slippage("PEPE/USDT") == D("0.003")


# --- Whole runs -------------------------------------------------------------------------


def test_fees_match_a_hand_calculation() -> None:
    # Candle 0 is warm-up. BUY on candle 1 fills at candle 2's open (100); SELL on
    # candle 2 fills at candle 3's open (110). Fee 0.1%, no slippage:
    #   buy:  amount = 1000 / (100 x 1.001) = 9.99000999 -> 9.9900 (step 0.0001)
    #         cost 999.00, fee 0.999, cash 1000 - 999 - 0.999 = 0.001
    #   sell: proceeds 9.99 x 110 = 1098.90, fee 1.0989, cash 0.001 + 1098.90 - 1.0989
    #         = 1097.8021; fees in total 0.999 + 1.0989 = 2.0979
    candles = [candle(0, "100"), candle(1, "100"), candle(2, "100", "105"), candle(3, "110")]
    strategy = script(buys=(1,), sells=(2,))

    result = run_backtest(strategy, candles, config(strategy, 1, 4, fee_rate=D("0.001")))

    assert result.fills == [
        Fill(
            ts=at(2),
            side="buy",
            price=D(100),
            amount=D("9.99"),
            fee=D("0.999"),
            cash_after=D("0.001"),
        ),
        Fill(
            ts=at(3),
            side="sell",
            price=D(110),
            amount=D("9.99"),
            fee=D("1.0989"),
            cash_after=D("1097.8021"),
        ),
    ]
    assert result.total_fees == D("2.0979")
    assert result.final_cash == D("1097.8021")
    assert result.final_amount == 0
    assert result.final_equity == D("1097.8021")
    assert result.skipped == []
    # One point per candle in the range, at its close: candle 2 holds 9.99 at 105.
    assert result.equity_curve == [
        EquityPoint(ts=at(1), equity=D(1000)),
        EquityPoint(ts=at(2), equity=D("0.001") + D("9.99") * 105),  # 1048.951
        EquityPoint(ts=at(3), equity=D("1097.8021")),
    ]


def test_fills_at_the_next_open_not_the_signal_close() -> None:
    # The signal candle closes at 100 but the next one opens at 102 (a gap up overnight,
    # say). The strategy couldn't have bought at 100: that price was gone by the time
    # the candle closed and the signal existed.
    candles = [candle(0, "100"), candle(1, "100"), candle(2, "102", "103"), candle(3, "103")]
    strategy = script(buys=(1,))

    result = run_backtest(strategy, candles, config(strategy, 1, 4))

    assert [(f.ts, f.price) for f in result.fills] == [(at(2), D(102))]


def test_slippage_is_applied_to_each_fill() -> None:
    candles = [candle(0, "100"), candle(1, "100"), candle(2, "100"), candle(3, "110")]
    strategy = script(buys=(1,), sells=(2,))

    result = run_backtest(strategy, candles, config(strategy, 1, 4, slippage_rate=D("0.01")))

    assert [f.price for f in result.fills] == [D(101), D("108.9")]


def test_size_hint_scales_the_buy() -> None:
    candles = flat(range(4))
    strategy = script(buys=(1,), size_hint=D("0.5"))

    result = run_backtest(strategy, candles, config(strategy, 1, 4))

    assert result.fills[0].amount == D(5)
    assert result.final_cash == D(500)


def test_a_signal_on_the_warmup_candle_trades_at_the_start() -> None:
    candles = flat(range(3))
    strategy = script(buys=(0,))

    result = run_backtest(strategy, candles, config(strategy, 1, 3))

    assert [f.ts for f in result.fills] == [at(1)]
    assert [p.ts for p in result.equity_curve] == [at(1), at(2)]


@pytest.mark.parametrize(
    ("cash", "rules", "reason"),
    [
        # 1 / 100 = 0.01, below a minimum amount of 0.02
        (D(1), RULES.model_copy(update={"min_amount": D("0.02")}), "below min_amount"),
        # 4 USDT of coin, below a minimum order of 5 USDT
        (D(4), RULES.model_copy(update={"min_cost": D(5)}), "below min_cost"),
        # 0.001 / 100 = 0.00001, which rounds down to nothing at a 0.0001 step
        (D("0.001"), RULES, "rounds to 0"),
    ],
    ids=["min_amount", "min_cost", "zero"],
)
def test_orders_below_the_minimums_are_skipped(
    cash: Decimal, rules: MarketRules, reason: str
) -> None:
    candles = flat(range(4))
    strategy = script(buys=(1,))

    result = run_backtest(
        strategy, candles, config(strategy, 1, 4, initial_cash=cash, market_rules=rules)
    )

    assert result.fills == []
    assert len(result.skipped) == 1
    assert result.skipped[0].signal_ts == at(1)
    assert result.skipped[0].side == "buy"
    assert reason in result.skipped[0].reason
    assert result.final_cash == cash


def test_a_fixed_size_buys_that_value() -> None:
    candles = flat(range(4))
    strategy = script(buys=(1,))

    result = run_backtest(
        strategy, candles, config(strategy, 1, 4, sizing=FixedAmount(amount=D(250)))
    )

    # 250 / 100 = 2.5 at candle 2's open; the rest stays cash.
    assert [(f.ts, f.amount) for f in result.fills] == [(at(2), D("2.5"))]
    assert result.final_cash == D(750)


def test_percent_of_equity_grows_with_the_account() -> None:
    # Half the equity each time. Buy at 100: 500 / 100 = 5. Sell at 120: cash 500 + 600 =
    # 1100. Buy again at 100: 550 / 100 = 5.5.
    candles = [*flat(range(3)), *flat(range(3, 5), "120"), *flat(range(5, 7))]
    strategy = script(buys=(1, 4), sells=(2,))

    result = run_backtest(
        strategy, candles, config(strategy, 1, 7, sizing=PercentOfEquity(percent=D(50)))
    )

    assert [(f.side, f.price, f.amount) for f in result.fills] == [
        ("buy", D(100), D(5)),
        ("sell", D(120), D(5)),
        ("buy", D(100), D("5.5")),
    ]


def ranged(hour: int) -> Candle:
    # Opens and closes at 100, ranging 99 to 101: a true range of 2 every candle.
    return Candle(ts=at(hour), open=D(100), high=D(101), low=D(99), close=D(100), volume=D(1))


def test_atr_risk_sizes_from_the_candles_up_to_the_signal() -> None:
    # ATR period 2 needs 13 candles (6 x 2 + 1): 0-12 are warm-up. BUY on candle 13, the
    # ATR of candles 1-13 is 2: 1% of 1000 = 10 at risk / (2 x 2) = 2.5 at candle 14's open.
    candles = [ranged(h) for h in range(16)]
    strategy = script(buys=(13,))
    sizing = AtrRisk(period=2)

    result = run_backtest(strategy, candles, config(strategy, 13, 16, sizing=sizing))

    assert [(f.ts, f.amount) for f in result.fills] == [(at(14), D("2.5"))]
    assert warmup_needed(strategy, config(strategy, 13, 16, sizing=sizing)) == 13


def test_atr_risk_needs_its_candles_before_the_start() -> None:
    candles = [ranged(h) for h in range(16)]
    strategy = script(buys=(13,))  # warm-up 1, but ATR period 2 needs 13 candles

    with pytest.raises(BacktestError, match="needs 13 warm-up candles"):
        run_backtest(strategy, candles, config(strategy, 5, 16, sizing=AtrRisk(period=2)))


def hl(hour: int, open_: str, high: str, low: str, close: str) -> Candle:
    return Candle(ts=at(hour), open=D(open_), high=D(high), low=D(low), close=D(close), volume=D(1))


def stopped(stops: Stops, *later: Candle, **overrides: Any) -> BacktestResult:
    """BUY on candle 1, filled at candle 2's open of 100; then `later` from candle 2 on."""
    candles = [*flat(range(2)), *later]
    strategy = script(buys=(1,), sells=(len(candles) - 2,))
    end = len(candles)
    return run_backtest(strategy, candles, config(strategy, 1, end, stops=stops, **overrides))


def test_a_stop_loss_sells_at_the_stop() -> None:
    # 5% below the 100 entry: 95. Candle 3's low of 94 reaches it. Fee 0.1%:
    #   buy 9.99 at 100 (as above), cash 0.001
    #   sell 9.99 at 95 = 949.05, fee 0.94905: cash 0.001 + 949.05 - 0.94905 = 948.10195
    result = stopped(
        Stops(stop_loss=Percent(percent=D(5))),
        hl(2, "100", "101", "99", "100"),
        hl(3, "98", "99", "94", "96"),
        hl(4, "96", "96", "96", "96"),
        hl(5, "96", "96", "96", "96"),
        fee_rate=D("0.001"),
    )

    assert result.fills[1] == Fill(
        ts=at(3),
        side="sell",
        price=D(95),
        amount=D("9.99"),
        fee=D("0.94905"),
        cash_after=D("948.10195"),
        exit="stop_loss",
    )
    assert result.fills[0].exit is None  # the strategy's buy
    # The strategy's own SELL (candle 4) finds nothing left to sell.
    assert [(s.signal_ts, s.reason) for s in result.skipped] == [(at(4), "nothing to sell")]


def test_a_stop_exit_pays_slippage_like_any_fill() -> None:
    # Bought at 100 x 1.01 = 101: the 5% stop sits at 95.95. Sold at 95.95 x 0.99 = 94.9905.
    result = stopped(
        Stops(stop_loss=Percent(percent=D(5))),
        hl(2, "100", "100", "100", "100"),
        hl(3, "98", "99", "94", "96"),
        hl(4, "96", "96", "96", "96"),
        slippage_rate=D("0.01"),
    )

    assert [(f.side, f.price, f.exit) for f in result.fills] == [
        ("buy", D(101), None),
        ("sell", D("94.9905"), "stop_loss"),
    ]


def test_a_stop_can_fire_on_the_candle_the_buy_filled() -> None:
    # Filled at candle 2's open of 100; its low of 94 is through the 95 stop already.
    result = stopped(
        Stops(stop_loss=Percent(percent=D(5))),
        hl(2, "100", "100", "94", "96"),
        hl(3, "96", "96", "96", "96"),
    )

    assert [(f.ts, f.side, f.price) for f in result.fills] == [
        (at(2), "buy", D(100)),
        (at(2), "sell", D(95)),
    ]


def test_a_take_profit_sells_at_its_level() -> None:
    # 10% above 100: 110, reached by candle 3's high of 111.
    result = stopped(
        Stops(take_profit=Percent(percent=D(10))),
        hl(2, "100", "102", "99", "101"),
        hl(3, "105", "111", "104", "108"),
        hl(4, "108", "108", "108", "108"),
    )

    assert [(f.side, f.price, f.exit) for f in result.fills][1] == ("sell", D(110), "take_profit")
    assert result.final_cash == D(1100)  # 10 x 110, no fees or slippage here


def test_a_trailing_stop_follows_the_price_up_then_sells() -> None:
    # 10% trail. Candle 2 fills at 100, high 110: stop 99 after it. Candle 3's high of 120:
    # 108 after it (its low of 107 is checked against 99). Candle 4's low of 106: out at 108.
    result = stopped(
        Stops(trailing=Percent(percent=D(10))),
        hl(2, "100", "110", "100", "108"),
        hl(3, "108", "120", "107", "118"),
        hl(4, "115", "116", "106", "107"),
        hl(5, "107", "107", "107", "107"),
    )

    assert [(f.ts, f.side, f.price, f.exit) for f in result.fills] == [
        (at(2), "buy", D(100), None),
        (at(4), "sell", D(108), "trailing_stop"),
    ]


def test_without_an_atr_there_is_no_buy() -> None:
    # Flat candles have an ATR of 0: an ATR stop can't be placed, so the buy is skipped
    # rather than opened without the stop it was told to have.
    candles = flat(range(16))
    strategy = script(buys=(13,))
    stops = Stops(stop_loss=AtrMultiple(multiple=D(2)), atr_period=2)  # needs 13 candles

    result = run_backtest(strategy, candles, config(strategy, 13, 16, stops=stops))

    assert result.fills == []
    assert [(s.side, s.reason) for s in result.skipped] == [
        ("buy", "ATR is 0: no price movement to set a stop by")
    ]


def test_an_atr_stop_uses_the_atr_up_to_the_signal() -> None:
    # Ranged candles (true range 2): ATR 2. 2 x 2 = 4 below the 100 entry: 96.
    candles = [*(ranged(h) for h in range(15)), hl(15, "97", "98", "95", "96")]
    strategy = script(buys=(13,))
    stops = Stops(stop_loss=AtrMultiple(multiple=D(2)), atr_period=2)

    result = run_backtest(strategy, candles, config(strategy, 13, 16, stops=stops))

    assert [(f.ts, f.side, f.price, f.exit) for f in result.fills] == [
        (at(14), "buy", D(100), None),
        (at(15), "sell", D(96), "stop_loss"),
    ]
    assert warmup_needed(strategy, config(strategy, 13, 16, stops=stops)) == 13


def test_buy_while_holding_and_sell_while_flat_are_skipped() -> None:
    candles = flat(range(5))
    strategy = script(buys=(1, 2), sells=(0, 3))

    result = run_backtest(strategy, candles, config(strategy, 1, 5))

    assert [(f.ts, f.side) for f in result.fills] == [(at(2), "buy"), (at(4), "sell")]
    assert [(s.signal_ts, s.side, s.reason) for s in result.skipped] == [
        (at(0), "sell", "nothing to sell"),
        (at(2), "buy", "already holding"),
    ]


def test_a_signal_on_the_last_candle_is_not_filled() -> None:
    candles = flat(range(4))
    strategy = script(buys=(3,))

    result = run_backtest(strategy, candles, config(strategy, 1, 4))

    assert result.fills == []
    assert [s.signal_ts for s in result.skipped] == [at(3)]
    assert "last candle" in result.skipped[0].reason
    assert [s.ts for s in result.signals] == [at(3)]


def test_an_open_position_is_valued_at_the_last_close() -> None:
    candles = [candle(0, "100"), candle(1, "100"), candle(2, "100", "120")]
    strategy = script(buys=(1,))

    result = run_backtest(strategy, candles, config(strategy, 1, 3))

    assert result.final_amount == D(10)
    assert result.final_cash == 0
    assert result.final_equity == D(1200)  # no exit fee: it hasn't been sold


def test_records_every_signal_that_is_not_hold() -> None:
    candles = flat(range(5))
    strategy = script(buys=(1, 2), sells=(3,))

    result = run_backtest(strategy, candles, config(strategy, 1, 5))

    assert [(s.ts, s.action) for s in result.signals] == [
        (at(1), "buy"),
        (at(2), "buy"),
        (at(3), "sell"),
    ]


def test_same_inputs_give_the_same_result() -> None:
    candles = [candle(h, str(100 + h % 7), str(101 + h % 5)) for h in range(40)]
    strategy = script(buys=(3, 20), sells=(10, 30), warmup=3)
    cfg = config(strategy, 3, 40, fee_rate=D("0.001"), slippage_rate=D("0.002"))

    first = run_backtest(strategy, candles, cfg)

    assert first == run_backtest(strategy, candles, cfg)
    assert len(first.fills) == 4
    # Round-trips through JSON, as it does through the database, without losing a digit.
    assert BacktestResult.model_validate_json(first.model_dump_json()) == first


def test_ema_crossover_trades_where_it_crosses() -> None:
    # The step series from test_ema_crossover: flat, then up (BUY), then down (SELL).
    # Each signal fills one candle later, at that candle's open.
    strategy = EmaCrossover(EmaCrossoverParams(fast=3, slow=5))
    warmup = strategy.warmup_candles
    up, down = warmup + 5, warmup + 5 + 15
    closes = ["100"] * up + ["110"] * (down - up) + ["90"] * 15
    candles = [candle(h, close) for h, close in enumerate(closes)]

    result = run_backtest(strategy, candles, config(strategy, warmup, len(closes)))

    assert [(f.ts, f.side, f.price) for f in result.fills] == [
        (at(up + 1), "buy", D(110)),
        (at(down + 1), "sell", D(90)),
    ]


def test_rsi_mean_reversion_trades_where_it_signals() -> None:
    # The V from test_rsi_mean_reversion: BUY on candle 17, SELL on 18.
    strategy = RsiMeanReversion(RsiMeanReversionParams(period=2))
    closes = ["10"] * 14 + ["9", "8", "7", "8", "9", "10"]
    candles = [candle(h, close) for h, close in enumerate(closes)]

    result = run_backtest(strategy, candles, config(strategy, 13, len(closes)))

    assert [(f.ts, f.side, f.price) for f in result.fills] == [
        (at(18), "buy", D(9)),
        (at(19), "sell", D(10)),
    ]


def test_bollinger_breakout_trades_where_it_signals() -> None:
    # The breakout from test_bollinger_breakout, with every candle's volume 1, so a
    # volume factor of 1 lets it through: BUY on candle 5, SELL on 8.
    strategy = BollingerBreakout(BollingerBreakoutParams(period=3, width=1.0, volume_factor=1.0))
    closes = ["10"] * 5 + ["11", "12", "11.6", "11", "11"]
    candles = [candle(h, close) for h, close in enumerate(closes)]

    result = run_backtest(strategy, candles, config(strategy, 4, len(closes)))

    assert [(f.ts, f.side, f.price) for f in result.fills] == [
        (at(6), "buy", D(12)),
        (at(9), "sell", D(11)),
    ]


# --- Bad inputs -------------------------------------------------------------------------


def test_gaps_refuse_the_run() -> None:
    candles = [*flat(range(3)), *flat(range(5, 8))]  # candles 3 and 4 are missing
    strategy = script()

    with pytest.raises(BacktestError, match="gap"):
        run_backtest(strategy, candles, config(strategy, 1, 8))


def test_too_little_warmup_history_is_an_error() -> None:
    candles = flat(range(5))
    strategy = script(warmup=3)

    with pytest.raises(BacktestError, match="warm-up"):
        run_backtest(strategy, candles, config(strategy, 2, 5))


def test_candles_must_cover_the_whole_range() -> None:
    candles = flat(range(4))
    strategy = script()

    with pytest.raises(BacktestError, match="end"):
        run_backtest(strategy, candles, config(strategy, 1, 6))


def test_the_config_must_match_the_strategy() -> None:
    candles = flat(range(4))
    strategy = script(buys=(1,))

    with pytest.raises(BacktestError, match="params"):
        run_backtest(strategy, candles, config(strategy, 1, 4, params={"buys": [2]}))
    with pytest.raises(BacktestError, match="version"):
        run_backtest(strategy, candles, config(strategy, 1, 4, strategy_version=2))


def test_range_must_start_before_it_ends_on_candle_boundaries() -> None:
    strategy = script()
    with pytest.raises(ValueError, match="before"):
        config(strategy, 4, 4)
    half_past = config(strategy, 1, 4).model_dump() | {"start": at(1) + timedelta(minutes=30)}
    with pytest.raises(ValueError, match="candle"):
        BacktestConfig.model_validate(half_past)
