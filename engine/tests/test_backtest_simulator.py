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
    buy_amount,
    fee,
    fill_price,
    run_backtest,
)
from engine.exchange.models import Candle, MarketRules
from engine.strategies.base import Position, Signal, Strategy, StrategyParams
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


def test_buy_amount_leaves_room_for_the_fee_and_rounds_down() -> None:
    # 1000 / (100 x 1.001) = 9.99000999..., down to the 0.0001 step.
    assert buy_amount(D(1000), D(100), D("0.001"), D(1), D("0.0001")) == D("9.9900")
    # 1000 / 300 = 3.333..., down (never up) to the 0.01 step.
    assert buy_amount(D(1000), D(300), D(0), D(1), D("0.01")) == D("3.33")
    assert buy_amount(D(1000), D(100), D(0), D("0.5"), D("0.0001")) == D(5)


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
