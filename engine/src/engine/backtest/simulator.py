"""Replays closed candles through a strategy and simulates its trades (ARCHITECTURE §7, D29).

The fill model:
- A signal on a candle fills at the *next* candle's open. The strategy only sees a
  candle once it has closed, so filling at that close would be look-ahead bias: trading
  at a price that was gone by the time the signal existed.
- Slippage moves every fill price against us; the fee is a rate on each fill's value,
  in the quote currency.
- Spot, long only, one position at a time. A BUY is sized by the run's sizing mode,
  as a paper bot's is (risk/sizing.py, D39; all-in by default), and a SELL sells
  everything, or the share of it its `size_hint` asks for (`sell_amount`). An
  ATR-sized BUY uses the ATR of the candles up to its signal's candle, what a live bot
  knows when it decides.
- A BUY while holding is skipped, unless its strategy `scales_in` (grid, DCA; P6-6,
  D58): then it adds to the position, sized with the coins held counted in the equity,
  and the position's entry is averaged as the paper ledger averages it.
- Amounts round down to the market's step. Orders below its minimums are skipped,
  never rounded up (D22).
- Stops (risk/stops.py, D40), if the run has any, are placed from a BUY's fill price
  and checked on every candle from that one on, after its open: a hit sells the whole
  position at the stop's price, with slippage and the fee like any fill. A BUY whose
  stops can't be placed (no ATR) is skipped. A BUY that adds to the position places
  them again from its new average entry (`reentry`), as a bot does; a partial sell
  leaves them where they were.

Auto-switching (P6-5, D55), if the run has it, switches the strategy as an
auto-switching bot does (bots/runner.py), with the same rule (regime/switcher.py):
- After each candle's signal, the regime is checked at that candle. A switch it calls
  for takes over from the next candle on, so its first signal fills an open later.
- The switch policy says what happens to an open position, as on a live bot (D51):
  `keep` leaves it to the config that bought it, whose SELL alone is acted on until
  the run is flat; `close_first` sells it at the next open (exit `switch`), after a
  BUY made on the same candle, as the bot places that before the switch is made;
  `hand_over` lets the new config sell it. Its stops stay, whatever the policy.
- Unlike a live bot, the new config is always warm by the next candle: it has every
  candle here, where a bot fetches them over REST (D51).

Pure and synchronous: no database, no exchange, no clock. Same inputs, same result.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Any

import structlog

from engine.backtest.models import (
    BacktestConfig,
    BacktestResult,
    BacktestSwitch,
    ConfigRef,
    EquityPoint,
    Fill,
    SkippedOrder,
)
from engine.exchange.models import Candle, OrderSide, Timeframe
from engine.marketdata.timeframes import STEP
from engine.regime.switcher import AutoSwitcher, same_config
from engine.risk.sizing import (
    NotSized,
    atr_for,
    atr_of,
    below_minimum,
    entry_amount,
    lookback,
    sell_amount,
)
from engine.risk.stops import (
    NO_STOPS,
    ExitState,
    NoLevels,
    after_candle,
    exit_on_candle,
    levels_at_entry,
    reentry,
)
from engine.risk.stops import lookback as stops_lookback
from engine.strategies.base import Position, Signal, Strategy, StrategyParams
from shared.backtest import FillExit

# Stored with every run. Bump it when the fill model changes, so old runs aren't
# "reproduced" by different rules.
BACKTESTER_VERSION = 1

log = structlog.get_logger()


class BacktestError(Exception):
    """The run can't go ahead: bad or incomplete candles, or a config that doesn't fit."""


def fill_price(side: OrderSide, open_: Decimal, slippage_rate: Decimal) -> Decimal:
    """Buys pay more than the open, sells get less."""
    if side == "buy":
        return open_ * (1 + slippage_rate)
    return open_ * (1 - slippage_rate)


def fee(price: Decimal, amount: Decimal, fee_rate: Decimal) -> Decimal:
    return price * amount * fee_rate


def _side(signal: Signal) -> OrderSide:
    return "buy" if signal.action == "buy" else "sell"  # callers never pass a HOLD


def warmup_needed[P: StrategyParams](
    strategy: Strategy[P], config: BacktestConfig, switcher: AutoSwitcher | None = None
) -> int:
    """Candles a run needs before its start: for the strategy, for ATR sizing or stops,
    and with auto-switching for the regime's readings and every config it maps."""
    needed = max(strategy.warmup_candles, _atr_window(config), 1)
    auto = _switcher(config, switcher)
    if auto is None:
        return needed
    return max(needed, auto.window, *(s.warmup_candles for s in auto.targets.values()))


def _atr_window(config: BacktestConfig) -> int:
    return max(lookback(config.sizing), stops_lookback(config.stops))


def _switcher(config: BacktestConfig, given: AutoSwitcher | None) -> AutoSwitcher | None:
    """The run's switcher: `given` (tests script one), or built from the config. Raises
    ValueError if a mapped config doesn't build."""
    if config.auto_switch is None:
        if given is not None:
            raise BacktestError("a switcher was given, but the config has no auto_switch")
        return None
    if given is None:
        return AutoSwitcher(config.auto_switch)
    if given.settings != config.auto_switch:
        raise BacktestError("the switcher's settings aren't the config's auto_switch")
    return given


def config_ref(strategy: Strategy[Any]) -> ConfigRef:
    return ConfigRef(
        strategy=strategy.name,
        version=strategy.version,
        params=strategy.params.model_dump(mode="json"),
    )


@dataclass
class _Account:
    """The simulated balances, and the record of what happened to them."""

    config: BacktestConfig
    cash: Decimal
    position: Position | None = None
    exits: ExitState | None = None  # the open position's stops, if the run has any
    # The config whose BUY opened the position (auto-switching runs only): its keeper,
    # under the policy `keep`.
    opened_by: Strategy[Any] | None = None
    fills: list[Fill] = field(default_factory=list)
    skipped: list[SkippedOrder] = field(default_factory=list)
    total_fees: Decimal = Decimal(0)

    @property
    def amount(self) -> Decimal:
        return Decimal(0) if self.position is None else self.position.amount

    def skip(self, signal: Signal, side: OrderSide, reason: str) -> None:
        self.skipped.append(SkippedOrder(signal_ts=signal.ts, side=side, reason=reason))
        log.debug("backtest.skipped", signal_ts=signal.ts.isoformat(), side=side, reason=reason)

    def execute(
        self,
        signal: Signal,
        candle: Candle,
        history: Sequence[Candle],
        decider: Strategy[Any],
        *,
        recorded: bool = False,
    ) -> None:
        """Fill `signal` (from the previous candle) at `candle`'s open, if it can be.

        `history` is the closed candles up to the signal's, for ATR sizing and stops.
        `decider` is the config whose signal it is; `recorded` names it on the fill (an
        auto-switching run).
        """
        side = _side(signal)
        price = fill_price(side, candle.open, self.config.slippage_rate)
        rules = self.config.market_rules
        adding = side == "buy" and self.position is not None
        levels: ExitState | None = None
        if side == "buy":
            if adding and not decider.scales_in:
                self.skip(signal, side, "already holding")
                return
            sizing = self.config.sizing
            sized = entry_amount(
                sizing,
                cash=self.cash,
                # The coins at the open, the price a bot would see as it buys.
                equity=self.cash + self.amount * candle.open,
                price=price,
                fee_rate=self.config.fee_rate,
                rules=rules,
                size_hint=signal.size_hint,
                atr=atr_for(sizing, history, self.config.timeframe),
            )
            if isinstance(sized, NotSized):
                self.skip(signal, side, sized.reason)
                return
            amount = sized.amount
            stops = self.config.stops
            if stops != NO_STOPS:
                atr = atr_of(history, stops.atr_period, self.config.timeframe)
                placed = levels_at_entry(stops, price, atr if stops.uses_atr else None)
                if isinstance(placed, NoLevels):
                    # Not bought without the stop it was told to have, as a bot skips it.
                    self.skip(signal, side, placed.reason)
                    return
                levels = placed
        else:
            if self.position is None:
                self.skip(signal, side, "nothing to sell")
                return
            sold = sell_amount(self.position.amount, signal.size_hint, price, rules)
            if isinstance(sold, NotSized):
                self.skip(signal, side, sold.reason)
                return
            amount = sold.amount
        by = config_ref(decider) if recorded else None
        self._fill(candle.ts, side, price, amount, decided_by=by)
        if self.position is None:
            self.exits = None  # sold out
            self.opened_by = None
        elif adding:
            self.exits = self._exits_after_add(levels)
        elif side == "buy":
            self.exits = levels
            self.opened_by = decider if recorded else None
        # A partial sell leaves the average entry, and so the stops, as they were.

    def _exits_after_add(self, levels: ExitState | None) -> ExitState | None:
        """The stops of a position a buy just added to, placed again from its new
        average entry, keeping the trailing stop's peak (`reentry`), as a bot does
        (bots/runner.py's _levels). `levels` are the add's own, placed from its price."""
        assert self.position is not None  # just bought
        if levels is None:
            return None  # the run has no stops
        entry = self.position.avg_entry
        if self.exits is None:
            # A stop's exit was too small to sell: placed afresh, as a bot with no levels.
            placed = levels_at_entry(self.config.stops, entry, levels.atr)
        else:
            placed = reentry(self.exits, self.config.stops, entry)
        return None if isinstance(placed, NoLevels) else placed

    def close(self, candle: Candle) -> None:
        """Sell the whole position at `candle`'s open: an auto-switch's `close_first`."""
        if self.position is None:
            return
        price = fill_price("sell", candle.open, self.config.slippage_rate)
        amount = self.position.amount
        too_small = below_minimum(amount, price, self.config.market_rules)
        if too_small is not None:
            reason = f"switch: {too_small}"
            self.skipped.append(SkippedOrder(signal_ts=candle.ts, side="sell", reason=reason))
            return
        self.exits = None
        self.opened_by = None
        self._fill(candle.ts, "sell", price, amount, exit="switch")

    def watch(self, candle: Candle) -> None:
        """After `candle`'s open: sell if it hits a stop, or else let the trailing stop follow."""
        if self.position is None or self.exits is None:
            return
        hit = exit_on_candle(self.exits, candle)
        if hit is None:
            self.exits = after_candle(self.exits, self.config.stops, candle)
            return
        price = fill_price("sell", hit.price, self.config.slippage_rate)
        amount = self.position.amount
        self.exits = None
        too_small = below_minimum(amount, price, self.config.market_rules)
        if too_small is not None:
            # It can't be sold, by a stop or otherwise; this is the one time it's said.
            reason = f"{hit.kind}: {too_small}"
            self.skipped.append(SkippedOrder(signal_ts=candle.ts, side="sell", reason=reason))
            return
        self.opened_by = None
        self._fill(candle.ts, "sell", price, amount, exit=hit.kind)

    def _fill(
        self,
        ts: datetime,
        side: OrderSide,
        price: Decimal,
        amount: Decimal,
        exit: FillExit | None = None,
        decided_by: ConfigRef | None = None,
    ) -> None:
        paid = fee(price, amount, self.config.fee_rate)
        position = self.position
        if side == "buy":
            self.cash -= price * amount + paid
            # Each buy is one fill here, so the last buy paid its price (D58).
            if position is None:
                self.position = Position(amount=amount, avg_entry=price, last_entry=price)
            else:
                # Averaged as the paper ledger does (brokers/ledger.py), fees left out.
                total = position.amount + amount
                entry = (position.avg_entry * position.amount + price * amount) / total
                self.position = Position(amount=total, avg_entry=entry, last_entry=price)
        else:
            assert position is not None  # callers sell only what's held
            self.cash += price * amount - paid
            left = position.amount - amount
            self.position = None if left == 0 else position.model_copy(update={"amount": left})
        self.total_fees += paid
        fill = Fill(
            ts=ts,
            side=side,
            price=price,
            amount=amount,
            fee=paid,
            cash_after=self.cash,
            exit=exit,
            decided_by=decided_by,
        )
        self.fills.append(fill)
        log.debug("backtest.fill", **fill.model_dump(mode="json"))


def run_backtest[P: StrategyParams](
    strategy: Strategy[P],
    candles: Sequence[Candle],
    config: BacktestConfig,
    *,
    switcher: AutoSwitcher | None = None,
) -> BacktestResult:
    """Run `strategy` over `candles`: its warm-up candles, then the range in `config`.

    `candles` must be contiguous (no gaps), with at least `warmup_candles` before
    `config.start` and the last one opening one step before `config.end`. With
    `config.auto_switch`, `strategy` is the config the run starts on, and the switcher
    is built from the settings unless given.
    """
    _check_strategy(strategy, config)
    auto = _switcher(config, switcher)
    first = _first_in_range(candles, config, warmup_needed(strategy, config, auto))
    atr_window = _atr_window(config)
    step = STEP[config.timeframe]
    last = len(candles) - 1
    account = _Account(config=config, cash=config.initial_cash)
    signals: list[Signal] = []
    equity_curve: list[EquityPoint] = []
    switches: list[BacktestSwitch] = []
    active: Strategy[Any] = strategy
    # The signal to fill at the next open, and whose it is.
    pending: tuple[Signal, Strategy[Any]] | None = None
    close_pending = False  # a `close_first` switch sells at the next open

    def on_candle(decider: Strategy[Any], i: int) -> Signal:
        # Only the last `warmup_candles`: all on_candle uses (D28), and it keeps a long
        # run from copying an ever-growing history on every candle.
        window = decider.warmup_candles
        return decider.on_candle(candles[max(0, i + 1 - window) : i + 1], account.position)

    # Starts on the last warm-up candle: its signal fills at the first open in range.
    for i in range(first - 1, len(candles)):
        candle = candles[i]
        if i >= first:
            if pending is not None:
                # Up to the signal's candle (i - 1): a live bot doesn't know candle i yet.
                history = candles[max(0, i - atr_window) : i] if atr_window else ()
                signal_, decider_ = pending
                account.execute(signal_, candle, history, decider_, recorded=auto is not None)
                pending = None
            if close_pending:
                account.close(candle)
                close_pending = False
            account.watch(candle)  # stops, from the open on, including a buy's own candle
            equity_curve.append(
                EquityPoint(ts=candle.ts, equity=account.cash + account.amount * candle.close)
            )
        signal: Signal | None = on_candle(active, i)
        decider = active
        keeper = _keeper(auto, account, active)
        if keeper is not None and signal is not None:
            # As a live bot's _step: the position is the keeper's to sell, and only its
            # SELL is acted on until the run is flat.
            if signal.action != "hold":
                signals.append(signal)
                why = f"its position is {config_ref(keeper).label}'s to sell (switched with keep)"
                account.skip(signal, _side(signal), why)
            kept = on_candle(keeper, i)
            signal, decider = (kept, keeper) if kept.action == "sell" else (None, active)
        if signal is not None and signal.action != "hold":
            signals.append(signal)
            if i == last:
                account.skip(signal, _side(signal), "signal on the last candle: no next candle")
            else:
                pending = (signal, decider)
        if auto is None:
            continue
        check = auto.check(candles[max(0, i + 1 - auto.window) : i + 1], active, step)
        if check.switch is None:
            continue
        target = check.switch.target
        switches.append(
            BacktestSwitch(
                ts=candle.ts,
                regime=check.switch.regime,
                from_config=config_ref(active),
                to_config=config_ref(target),
                reason=check.switch.reason,
            )
        )
        log.debug("backtest.switch", **switches[-1].model_dump(mode="json"))
        if auto.settings.policy == "close_first":
            # A SELL of the whole position already does what closing would; one of part
            # of it (P6-6) leaves the rest to close.
            selling = (
                pending is not None and pending[0].action == "sell" and pending[0].size_hint is None
            )
            buying = pending is not None and pending[0].action == "buy"
            if not selling and (account.position is not None or buying):
                if i == last:
                    reason = "switch on the last candle: no next candle"
                    account.skipped.append(
                        SkippedOrder(signal_ts=candle.ts, side="sell", reason=reason)
                    )
                else:
                    close_pending = True
        active = target

    last_close = candles[-1].close
    return BacktestResult(
        signals=signals,
        fills=account.fills,
        skipped=account.skipped,
        equity_curve=equity_curve,
        final_cash=account.cash,
        final_amount=account.amount,
        final_equity=account.cash + account.amount * last_close,
        total_fees=account.total_fees,
        switches=switches,
    )


def _keeper(
    auto: AutoSwitcher | None, account: _Account, active: Strategy[Any]
) -> Strategy[Any] | None:
    """The config that still sells the open position, as a live bot works it out
    (bots/store.py's _with_keeper): under `keep`, while the buy's config isn't the one
    the run is on now. Switching back to it ends the keeper."""
    opened_by = account.opened_by
    if auto is None or auto.settings.policy != "keep" or opened_by is None:
        return None
    if account.position is None or same_config(opened_by, active):
        return None
    return opened_by


def _check_strategy[P: StrategyParams](strategy: Strategy[P], config: BacktestConfig) -> None:
    """The config is what gets stored, so it must describe the strategy that really ran."""
    if strategy.name != config.strategy:
        raise BacktestError(f"config is for {config.strategy!r}, strategy is {strategy.name!r}")
    if strategy.version != config.strategy_version:
        raise BacktestError(
            f"config is for {config.strategy} version {config.strategy_version}, "
            f"strategy is version {strategy.version}"
        )
    params = strategy.params.model_dump(mode="json")
    if params != config.params:
        raise BacktestError(f"config params {config.params} aren't the strategy's {params}")


def check_contiguous(candles: Sequence[Candle], timeframe: Timeframe) -> None:
    """Refuse an empty list, or one with a candle missing anywhere."""
    if not candles:
        raise BacktestError("no candles")
    step = STEP[timeframe]
    gaps = [(a.ts, b.ts) for a, b in pairwise(candles) if b.ts - a.ts != step]
    if gaps:
        # A fill can't jump a gap to an unrelated price later on, and missing candles
        # aren't made up (D25). Fill the gap or pick a range without it.
        listed = ", ".join(f"{a:%Y-%m-%d %H:%M} to {b:%Y-%m-%d %H:%M}" for a, b in gaps[:5])
        raise BacktestError(f"{len(gaps)} gap(s) in the candles (UTC): {listed}")


def _first_in_range(candles: Sequence[Candle], config: BacktestConfig, warmup: int) -> int:
    """The index of the candle at `config.start`, after checking the candles fit the run."""
    check_contiguous(candles, config.timeframe)
    step = STEP[config.timeframe]
    first = (config.start - candles[0].ts) // step
    needed = max(warmup, 1)
    if first < needed:
        raise BacktestError(
            f"needs {needed} warm-up candles before start ({config.start:%Y-%m-%d %H:%M} UTC), "
            f"has {max(first, 0)}"
        )
    expected_last = config.end - step
    if candles[-1].ts != expected_last:
        raise BacktestError(
            f"candles end at {candles[-1].ts:%Y-%m-%d %H:%M} UTC, but the range ends at "
            f"{config.end:%Y-%m-%d %H:%M} UTC"
        )
    return first
