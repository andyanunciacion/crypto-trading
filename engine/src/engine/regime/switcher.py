"""Auto-switching: when a bot switches strategy because the regime changed (P6-4, D54).

A bot with auto-switching on maps each regime but `unclear` to a strategy config
(`shared.regime.AutoSwitchSettings`). On each closed candle the detector reads the
regime of the last `min_hold` candles, each over its own window. The regime is
*confirmed* when all of them read the same one and it isn't `unclear`; if the bot
doesn't already run that regime's config, it switches to it.

- **The minimum hold** keeps one odd candle from switching the bot. It's a cooldown
  too: switching back needs `min_hold` new readings in a row, so two switches are always
  at least `min_hold` candles apart.
- **`unclear` is the dead band.** It never confirms, and a reading of it breaks a run,
  so the bot keeps the config it has until a regime holds again.
- **Nothing is remembered.** The bot's active config is the only state: which regime it
  was "in" is whichever config it runs. So a restart, a live bot and a backtest (P6-5)
  decide alike, from the candles alone, as a strategy does (D28). Two regimes mapped to
  the same config never switch between each other.

This adds to the detector's lag (D53): a new trend is seen about 2 x `adx_period`
candles late, and confirmed `min_hold - 1` candles after that.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from engine.exchange.models import Candle
from engine.regime.detector import RegimeDetector, RegimeReading
from engine.strategies.base import Strategy
from engine.strategies.registry import build, strategy_class
from shared.regime import AutoSwitchSettings, MappedConfig, MappedRegime


@dataclass(frozen=True)
class AutoSwitch:
    """A switch the regime calls for: to `target`, the config `regime` is mapped to."""

    regime: MappedRegime
    target: Strategy[Any]
    reason: str


@dataclass(frozen=True)
class RegimeCheck:
    """What the regime says at one closed candle."""

    reading: RegimeReading  # the newest candle's
    held: int  # how many of the newest readings in a row are that regime, up to min_hold
    confirmed: MappedRegime | None  # the regime, once it has held min_hold candles
    switch: AutoSwitch | None  # when the bot doesn't run that regime's config yet


class AutoSwitcher:
    def __init__(self, settings: AutoSwitchSettings) -> None:
        """Raises ValueError (pydantic's ValidationError among them) if a mapped config
        doesn't build: an unknown strategy, bad params, or a version that moved on."""
        self.settings = settings
        self.detector = RegimeDetector(settings.detector)
        targets: dict[MappedRegime, Strategy[Any]] = {}
        for regime, config in settings.mapping.items():
            if config.version is None:
                raise ValueError(f"{regime}: no strategy version (the engine stores one)")
            targets[regime] = build(config.strategy, config.version, config.params)
        self.targets = targets

    @property
    def window(self) -> int:
        """Closed candles a check needs: the detector's window for each of `min_hold`."""
        return self.detector.warmup_candles + self.settings.min_hold - 1

    def check(
        self, candles: Sequence[Candle], current: Strategy[Any], step: timedelta
    ) -> RegimeCheck:
        """The regime at the last of `candles` (closed, oldest first), for a bot running
        `current`. Only the candles after any gap count: a window across a missing candle
        would mix unrelated prices (D25), so the readings warm up again after one."""
        if not candles:
            raise ValueError("check needs at least one candle")
        recent = list(candles[-self.window :])
        start = next(
            (i for i in range(len(recent) - 1, 0, -1) if recent[i].ts - recent[i - 1].ts != step),
            0,
        )
        recent = recent[start:]
        min_hold = self.settings.min_hold
        readings = [
            self.detector.detect(recent[:end])
            for end in range(max(1, len(recent) - min_hold + 1), len(recent) + 1)
        ]
        reading = readings[-1]
        held = 0
        for earlier in reversed(readings):
            if earlier.regime != reading.regime:
                break
            held += 1
        if reading.regime == "unclear" or held < min_hold:
            return RegimeCheck(reading, held, None, None)
        regime = reading.regime  # not unclear: a mapped regime
        target = self.targets[regime]
        switch = None
        if not same_config(target, current):
            params = json.dumps(target.params.model_dump(mode="json"))
            switch = AutoSwitch(
                regime,
                target,
                f"{regime} for {held} candle{'' if held == 1 else 's'} in a row "
                f"({reading.reason}): "
                f"switch to {target.name} v{target.version} {params}",
            )
        return RegimeCheck(reading, held, regime, switch)


class MappingRefused(ValueError):
    """A regime's config doesn't build: `cause` says why (pydantic's ValidationError for
    bad params, a ValueError for an unknown strategy)."""

    def __init__(self, regime: MappedRegime, cause: ValueError) -> None:
        super().__init__(f"{regime}: {cause}")
        self.regime = regime
        self.cause = cause


def with_versions(settings: AutoSwitchSettings) -> AutoSwitchSettings:
    """`settings` with each regime's config checked and stored at its strategy's version
    now, params with their defaults filled in: what a bot (control/bots.py) and a
    backtest (P6-5) store. Raises MappingRefused for the first that doesn't build."""
    mapping = {}
    for regime, mapped in settings.mapping.items():
        try:
            version = strategy_class(mapped.strategy).version
            strategy = build(mapped.strategy, version, mapped.params)
        except ValueError as exc:  # includes pydantic's ValidationError
            raise MappingRefused(regime, exc) from None
        mapping[regime] = MappedConfig(
            strategy=strategy.name,
            version=strategy.version,
            params=strategy.params.model_dump(mode="json"),
        )
    checked = settings.model_copy(update={"mapping": mapping})
    AutoSwitcher(checked)  # builds, as it will when it's used
    return checked


def same_config(a: Strategy[Any], b: Strategy[Any]) -> bool:
    """Whether two strategies are the same config: name, version and params."""
    return (a.name, a.version, a.params.model_dump(mode="json")) == (
        b.name,
        b.version,
        b.params.model_dump(mode="json"),
    )
