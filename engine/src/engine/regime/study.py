"""P6-5b's study: is the regime detector too late, and does a faster one pay? (D56)

Run with `uv run python -m engine.regime.study FILE...` on candle files that
`record_history` wrote (the first is the one the labeled spans and the efficiency
ratio's levels come from: BTC/USDT 2019-2023; the others are holdouts). For each
detector variant below it prints:

1. **The lag/flip report** (`lag.py`): how many days each labeled span takes to be
   recognised and confirmed, and how often the labels and the confirmed regime change.
2. **The combined backtest** (D55) with P6-5's mapping (trend up -> EMA crossover 9/21,
   range -> RSI, down and volatile -> stay flat), for each hold and policy, with EMA's
   cross entry and with a state entry (buy while fast > slow), against each config alone
   and buy-and-hold over the same candles.

Then it applies the rule fixed before any numbers were seen: a variant is kept only if
its combined return beats the detector's (v1) in at least 6 of the 9 hold x policy
cells on the first file and in at least 2 of the 3 cells (hold 3) on each other file,
both with the cross entry. The state entry is judged the same way, against the cross
entry under the same detector.

The variants, stateless like the detector (D28):
- **v1**: the detector as it is (RegimeDetector, version 1: ADX for strength).
- **er**: Kaufman's efficiency ratio over 20 candles replaces ADX as the strength
  measure; direction is still confirmed by the DI lines and the EMA's slope.
- **adx_er_exit**: ADX finds a trend as v1, but a trend also needs the efficiency ratio
  at or above the range level; below it the reading is `unclear`.
- **adx10**: v1 with `adx_period` 10, the issue's fallback, for reference.

The efficiency ratio's levels are set so it calls "trend" and "range" on as many
candles of the first file as ADX >= 25 and ADX < 20 do, rounded to 0.05: the comparison
is then about timing, not about one of them saying "trend" more often. They're never
fitted to the labeled spans. `--er-levels` runs it with others (a sensitivity check).

**What it found (D56), and why nothing here is live.** `er` passed the backtest rule
and the state entry passed under it, but `er` then failed 3 of the 9 labeled spans that
v1 passes (test_regime_history.py, P6-3's done-when): the ratio ignores how big a move
is, so in the quiet summer of 2023 a drift of 0.01% a day read as a trend. The state
entry failed under v1 (it lost every cell on BTC 2024-2026). `adx_er_exit` changed
almost nothing (that summer was a slow drift, so the ratio stayed above the level) and
`adx10` lost on BTC 2024-2026. So the detector stays at version 1, EMA crossover keeps
its cross entry, and the variants and `EmaStateEntry` live only in this module.
"""

import argparse
import itertools
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

from engine.backtest.compare import BuyAndHold, BuyAndHoldParams
from engine.backtest.models import DEFAULT_INITIAL_CASH, BacktestConfig, BacktestResult
from engine.backtest.simulator import run_backtest, warmup_needed
from engine.exchange.models import Candle, MarketRules
from engine.regime.detector import RegimeDetector, RegimeReading
from engine.regime.history import LAG, SPANS, load
from engine.regime.lag import Label, flips, recognition
from engine.regime.switcher import AutoSwitcher, with_versions
from engine.strategies.base import Position, Signal, Strategy
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from engine.strategies.registry import build
from shared.backtest import BacktestReport, metrics
from shared.commands import SwitchPolicy
from shared.indicators import efficiency_ratio
from shared.logs import configure_logging
from shared.regime import AutoSwitchSettings, MappedConfig, MappedRegime, Regime, RegimeParams

DAY = timedelta(days=1)
D = Decimal
ER_PERIOD = 20
HOLDS = (1, 3, 5)
HOLDOUT_HOLD = 3
POLICIES: tuple[SwitchPolicy, ...] = get_args(SwitchPolicy)
# Kept if it beats v1 (or EMA's cross entry) in this many cells.
KEEP_MAIN = 6  # of len(HOLDS) x len(POLICIES)
KEEP_HOLDOUT = 2  # of len(POLICIES), on each holdout

EMA = build("ema_crossover", 1, {"fast": 9, "slow": 21})
RSI = build("rsi_mean_reversion", 1, {})
FLAT = build("stay_flat", 1, {})


class EmaStateEntry(EmaCrossover):
    """EMA crossover that buys whenever the fast EMA is above the slow one and it's
    flat (a state entry, D31's revisit), so it buys on the candle it's switched to in an
    uptrend instead of waiting for a fresh cross. Exits on the cross below, as v1. Its
    own name, so `same_config` tells it apart from EMA crossover; not registered."""

    name = "ema_crossover_state"

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        signal = super().decide(window, position)
        if position is None and signal.indicators["fast_ema"] > signal.indicators["slow_ema"]:
            return signal.model_copy(update={"action": "buy", "reason": "fast EMA above slow EMA"})
        return signal


EMA_STATE = EmaStateEntry(EmaCrossoverParams(fast=9, slow=21))


@dataclass(frozen=True)
class ErLevels:
    trend: float  # the efficiency ratio at or above this is a trend
    range: float  # below this is a range


class EfficiencyStrength(RegimeDetector):
    """`er`: the efficiency ratio decides trend or range instead of ADX."""

    def __init__(self, params: RegimeParams, levels: ErLevels) -> None:
        super().__init__(params)
        self.levels = levels

    def _decide(self, window: Sequence[Candle]) -> RegimeReading:
        base = super()._decide(window)
        er = efficiency_ratio([float(c.close) for c in window], ER_PERIOD)[-1]
        x = base.indicators | {"er": er}

        def reading(regime: Regime, reason: str) -> RegimeReading:
            return base.model_copy(update={"regime": regime, "reason": reason, "indicators": x})

        if base.regime == "high_volatility":
            return reading("high_volatility", base.reason)
        if er >= self.levels.trend:
            if x["plus_di"] > x["minus_di"] and x["ema_slope_pct"] > 0:
                return reading("trending_up", f"ER {er:.2f}, +DI above -DI and the EMA rising")
            if x["minus_di"] > x["plus_di"] and x["ema_slope_pct"] < 0:
                return reading("trending_down", f"ER {er:.2f}, -DI above +DI and the EMA falling")
            return reading("unclear", f"ER {er:.2f}, but the DI lines and the EMA's slope disagree")
        if er < self.levels.range:
            return reading("ranging", f"ER {er:.2f}, below {self.levels.range:g}: no trend")
        return reading("unclear", f"ER {er:.2f}, between the range and trend levels")


class EfficiencyExit(RegimeDetector):
    """`adx_er_exit`: v1, but a trend also needs the efficiency ratio at the range level."""

    def __init__(self, params: RegimeParams, levels: ErLevels) -> None:
        super().__init__(params)
        self.levels = levels

    def _decide(self, window: Sequence[Candle]) -> RegimeReading:
        base = super()._decide(window)
        er = efficiency_ratio([float(c.close) for c in window], ER_PERIOD)[-1]
        x = base.indicators | {"er": er}
        if base.regime in ("trending_up", "trending_down") and er < self.levels.range:
            reason = f"{base.reason}, but ER {er:.2f} is below {self.levels.range:g}"
            return base.model_copy(update={"regime": "unclear", "reason": reason, "indicators": x})
        return base.model_copy(update={"indicators": x})


class Memo(RegimeDetector):
    """`inner`'s readings, remembered by candle: a check reads each candle `min_hold`
    times, and every run on a file reads the same candles. One per file, since the key
    is the candle's time."""

    def __init__(self, inner: RegimeDetector) -> None:
        super().__init__(inner.params)
        self.inner = inner
        self.seen: dict[tuple[object, int], RegimeReading] = {}

    @property
    def warmup_candles(self) -> int:
        return self.inner.warmup_candles

    def detect(self, candles: Sequence[Candle]) -> RegimeReading:
        key = (candles[-1].ts, min(len(candles), self.warmup_candles))
        if key not in self.seen:
            self.seen[key] = self.inner.detect(candles)
        return self.seen[key]


Variant = Callable[[], RegimeDetector]


def variants(levels: ErLevels) -> dict[str, Variant]:
    return {
        "v1": lambda: RegimeDetector(RegimeParams()),
        "er": lambda: EfficiencyStrength(RegimeParams(), levels),
        "adx_er_exit": lambda: EfficiencyExit(RegimeParams(), levels),
        "adx10": lambda: RegimeDetector(RegimeParams(adx_period=10)),
    }


def er_levels(candles: Sequence[Candle]) -> ErLevels:
    """The efficiency ratio's levels that call as many candles trend and range as ADX's."""
    detector = RegimeDetector(RegimeParams())
    p = detector.params
    w = detector.warmup_candles
    adx_values: list[float] = []
    er_values: list[float] = []
    for end in range(w, len(candles) + 1):
        window = candles[end - w : end]
        adx_values.append(detector.detect(window).indicators["adx"])
        er_values.append(efficiency_ratio([float(c.close) for c in window], ER_PERIOD)[-1])
    trend_share = sum(v >= p.trend_adx for v in adx_values) / len(adx_values)
    range_share = sum(v < p.range_adx for v in adx_values) / len(adx_values)
    ranked = sorted(er_values)
    trend = _quantile(ranked, 1 - trend_share)
    range_ = _quantile(ranked, range_share)
    return ErLevels(trend=round(trend * 20) / 20, range=round(range_ * 20) / 20)


def _quantile(ranked: Sequence[float], q: float) -> float:
    return ranked[min(len(ranked) - 1, max(0, round(q * (len(ranked) - 1))))]


def labels(candles: Sequence[Candle], detector: RegimeDetector) -> list[Label]:
    """Each candle's regime, from the first with a full window."""
    w = detector.warmup_candles
    return [
        (candles[end - 1].ts.date(), detector.detect(candles[end - w : end]).regime)
        for end in range(w, len(candles) + 1)
    ]


# --- The combined backtest --------------------------------------------------------------


def settings(min_hold: int, policy: SwitchPolicy) -> AutoSwitchSettings:
    mapping: dict[MappedRegime, Strategy[Any]] = {
        "trending_up": EMA,
        "ranging": RSI,
        "trending_down": FLAT,
        "high_volatility": FLAT,
    }
    return with_versions(
        AutoSwitchSettings(
            mapping={
                regime: MappedConfig(
                    strategy=s.name, version=s.version, params=s.params.model_dump(mode="json")
                )
                for regime, s in mapping.items()
            },
            min_hold=min_hold,
            policy=policy,
        )
    )


def rules(symbol: str) -> MarketRules:
    return MarketRules(
        symbol=symbol,
        amount_step=D("0.000001"),
        price_step=D("0.01"),
        min_amount=D("0.000001"),
        min_cost=D("1"),
    )


@dataclass(frozen=True)
class Series:
    name: str
    symbol: str
    candles: list[Candle]
    start_index: int  # the first candle every run on it trades, so all share one range

    def config(self, strategy: Strategy[Any], auto: AutoSwitchSettings | None) -> BacktestConfig:
        return BacktestConfig(
            strategy=strategy.name,
            strategy_version=strategy.version,
            params=strategy.params.model_dump(mode="json"),
            exchange="binance",
            symbol=self.symbol,
            timeframe="1d",
            start=self.candles[self.start_index].ts,
            end=self.candles[-1].ts + DAY,
            fee_rate=D("0.001"),
            slippage_rate=D("0.0005"),
            market_rules=rules(self.symbol),
            auto_switch=auto,
        )


def series(path: Path, slowest: Sequence[RegimeDetector]) -> Series:
    candles = load(path)
    symbol = path.name.split("_")[0].upper().replace("USDT", "/USDT")
    # Enough warm-up for the longest hold, the slowest detector and every config.
    auto_settings = settings(max(HOLDS), "keep")
    probe = BacktestConfig(
        strategy=EMA.name,
        strategy_version=EMA.version,
        params=EMA.params.model_dump(mode="json"),
        exchange="binance",
        symbol=symbol,
        timeframe="1d",
        start=candles[-1].ts,
        end=candles[-1].ts + DAY,
        slippage_rate=D("0.0005"),
        market_rules=rules(symbol),
        auto_switch=auto_settings,
    )
    needed = 0
    for detector in slowest:
        auto = AutoSwitcher(auto_settings)
        auto.detector = detector
        needed = max(needed, warmup_needed(EMA, probe, auto), warmup_needed(EMA_STATE, probe, auto))
    return Series(path.stem.removesuffix(".json"), symbol, candles, needed)


def combined(
    s: Series, detector: RegimeDetector, state_entry: bool, hold: int, policy: SwitchPolicy
) -> BacktestResult:
    auto_settings = settings(hold, policy)
    auto = AutoSwitcher(auto_settings)
    auto.detector = detector
    start: Strategy[Any] = EMA
    if state_entry:
        auto.targets = {**auto.targets, "trending_up": EMA_STATE}
        start = EMA_STATE
    return run_backtest(start, s.candles, s.config(start, auto_settings), switcher=auto)


def alone(s: Series, strategy: Strategy[Any]) -> BacktestResult:
    return run_backtest(strategy, s.candles, s.config(strategy, None))


# --- The report -------------------------------------------------------------------------


def _pct(r: BacktestReport) -> str:
    return f"{r.total_return * 100:+.1f}%"


def _detail(r: BacktestReport, switches: int) -> str:
    sharpe = "n/a" if r.sharpe is None else f"{r.sharpe:.2f}"
    return (
        f"{_pct(r):>9}  dd {r.max_drawdown * 100:5.1f}%  Sharpe {sharpe:>5}  "
        f"{r.trade_count:>3} trades  {switches:>3} switches"
    )


def lag_report(name: str, found: list[Label]) -> list[str]:
    lines = [f"  {name}"]
    for expected, start, end, what in SPANS:
        r = recognition(found, expected, start, end, HOLDS)
        first = "never" if r.first is None else f"{r.first:>3}d"
        held = "  ".join(
            f"h{h} {'never' if d is None else f'{d:>3}d'}" for h, d in r.confirmed.items()
        )
        judged_from = start if expected == "high_volatility" else start + LAG
        judged = [regime for day, regime in found if judged_from <= day <= end]
        share = sum(regime == expected for regime in judged) / len(judged)
        lines.append(
            f"    {expected:<15} {start}  first {first}  {held}  "
            f"reads as it {r.share * 100:3.0f}% (from +{(judged_from - start).days}d "
            f"{share * 100:3.0f}%)  {what}"
        )
    return lines


def flip_report(name: str, found: list[Label]) -> str:
    f = flips(found, HOLDS)
    confirmed = "  ".join(f"h{h} {v:5.1f}" for h, v in f.confirmed_per_year.items())
    shares = "  ".join(f"{k} {v * 100:.0f}%" for k, v in sorted(f.shares.items()))
    return f"    {name:<12} labels {f.per_year:5.1f}/yr  confirmed {confirmed}/yr  ({shares})"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path, help="the main file first, then holdouts")
    parser.add_argument(
        "--er-levels",
        nargs=2,
        type=float,
        metavar=("TREND", "RANGE"),
        help="the efficiency ratio's levels instead of the matched ones (a sensitivity check)",
    )
    args = parser.parse_args()
    files: list[Path] = args.files
    # Hundreds of runs: only warnings, to stderr, so the report is all that's printed.
    configure_logging("WARNING", sys.stderr)

    main_candles = load(files[0])
    matched = er_levels(main_candles)
    levels = matched if args.er_levels is None else ErLevels(*args.er_levels)
    made = variants(levels)
    print(f"Efficiency ratio ({ER_PERIOD}) levels, matched to ADX's base rates on {files[0].name}:")
    print(f"  trend at or above {matched.trend:g}, range below {matched.range:g}")
    if levels != matched:
        print(f"  this run uses trend {levels.trend:g}, range {levels.range:g} instead")

    every = [
        series(path, [build_() for build_ in made.values()]) for path in files
    ]  # one range per file for every run
    detectors = {s.name: {n: Memo(build_()) for n, build_ in made.items()} for s in every}

    print("\n1. Lag (days from each labeled span's start) on", every[0].name)
    for n in made:
        for line in lag_report(n, labels(every[0].candles, detectors[every[0].name][n])):
            print(line)
    print("\n   Flips per year:")
    for s in every:
        print(f"   {s.name}")
        for n in made:
            print(flip_report(n, labels(s.candles, detectors[s.name][n])))

    print("\n2. Combined backtest (P6-5's mapping), against each config alone")
    wins: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for index, s in enumerate(every):
        holds = HOLDS if index == 0 else (HOLDOUT_HOLD,)
        first, last = s.candles[s.start_index].ts.date(), s.candles[-1].ts.date()
        print(f"\n  {s.name} ({s.symbol}), {first} to {last}")
        hold_strategy = BuyAndHold(BuyAndHoldParams())
        for label, strategy in (
            ("EMA cross alone", EMA),
            ("EMA state alone", EMA_STATE),
            ("RSI alone", RSI),
            ("buy and hold", hold_strategy),
        ):
            print(f"    {label:<28} {_detail(_report(alone(s, strategy)), 0)}")
        returns: dict[tuple[str, bool], list[Decimal]] = {}
        for n, state_entry in itertools.product(made, (False, True)):
            entry = "state" if state_entry else "cross"
            cells = []
            for hold, policy in itertools.product(holds, POLICIES):
                result = combined(s, detectors[s.name][n], state_entry, hold, policy)
                report = _report(result)
                cells.append(report.total_return)
                tag = f"{n} {entry} h{hold} {policy}"
                print(f"    {tag:<28} {_detail(report, len(result.switches))}")
            returns[(n, state_entry)] = cells
        for n in made:
            if n != "v1":
                wins.setdefault((n, "detector"), []).append(
                    _beats(returns[(n, False)], returns[("v1", False)])
                )
            wins.setdefault((n, "state entry"), []).append(
                _beats(returns[(n, True)], returns[(n, False)])
            )

    print(
        "\n3. Verdicts (cells won / cells; kept needs "
        f"{KEEP_MAIN} on the first file and {KEEP_HOLDOUT} on each holdout):"
    )
    for (n, what), counts in wins.items():
        against = "v1" if what == "detector" else f"{n} with the cross entry"
        scores = ", ".join(f"{won}/{of}" for won, of in counts)
        print(f"  {n} {what} vs {against}: {scores} -> {'KEEP' if _kept(counts) else 'drop'}")


def _report(result: BacktestResult) -> BacktestReport:
    return metrics(DEFAULT_INITIAL_CASH, "1d", result)


def _beats(variant: Sequence[Decimal], baseline: Sequence[Decimal]) -> tuple[int, int]:
    return sum(a > b for a, b in zip(variant, baseline, strict=True)), len(variant)


def _kept(counts: Sequence[tuple[int, int]]) -> bool:
    main_won = counts[0][0]
    return main_won >= KEEP_MAIN and all(won >= KEEP_HOLDOUT for won, _ in counts[1:])


if __name__ == "__main__":
    main()
