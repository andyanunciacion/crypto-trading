"""Auto-switching backtests (P6-5, D55): the switcher inside the simulator.

The detector is scripted, as in test_regime_switcher.py, and the regimes' configs are
scripted strategies, so each case says exactly when the regime changes and what each
config signals. A switch confirmed at candle i takes over at candle i + 1, whose signal
fills at i + 2's open, as on a live bot (bots/runner.py). The last test runs the real
detector and strategies over five years of BTC.
"""

import gzip
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from engine.backtest.models import BacktestConfig, BacktestResult
from engine.backtest.report import describe
from engine.backtest.simulator import run_backtest, warmup_needed
from engine.exchange.models import Candle, MarketRules
from engine.regime.detector import Regime, RegimeDetector, RegimeParams, RegimeReading
from engine.regime.switcher import AutoSwitcher
from engine.risk.stops import Percent, Stops
from engine.strategies.base import Position, Signal, Strategy, StrategyParams
from engine.strategies.registry import build
from shared.backtest import ConfigRef, metrics
from shared.commands import SwitchPolicy
from shared.regime import AutoSwitchSettings, MappedConfig

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
EMA = build("ema_crossover", 1, {"fast": 9, "slow": 21})
RSI = build("rsi_mean_reversion", 1, {})
FLAT = build("stay_flat", 1, {})
SHORTHAND: dict[str, Regime] = {
    "U": "trending_up",
    "D": "trending_down",
    "R": "ranging",
    "V": "high_volatility",
    "?": "unclear",
}


def at(hour: int) -> datetime:
    return T0 + hour * HOUR


def flat(n: int, price: str = "100") -> list[Candle]:
    p = D(price)
    return [Candle(ts=at(h), open=p, high=p, low=p, close=p, volume=D(1)) for h in range(n)]


class ScriptParams(StrategyParams):
    tag: str = ""  # tells two scripted configs apart
    buys: tuple[int, ...] = ()  # hours (from T0) of the candles to signal BUY on
    sells: tuple[int, ...] = ()


class Script(Strategy[ScriptParams]):
    """Signals on given candles whatever the position."""

    name = "script"
    version = 1
    params_model = ScriptParams

    @property
    def warmup_candles(self) -> int:
        return 1

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        ts = window[-1].ts
        hour = (ts - T0) // HOUR
        if hour in self.params.buys:
            return Signal(action="buy", ts=ts, reason="scripted")
        if hour in self.params.sells:
            return Signal(action="sell", ts=ts, reason="scripted")
        return Signal(action="hold", ts=ts, reason="scripted")


def script(tag: str, **params: Any) -> Script:
    return Script(ScriptParams(tag=tag, **params))


class Scripted(RegimeDetector):
    """Reads candle i's regime from `regimes[i]`, with no warm-up."""

    def __init__(self, regimes: str) -> None:
        super().__init__(RegimeParams())
        self.regimes = regimes

    @property
    def warmup_candles(self) -> int:
        return 1

    def detect(self, candles: Sequence[Candle]) -> RegimeReading:
        last = candles[-1]
        regime = SHORTHAND[self.regimes[(last.ts - T0) // HOUR]]
        return RegimeReading(regime=regime, ts=last.ts, reason=f"scripted {regime}")


def mapped(strategy: Strategy[Any]) -> MappedConfig:
    return MappedConfig(
        strategy=strategy.name,
        version=strategy.version,
        params=strategy.params.model_dump(mode="json"),
    )


def settings(min_hold: int, policy: SwitchPolicy) -> AutoSwitchSettings:
    # Real configs, so the settings are what a CLI run stores; the tests swap in scripts.
    return AutoSwitchSettings(
        mapping={
            "trending_up": mapped(EMA),
            "ranging": mapped(RSI),
            "trending_down": mapped(FLAT),
            "high_volatility": mapped(FLAT),
        },
        min_hold=min_hold,
        policy=policy,
    )


def switcher(
    regimes: str,
    up: Strategy[Any],
    ranging: Strategy[Any],
    *,
    min_hold: int = 2,
    policy: SwitchPolicy = "hand_over",
) -> AutoSwitcher:
    auto = AutoSwitcher(settings(min_hold, policy))
    auto.detector = Scripted(regimes)
    auto.targets = {
        "trending_up": up,
        "ranging": ranging,
        "trending_down": FLAT,
        "high_volatility": FLAT,
    }
    return auto


def config(
    strategy: Strategy[Any], auto: AutoSwitcher, start: int, end: int, **overrides: Any
) -> BacktestConfig:
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
        "auto_switch": auto.settings,
    }
    return BacktestConfig(**(values | overrides))


def ref(strategy: Strategy[Any]) -> ConfigRef:
    return ConfigRef(
        strategy=strategy.name,
        version=strategy.version,
        params=strategy.params.model_dump(mode="json"),
    )


def run(
    start_on: Strategy[Any],
    auto: AutoSwitcher,
    candles: list[Candle],
    **overrides: Any,
) -> BacktestResult:
    # The scripted switcher needs 2 candles (1 + min_hold - 1) before the start.
    cfg = config(start_on, auto, 2, len(candles), **overrides)
    return run_backtest(start_on, candles, cfg, switcher=auto)


def fills(result: BacktestResult) -> list[tuple[int, str, str | None, str | None]]:
    """(hour, side, the tag of the config that decided it, exit) per fill."""
    return [
        (
            (f.ts - T0) // HOUR,
            f.side,
            None if f.decided_by is None else str(f.decided_by.params["tag"]),
            f.exit,
        )
        for f in result.fills
    ]


# Ranging from candle 5: confirmed with a hold of 2 at candle 6, so B takes over at 7.
RANGING_FROM_5 = "UUUUURRRRRRRRRRR"


def test_a_switch_takes_over_at_the_next_candle() -> None:
    a = script("A", buys=(2,), sells=(9,))
    b = script("B", sells=(6, 7))  # 6 is still A's candle; 7 is B's first
    auto = switcher(RANGING_FROM_5, a, b)

    result = run(a, auto, flat(12))

    [switch] = result.switches
    assert switch.ts == at(6)
    assert switch.regime == "ranging"
    assert (switch.from_config, switch.to_config) == (ref(a), ref(b))
    assert switch.reason.startswith("ranging for 2 candles in a row (scripted ranging): ")
    # B's SELL on candle 7 fills at candle 8's open; A's on 9 is never made.
    assert fills(result) == [(3, "buy", "A", None), (8, "sell", "B", None)]
    assert [s.ts for s in result.signals] == [at(2), at(7)]


def test_running_the_regimes_config_already_is_no_switch() -> None:
    a = script("A", buys=(2,))
    auto = switcher("U" * 8, a, script("B"))

    result = run(a, auto, flat(8))

    assert result.switches == []
    assert fills(result) == [(3, "buy", "A", None)]


def test_hand_over_lets_the_new_config_sell() -> None:
    a = script("A", buys=(2,))
    b = script("B", sells=(10,))
    auto = switcher(RANGING_FROM_5, a, b, policy="hand_over")

    result = run(a, auto, flat(12))

    assert fills(result) == [(3, "buy", "A", None), (11, "sell", "B", None)]


def test_keep_leaves_the_position_to_the_config_that_bought_it() -> None:
    a = script("A", buys=(2,), sells=(10,))
    b = script("B", buys=(9, 12), sells=(7,))
    auto = switcher(RANGING_FROM_5, a, b, policy="keep")

    result = run(a, auto, flat(15))

    # A still sells its trade (candle 10 -> 11); then B, flat, makes its own buy.
    assert fills(result) == [
        (3, "buy", "A", None),
        (11, "sell", "A", None),
        (13, "buy", "B", None),
    ]
    keeper = ref(a).label
    assert [(s.signal_ts, s.side, s.reason) for s in result.skipped] == [
        (at(7), "sell", f"its position is {keeper}'s to sell (switched with keep)"),
        (at(9), "buy", f"its position is {keeper}'s to sell (switched with keep)"),
    ]


def test_keep_stays_with_the_buyer_through_two_switches() -> None:
    # Ranging (B) at 6, then high volatility (stay flat) at 9. Stay flat would sell at
    # every candle; under keep, only A sells it, on candle 11.
    regimes = "UUUUURRRVVVVVVVV"
    a = script("A", buys=(2,), sells=(11,))
    b = script("B", sells=(7, 8))
    auto = switcher(regimes, a, b, policy="keep")

    result = run(a, auto, flat(14))

    assert [(s.ts, s.to_config) for s in result.switches] == [(at(6), ref(b)), (at(9), ref(FLAT))]
    assert fills(result) == [(3, "buy", "A", None), (12, "sell", "A", None)]
    # Stay flat's SELLs, from candle 10 on, are each skipped until A has sold.
    assert [s.signal_ts for s in result.skipped] == [at(7), at(8), at(10), at(11)]


def test_close_first_sells_at_the_next_open() -> None:
    # Bought at 100 x 1.01 = 101: 1000 / (101 x 1.001) = 9.891099... -> 9.8910.
    # Sold at the open after the switch: 100 x 0.99 = 99, fee 99 x 9.8910 x 0.001.
    a = script("A", buys=(2,))
    b = script("B")
    auto = switcher(RANGING_FROM_5, a, b, policy="close_first")

    result = run(a, auto, flat(10), fee_rate=D("0.001"), slippage_rate=D("0.01"))

    assert fills(result) == [(3, "buy", "A", None), (7, "sell", None, "switch")]
    sale = result.fills[1]
    assert (sale.price, sale.amount, sale.fee) == (D(99), D("9.8910"), D("0.979209"))
    report = metrics(D(1000), "1h", result)
    assert [t.exit for t in report.trades] == ["switch"]
    assert report.trades[0].decided_by == ref(a)  # the buy's config


def test_close_first_with_a_buy_on_the_confirming_candle_buys_then_sells() -> None:
    # As live: the bot places A's buy on candle 6, then the switch sells what it bought.
    a = script("A", buys=(6,))
    auto = switcher(RANGING_FROM_5, a, script("B"), policy="close_first")

    result = run(a, auto, flat(10))

    assert fills(result) == [(7, "buy", "A", None), (7, "sell", None, "switch")]


def test_close_first_with_a_sell_on_the_confirming_candle_sells_once() -> None:
    a = script("A", buys=(2,), sells=(6,))
    auto = switcher(RANGING_FROM_5, a, script("B"), policy="close_first")

    result = run(a, auto, flat(10))

    assert fills(result) == [(3, "buy", "A", None), (7, "sell", "A", None)]


def test_close_first_while_flat_sells_nothing() -> None:
    a = script("A")
    auto = switcher(RANGING_FROM_5, a, script("B"), policy="close_first")

    result = run(a, auto, flat(10))

    assert result.fills == []
    assert result.skipped == []
    assert len(result.switches) == 1


def test_the_trade_keeps_its_stops_after_a_switch() -> None:
    # A buys at 100 with a 5% stop-loss; B takes over and never sells; candle 9's low
    # of 94 reaches the stop at 95.
    a = script("A", buys=(2,))
    auto = switcher(RANGING_FROM_5, a, script("B"), policy="hand_over")
    candles = flat(12)
    candles[9] = candles[9].model_copy(update={"low": D(94)})

    result = run(a, auto, candles, stops=Stops(stop_loss=Percent(percent=D(5))))

    assert fills(result) == [(3, "buy", "A", None), (9, "sell", None, "stop_loss")]


def test_the_report_says_what_it_switched_between_and_how_each_config_did() -> None:
    a = script("A", buys=(2,), sells=(9,))
    b = script("B", buys=(9,), sells=(7,))
    auto = switcher(RANGING_FROM_5, a, b)
    candles = flat(12)
    cfg = config(a, auto, 2, 12)

    text = describe(7, cfg, run_backtest(a, candles, cfg, switcher=auto))

    a_label, b_label = ref(a).label, ref(b).label
    lines = text.splitlines()
    assert "  auto-switching: min hold 2, policy hand_over" in lines
    assert (
        '    ranging: rsi_mean_reversion v1 {"period": 14, "oversold": 30.0, "overbought": 70.0}'
        in lines
    )
    assert "  1 switch, 876.0 a year:" in lines  # 1 in 10 hours
    assert f"    2026-01-01 06:00 ranging: {a_label} -> {b_label}" in lines
    # A decides candles 2-6 and bought the first trade; B decides 7-11, sold that trade,
    # and bought on 9 (open at the end).
    assert f"    {a_label}: 5 candles active, 1 trade, P&L +0.00 USDT, 0.0% won" in lines
    assert f"    {b_label}: 5 candles active, 0 trades, P&L +0.00 USDT" in lines


def test_warmup_counts_the_detector_and_every_mapped_config() -> None:
    cfg = BacktestConfig(
        strategy=EMA.name,
        strategy_version=EMA.version,
        params=EMA.params.model_dump(mode="json"),
        exchange="mexc",
        symbol="BTC/USDT",
        timeframe="1h",
        start=at(300),
        end=at(400),
        slippage_rate=D(0),
        market_rules=RULES,
        auto_switch=settings(3, "keep"),
    )
    # The detector's 199 plus 2 more readings; EMA 9/21 needs 64, RSI 14 needs 85.
    assert warmup_needed(EMA, cfg) == 201
    long_rsi = settings(3, "keep").model_copy(
        update={
            "mapping": {
                **settings(3, "keep").mapping,
                "ranging": MappedConfig(
                    strategy="rsi_mean_reversion", version=1, params={"period": 50}
                ),
            }
        }
    )
    assert warmup_needed(EMA, cfg.model_copy(update={"auto_switch": long_rsi})) == 301


def test_without_auto_switching_nothing_changes() -> None:
    a = script("A", buys=(2,), sells=(5,))
    cfg = config(a, switcher("U" * 8, a, script("B")), 2, 8, auto_switch=None)

    result = run_backtest(a, flat(8), cfg)

    assert result.switches == []
    assert [(f.side, f.decided_by) for f in result.fills] == [("buy", None), ("sell", None)]


# --- Five years of BTC (the done-when) ----------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "binance" / "btcusdt_1d_2019_2023.json.gz"
DAY = timedelta(days=1)
BTC_RULES = MarketRules(
    symbol="BTC/USDT",
    amount_step=D("0.000001"),
    price_step=D("0.01"),
    min_amount=D("0.000001"),
    min_cost=D("1"),
)


@pytest.fixture(scope="module")
def btc() -> list[Candle]:
    rows = json.loads(gzip.decompress(FIXTURE.read_bytes()))
    return [Candle.model_validate(row) for row in rows]


def btc_config(candles: list[Candle], min_hold: int, policy: SwitchPolicy) -> BacktestConfig:
    auto = settings(min_hold, policy)
    probe = BacktestConfig(
        strategy=EMA.name,
        strategy_version=EMA.version,
        params=EMA.params.model_dump(mode="json"),
        exchange="binance",
        symbol="BTC/USDT",
        timeframe="1d",
        start=candles[-1].ts,
        end=candles[-1].ts + DAY,
        fee_rate=D("0.001"),
        slippage_rate=D("0.0005"),
        market_rules=BTC_RULES,
        auto_switch=auto,
    )
    warmup = warmup_needed(EMA, probe)
    return probe.model_copy(update={"start": candles[warmup].ts})


def replayed(candles: list[Candle], cfg: BacktestConfig) -> list[tuple[datetime, Strategy[Any]]]:
    """The switches AutoSwitcher.check() calls for, fed candle by candle as the bot runner
    feeds it (test_auto_switch_replay.py), from the run's last warm-up candle on."""
    assert cfg.auto_switch is not None
    auto = AutoSwitcher(cfg.auto_switch)
    current: Strategy[Any] = EMA
    first = (cfg.start - candles[0].ts) // DAY
    switches = []
    for i in range(first - 1, len(candles)):
        check = auto.check(candles[max(0, i + 1 - auto.window) : i + 1], current, DAY)
        if check.switch is not None:
            switches.append((candles[i].ts, check.switch.target))
            current = check.switch.target
    return switches


@pytest.mark.parametrize(
    ("min_hold", "policy"),
    [(1, "keep"), (3, "keep"), (5, "keep"), (3, "close_first"), (3, "hand_over")],
)
def test_btc_switches_as_the_live_rule_does_and_reports(
    btc: list[Candle], min_hold: int, policy: SwitchPolicy
) -> None:
    cfg = btc_config(btc, min_hold, policy)

    result = run_backtest(EMA, btc, cfg)

    expected = replayed(btc, cfg)
    assert len(result.switches) == len(expected) > 0
    for switch, (ts, target) in zip(result.switches, expected, strict=True):
        assert switch.ts == ts
        assert switch.to_config == ref(target)
    # The full report: every fill pairs into trades, and the equity curve covers the range.
    report = metrics(cfg.initial_cash, cfg.timeframe, result)
    assert report.trade_count > 0
    assert all(t.decided_by is not None for t in report.trades)
    assert len(result.equity_curve) == len(btc) - (cfg.start - btc[0].ts) // DAY
    if policy == "close_first":
        assert any(t.exit == "switch" for t in report.trades)
    print(describe(0, cfg, result))
