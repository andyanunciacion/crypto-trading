"""Runs the bots: on each closed candle, each bot's signal becomes an order (ARCHITECTURE §7, D36).

Bots on the same symbol and timeframe share one live candle stream (D33). At startup
each bot's SignalRunner is seeded with its warm-up candles from REST; then, for every
closed candle, each bot in turn:

1. cancels what's left of its open orders. An order lives at most one candle, like a
   backtest signal that fills at the next open or not at all, so a stale order never
   sits on reserved cash;
2. asks its strategy for a signal, given its position (SignalRunner logs it);
3. on BUY or SELL, sizes and prices an order (orders.py; a buy by the bot's sizing
   mode, risk/sizing.py) and hands it to the RiskManager, which decides on it and
   places it. The client order ID comes from the
   bot, the candle and the side, so a retried candle returns the first order (D18).

An error stays with the bot and candle it happened on: it's logged (`bot.error`) and
the bot carries on at the next candle, so one bot can't stop the others or the engine.
Each closed candle is also stored, keeping the database current, so trades can be
checked against the candles they traded on.

Stops (D40, exits.py) are checked on every price, not every candle: `on_price()` runs
after the PaperBroker has filled what each price update reaches. It places a bot's
levels once its buy fills, lets a trailing stop follow the bid, and when the bid
reaches a level, cancels the bot's open orders and sells the whole position through
the RiskManager. A bot's candle and its prices take turns (one lock per bot): a price
that arrives while the bot acts on a candle skips it, and the next price checks again.
An exit that can't be placed (too small to sell, say) isn't tried again until the next
candle, so it's logged once a candle rather than on every price.

After the engine was down or offline (D38):
- At startup, `recover()` cancels every open order left from before, before any price
  can fill one, and logs each bot's position and balances as the database has them.
  The newest closed candle only seeds the window: it may have been traded already.
- A bot acts only on a candle that closed less than one timeframe step ago. Older ones
  (all the candles missed in an outage arrive at once) just fill its window and cancel
  its leftovers (`bot.caught_up`): their signals are stale, and the price is not theirs.

The kill switch (risk/kill.py, D43) and the Bots page stop bots with `stop()`: once
whatever a bot is doing has finished, it never acts again, on a candle or a price. A
series whose bots are all stopped ends its candle stream.

The Bots page starts them with `add()` (D47), while the others run. The new bot warms
up in the background, so a slow REST call never holds up the command listener (a kill
must stay fast). On a series that's already streaming, it's seeded from REST and then,
in one step with no await in between, with the candles the series delivered since, so
it joins with a whole window. A series that isn't streaming (new, or ended) is started.

The Bots page switches a bot's strategy or params with `switch()` (P6-1, D51). The new
config warms up in the background, like a bot being added, and takes over at the bot's
next candle, never between two: strategies only act on closed candles, and so no candle
reaches its window twice. Until it's ready, the old config carries on, and each order's
`reason` names the config that decided it. What happens to an open position:
- `keep`: the config that bought it (the bot's `keeper`) still decides when to sell it,
  along with the trade's stops. The new config sees every candle but only trades once
  the bot is flat, and then the keeper is dropped.
- `close_first`: the kill switch sells it (kind `switch`). While it does, the bot only
  takes in candles, and its stops are left alone (`selling`), so nothing sells twice.
- `hand_over`: the new config's SELL sells it. The trade keeps its stops.

A bot with auto-switching on (P6-4, D54) also has its regime checked after each live
candle it acts on (regime/switcher.py). When the regime calls for another config,
`on_regime` (BotControl.auto_switch) makes the switch, as the Bots page's Edit would,
in the background: the candle loop never waits for it. The new config then warms up
and takes over at a later candle, like any switch. Until it has, no second one is sent.
"""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import structlog
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.backoff import Backoff
from engine.backtest.models import DEFAULT_FEE_RATE, default_slippage
from engine.bots.exits import (
    ExitCause,
    Held,
    Sell,
    StopPlan,
    Trail,
    exit_order_id,
    levels_json,
    load_held,
    on_bid,
    save_levels,
)
from engine.bots.orders import Skip, exit_order, order_for
from engine.bots.store import Bot, open_orders
from engine.brokers.base import client_order_id
from engine.exchange.models import Balance, Candle, MarketRules, OrderSide, Timeframe
from engine.marketdata.candle_stream import CandleSource, CandleStream, KlineHook, fetch_range
from engine.marketdata.quotes import MAX_QUOTE_AGE, QuoteSource
from engine.marketdata.store import CandleStore, Series
from engine.marketdata.timeframes import STEP, floor
from engine.regime.switcher import RegimeCheck
from engine.risk.manager import RiskManager
from engine.risk.sizing import SIZING, atr_for, atr_of, lookback
from engine.risk.stops import NO_STOPS, ExitState, NoLevels, levels_at_entry, reentry
from engine.risk.stops import lookback as stops_lookback
from engine.strategies.base import Signal
from engine.strategies.runner import SignalRunner
from shared.prices import PriceUpdate

log = structlog.get_logger()

# Closed candles of a series after the given open time, as CandleStream.closed() yields them.
StreamFactory = Callable[[str, Timeframe, datetime], AsyncGenerator[Candle]]
# A series' newest candles, for a bot joining it: enough for the seconds its warm-up takes.
RECENT_CANDLES = 5
# Makes the switch an auto-switching bot's regime calls for (BotControl.auto_switch).
RegimeHook = Callable[[Bot, RegimeCheck], Awaitable[None]]


class MarketSource(CandleSource, Protocol):
    """The part of ExchangeAdapter the runner uses: public market data only."""

    async def market_rules(self, symbol: str) -> MarketRules: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Keeper:
    """The config that bought the open position, and its signals: it sells it (`keep`)."""

    strategy_config_id: int
    signals: SignalRunner[Any]


@dataclass
class _Running:
    bot: Bot
    signals: SignalRunner[Any]
    keeper: _Keeper | None = None  # dropped once the bot is flat


@dataclass(frozen=True)
class _Switch:
    """A switch, warmed up: the bot's new config and the candles to seed it with."""

    running: _Running  # not seeded yet
    history: list[Candle]


class BotRunner:
    def __init__(
        self,
        bots: Sequence[Bot],
        risk: RiskManager,
        market: MarketSource,
        quotes: QuoteSource,
        candles: CandleStore,
        db: AsyncEngine,
        *,
        stream: StreamFactory | None = None,
        on_kline: KlineHook | None = None,
        on_new_symbol: Callable[[str], None] | None = None,
        selling: Callable[[int], bool] | None = None,
        on_regime: RegimeHook | None = None,
        clock: Callable[[], datetime] = _utcnow,
        fee_rate: Decimal = DEFAULT_FEE_RATE,
    ) -> None:
        self._risk = risk
        self._market = market
        self._quotes = quotes
        self._candles = candles
        self._db = db
        self._stream = stream or self._live_stream
        self._on_kline = on_kline  # the forming candle, for the dashboard's chart (D46)
        # Told each symbol a bot added later trades, so its prices get streamed.
        self._on_new_symbol = on_new_symbol
        # Whether the kill switch is selling a bot's position for it (a `close_first`
        # switch, D51): the bot is held back until it's gone.
        self._selling = selling or (lambda bot_id: False)
        self._on_regime = on_regime
        self._clock = clock
        self._fee_rate = fee_rate  # the fee the PaperBroker charges, left room for when buying
        self._series: dict[Series, list[_Running]] = {}
        self._watched: dict[str, list[Bot]] = {}  # bots with stops, by symbol
        self._locks: dict[int, asyncio.Lock] = {}  # a bot's candle and prices take turns
        self._exit_skipped: dict[int, str] = {}  # bots whose exit can't be placed this candle
        self._stopped: set[int] = set()  # bots stopped: they never act again (until added)
        self._tasks: asyncio.TaskGroup | None = None  # set while run() runs
        self._live: set[Series] = set()  # series with a stream task
        self._warm: set[Series] = set()  # ... that has warmed up its bots
        self._recent: dict[Series, deque[Candle]] = {}  # each series' newest closed candles
        self._joining: dict[int, asyncio.Task[None]] = {}  # bots added, still warming up
        self._switching: dict[int, asyncio.Task[None]] = {}  # switches still warming up
        self._switches: dict[int, _Switch] = {}  # ... and warmed up, for the next candle
        self._regime_switching: dict[int, asyncio.Task[None]] = {}  # on_regime, still running
        for bot in bots:
            self._enlist(_running(bot))
            self._locks[bot.id] = asyncio.Lock()

    @property
    def running(self) -> list[int]:
        """The bots it runs, warmed up: not stopped, not still joining."""
        return sorted(
            r.bot.id
            for series, bots in self._series.items()
            if series in self._warm
            for r in bots
            if r.bot.id not in self._stopped
        )

    @property
    def switching(self) -> list[int]:
        """The bots whose switch (D51) is still being made by the regime (P6-4) or warming
        up: their old config still acts."""
        return sorted({*self._switching, *self._regime_switching})

    async def run(self) -> None:
        """Run every bot until cancelled. With no bots, it waits for `add()`."""
        async with asyncio.TaskGroup() as tasks:
            self._tasks = tasks
            try:
                for series in list(self._series):
                    self._start_series(series)
                await asyncio.Event().wait()  # only cancelling ends it
            finally:
                self._tasks = None

    def add(self, bot: Bot) -> bool:
        """Start running `bot`, while the others run (D47). False if it runs already.

        It warms up in the background (module docstring), and only acts once that's
        done. Stopped before then, it never joins. Call it while `run()` runs.
        """
        if bot.id in self._joining or self._runs(bot.id):
            return False
        if self._tasks is None:
            raise RuntimeError("add() needs run() to be running")
        self._forget(bot.id)  # an earlier run of it, stopped, perhaps with other settings
        self._drop_switch(bot.id)
        self._stopped.discard(bot.id)
        self._exit_skipped.pop(bot.id, None)
        self._locks.setdefault(bot.id, asyncio.Lock())
        if self._on_new_symbol is not None:
            self._on_new_symbol(bot.symbol)  # first: its first order needs a fresh price
        self._joining[bot.id] = self._tasks.create_task(self._join(bot))
        log.info("bot.starting", bot_id=bot.id, name=bot.name, series=str(bot.series))
        return True

    def starting(self, bot_id: int) -> bool:
        """Whether bot `bot_id` was added and is still warming up."""
        return bot_id in self._joining

    def switch(self, bot: Bot) -> bool:
        """Run `bot`, a bot this runner runs, with its new strategy config (D51).

        The new config (and its keeper, if any) warms up in the background, then takes
        over at the bot's next candle (module docstring). A switch still warming up is
        replaced. False if the runner doesn't run the bot (stopped, or still starting):
        then a later `add()` or a restart loads it as it is.
        """
        if bot.id in self._joining or not self._runs(bot.id):
            return False
        if self._tasks is None:
            raise RuntimeError("switch() needs run() to be running")
        self._drop_switch(bot.id)
        self._switching[bot.id] = self._tasks.create_task(self._warm_switch(bot))
        log.info(
            "bot.switch_warming_up",
            bot_id=bot.id,
            name=bot.name,
            strategy_config_id=bot.strategy_config_id,
            keeper=None if bot.keeper is None else bot.keeper.strategy_config_id,
        )
        return True

    async def warm_up(self, series: Series) -> datetime:
        """Seed the bots on `series` with their warm-up candles from REST.

        Returns the open time of the newest one: the live stream carries on after it.
        """
        # A copy: a bot that joins while this waits on REST seeds itself (_join).
        bots = list(self._series[series])
        history = await self._history(series, bots)
        for running in bots:
            _seed(running, history)
        # REST may not have the newest candle yet; then the stream fetches it first.
        last = history[-1].ts if history else self._last_closed(series)
        log.info(
            "bots.warmed_up",
            series=str(series),
            candles=len(history),
            last=last.isoformat(),
            bot_ids=[b.bot.id for b in bots],
        )
        return last

    async def recover(self) -> None:
        """Cancel every open order left from before the engine started; log each bot's state.

        Call it before prices are fed to the broker, so none of those orders fills first.
        Orders of stopped bots are cancelled too: nothing else ever would.
        """
        canceled: dict[int, list[str]] = {}
        for cid in await open_orders(self._db, mode=self._risk.mode):
            order = await self._risk.cancel(cid)
            canceled.setdefault(order.bot_id, []).append(cid)
        for running in (r for bots in self._series.values() for r in bots):
            bot = running.bot
            position = await self._risk.position(bot.id, bot.symbol)
            balances = await self._risk.balances(bot.id)
            held = None
            if position is not None and bot.stops != NO_STOPS:
                held = await load_held(self._db, bot.id, bot.symbol, mode=self._risk.mode)
            log.info(
                "bot.recovered",
                bot_id=bot.id,
                name=bot.name,
                symbol=bot.symbol,
                position=None if position is None else position.model_dump(mode="json"),
                # Fixed-point, as psql shows them: str() writes a stored 0 as "0E-13".
                balances={
                    c: {"free": f"{b.free:f}", "used": f"{b.used:f}"} for c, b in balances.items()
                },
                # Its stops carry on from these (D40); None when flat or without stops.
                levels=None if held is None else levels_json(held.levels),
                # The config still selling its position after a switch (keep, D51).
                keeper=None if running.keeper is None else running.keeper.strategy_config_id,
                canceled=canceled.get(bot.id, []),
            )

    async def on_closed(self, series: Series, candle: Candle) -> None:
        """Run every bot on `series` on `candle`, which has closed.

        A candle that closed a timeframe step or more ago only catches the bots up.
        """
        try:
            await self._candles.upsert(series, [candle])
        except Exception as exc:
            # Only the record is missing; the bots can still trade on it.
            log.warning(
                "bots.candle_not_stored",
                series=str(series),
                ts=candle.ts.isoformat(),
                error=type(exc).__name__,
                detail=str(exc),
            )
        self._recent.setdefault(series, deque(maxlen=RECENT_CANDLES)).append(candle)
        step = STEP[series.timeframe]
        closed_ago = self._clock() - (candle.ts + step)
        # A copy: a bot joining meanwhile starts at the next candle (it's seeded with this one).
        for bot in [r.bot for r in self._series[series]]:
            try:
                async with self._locks[bot.id]:
                    if bot.id in self._stopped:
                        continue
                    # Looked up again: a switch may have replaced it while we waited.
                    running = self._switched(series, bot.id, candle)
                    if running is None:
                        continue
                    self._exit_skipped.pop(bot.id, None)  # a new candle tries again
                    if self._selling(bot.id):
                        self._hold(running, candle)
                    elif closed_ago < step:
                        await self._step(running, candle)
                        self._regime(running, candle)
                    else:
                        await self._catch_up(running, candle, closed_ago)
            except Exception as exc:
                log.error(
                    "bot.error",
                    bot_id=bot.id,
                    name=bot.name,
                    candle_ts=candle.ts.isoformat(),
                    error=type(exc).__name__,
                    detail=str(exc),
                )

    async def on_price(self, update: PriceUpdate) -> None:
        """Check the stops of every bot on `update.symbol` that has them (D40).

        Call it after the broker has filled what `update` reaches, so the positions are
        as that price left them. An old price, or one with no bid, does nothing.
        """
        if update.bid is None or self._clock() - update.ts > MAX_QUOTE_AGE:
            return
        for bot in list(self._watched.get(update.symbol, [])):
            lock = self._locks[bot.id]
            if bot.exchange != update.exchange or lock.locked():
                continue  # acting on its candle; the next price checks again
            try:
                async with lock:
                    if bot.id in self._stopped or self._selling(bot.id):
                        continue  # stopped, or the kill switch is selling it (D51)
                    await self._watch(bot, update, update.bid)
            except Exception as exc:
                log.error(
                    "bot.error",
                    bot_id=bot.id,
                    name=bot.name,
                    price_ts=update.ts.isoformat(),
                    error=type(exc).__name__,
                    detail=str(exc),
                )

    async def stop(self, bot_ids: Collection[int]) -> list[int]:
        """Stop the bots in `bot_ids` that this runner runs (the kill switch, D43; the
        Bots page, D47).

        Each one first finishes what it's doing (a candle, or a price), so when this
        returns, none of them is acting or will act again. A bot still warming up after
        `add()` never joins. Returns the ones it stopped.
        """
        stopping = [i for i in bot_ids if i in self._locks and i not in self._stopped]
        self._stopped.update(stopping)  # first, so nothing new starts while we wait
        for bot_id in stopping:
            self._drop_switch(bot_id)  # a restart, or Start, loads its config afresh
            async with self._locks[bot_id]:
                pass  # its candle or price is done
        if stopping:
            log.info("bots.stopped", bot_ids=stopping)
        return stopping

    # --- Internals ----------------------------------------------------------------------

    def _enlist(self, running: _Running) -> None:
        bot = running.bot
        self._series.setdefault(bot.series, []).append(running)
        if bot.stops != NO_STOPS:
            self._watched.setdefault(bot.symbol, []).append(bot)

    def _forget(self, bot_id: int) -> None:
        for bots in self._series.values():
            bots[:] = [r for r in bots if r.bot.id != bot_id]
        for watched in self._watched.values():
            watched[:] = [b for b in watched if b.id != bot_id]

    def _runs(self, bot_id: int) -> bool:
        """On a series (maybe still warming up) and not stopped."""
        enlisted = any(r.bot.id == bot_id for bots in self._series.values() for r in bots)
        return enlisted and bot_id not in self._stopped

    def _start_series(self, series: Series) -> None:
        assert self._tasks is not None  # run() is running
        # Stopped bots stay behind: their windows are old, and a restart re-adds them anew.
        bots = [r for r in self._series[series] if r.bot.id not in self._stopped]
        self._series[series] = bots
        if bots:
            self._live.add(series)
            self._tasks.create_task(self._run_series(series))

    def _last_closed(self, series: Series) -> datetime:
        return floor(self._clock(), series.timeframe) - STEP[series.timeframe]

    async def _history(self, series: Series, bots: Sequence[_Running]) -> list[Candle]:
        """The closed candles `bots` need to warm up, up to the newest, from REST (and stored)."""
        step = STEP[series.timeframe]
        last_closed = self._last_closed(series)
        needed = max(_candles_needed(b) for b in bots)
        history = await fetch_range(
            self._market,
            series.symbol,
            series.timeframe,
            last_closed - (needed - 1) * step,
            last_closed + step,
        )
        await self._candles.upsert(series, history)
        return history

    async def _join(self, bot: Bot) -> None:
        """Warm up a bot `add()` started, then put it on its series."""
        running = _running(bot)
        series = bot.series
        try:
            history: list[Candle] = []
            if series in self._live:
                history = await self._until_done(
                    "bot.warm_up_failed", series, lambda: self._history(series, [running])
                )
            if bot.id in self._stopped:
                log.info("bot.start_abandoned", bot_id=bot.id, name=bot.name, reason="stopped")
                return
            # From here to the end, no await: no candle can slip in between.
            if series in self._live:
                newest = history[-1].ts if history else None
                _seed(running, history)
                _seed(
                    running,
                    [c for c in self._recent.get(series, ()) if newest is None or c.ts > newest],
                )
                self._enlist(running)
                log.info(
                    "bot.started",
                    bot_id=bot.id,
                    name=bot.name,
                    series=str(series),
                    candles=len(running.signals.window),
                )
            else:
                # Its stream warms it up, with whichever bots are on the series.
                self._enlist(running)
                self._start_series(series)
                log.info("bot.started", bot_id=bot.id, name=bot.name, series=str(series))
        finally:
            self._joining.pop(bot.id, None)

    async def _warm_switch(self, bot: Bot) -> None:
        """Fetch what a switched bot's new config needs; it takes over at the next candle."""
        running = _running(bot)
        try:
            history = await self._until_done(
                "bot.switch_warm_up_failed",
                bot.series,
                lambda: self._history(bot.series, [running]),
            )
            self._switches[bot.id] = _Switch(running, history)
            log.info(
                "bot.switch_ready",
                bot_id=bot.id,
                name=bot.name,
                strategy_config_id=bot.strategy_config_id,
                candles=len(history),
            )
        finally:
            # Only its own: a newer switch may have replaced (and cancelled) this one.
            if self._switching.get(bot.id) is asyncio.current_task():
                del self._switching[bot.id]

    def _drop_switch(self, bot_id: int) -> None:
        """Forget a switch of `bot_id` that hasn't taken over yet."""
        task = self._switching.pop(bot_id, None)
        if task is not None:
            task.cancel()
        self._switches.pop(bot_id, None)

    def _switched(self, series: Series, bot_id: int, candle: Candle) -> _Running | None:
        """The bot as it runs `candle`: switched first if its new config is ready.

        No await: nothing can change the series or the bot's window meanwhile.
        """
        bots = self._series[series]
        index = next((i for i, r in enumerate(bots) if r.bot.id == bot_id), None)
        if index is None:
            return None
        old = bots[index]
        switch = self._switches.pop(bot_id, None)
        if switch is None:
            if bot_id in self._switching:
                log.info(
                    "bot.switch_pending",
                    bot_id=bot_id,
                    name=old.bot.name,
                    candle_ts=candle.ts.isoformat(),
                    acting=old.bot.strategy_config_id,
                )
            return old
        new = switch.running
        # Up to the candle before this one: REST may already have had this one, and the
        # loop gives it to the bot next.
        before = sorted(
            {
                c.ts: c
                for c in [*switch.history, *self._recent.get(series, ())]
                if c.ts < candle.ts
            }.values(),
            key=lambda c: c.ts,
        )
        _seed(new, before)
        bots[index] = new
        for watched in self._watched.values():
            watched[:] = [b for b in watched if b.id != bot_id]
        if new.bot.stops != NO_STOPS:
            self._watched.setdefault(new.bot.symbol, []).append(new.bot)
        log.info(
            "bot.switched",
            bot_id=bot_id,
            name=new.bot.name,
            candle_ts=candle.ts.isoformat(),
            old=_config_json(old.bot.strategy_config_id, old.signals),
            new=_config_json(new.bot.strategy_config_id, new.signals),
            keeper=None
            if new.keeper is None
            else _config_json(new.keeper.strategy_config_id, new.keeper.signals),
            candles=len(new.signals.window),
        )
        return new

    async def _run_series(self, series: Series) -> None:
        last = await self._until_done("bots.warm_up_failed", series, lambda: self.warm_up(series))
        self._warm.add(series)
        async with aclosing(self._stream(series.symbol, series.timeframe, last)) as closed:
            async for candle in closed:
                await self.on_closed(series, candle)
                if all(r.bot.id in self._stopped for r in self._series[series]):
                    log.info("bots.series_ended", series=str(series), reason="every bot stopped")
                    # Now, before the stream closes (an await): a bot added from here on
                    # starts the series anew, and this task mustn't undo that.
                    self._live.discard(series)
                    self._warm.discard(series)
                    return

    async def _until_done[T](
        self, event: str, series: Series, attempt: Callable[[], Awaitable[T]]
    ) -> T:
        # The adapter already retried network errors; this covers a longer outage.
        backoff = Backoff()
        while True:
            try:
                return await attempt()
            except Exception as exc:
                delay = backoff.next_delay()
                log.warning(
                    event,
                    series=str(series),
                    attempt=backoff.attempts,
                    delay_s=round(delay, 2),
                    error=type(exc).__name__,
                    detail=str(exc),
                )
                await asyncio.sleep(delay)

    def _live_stream(
        self, symbol: str, timeframe: Timeframe, last_closed: datetime
    ) -> AsyncGenerator[Candle]:
        return CandleStream(
            self._market, symbol, timeframe, last_closed, on_kline=self._on_kline
        ).closed()

    async def _cancel_leftovers(self, bot: Bot) -> list[str]:
        canceled = await open_orders(self._db, bot.id, mode=self._risk.mode)
        for stale in canceled:
            await self._risk.cancel(stale)
        return canceled

    async def _catch_up(self, running: _Running, candle: Candle, closed_ago: timedelta) -> None:
        """A candle missed while the engine was down or offline: into the window, no signal."""
        canceled = await self._cancel_leftovers(running.bot)
        _seed(running, [candle])
        log.info(
            "bot.caught_up",
            bot_id=running.bot.id,
            name=running.bot.name,
            candle_ts=candle.ts.isoformat(),
            closed_ago_s=round(closed_ago.total_seconds()),
            canceled=canceled,
        )

    def _hold(self, running: _Running, candle: Candle) -> None:
        """While the kill switch sells the bot's position (a `close_first` switch): into
        the window, no signal, and its orders are left alone: one of them is that exit."""
        _seed(running, [candle])
        log.info(
            "bot.waiting",
            bot_id=running.bot.id,
            name=running.bot.name,
            candle_ts=candle.ts.isoformat(),
            reason="its position is being sold before its new strategy trades",
        )

    async def _step(self, running: _Running, candle: Candle) -> None:
        bot = running.bot
        await self._cancel_leftovers(bot)
        position = await self._risk.position(bot.id, bot.symbol)
        keeper = running.keeper
        if keeper is not None and position is None:
            # Its trade is over: from now on the bot's own config decides everything.
            running.keeper = keeper = None
            log.info(
                "bot.keeper_done",
                bot_id=bot.id,
                name=bot.name,
                candle_ts=candle.ts.isoformat(),
                strategy_config_id=bot.strategy_config_id,
            )
        signal = running.signals.on_closed(candle, position)
        decided_by = (bot.strategy_config_id, running.signals)
        if keeper is not None:
            kept = keeper.signals.on_closed(candle, position)
            if signal.action != "hold":
                log.info(
                    "bot.skipped",
                    bot_id=bot.id,
                    name=bot.name,
                    symbol=bot.symbol,
                    signal_ts=signal.ts.isoformat(),
                    side=signal.action,
                    reason=(
                        f"its position is strategy config {keeper.strategy_config_id}'s to "
                        "sell (switched with keep)"
                    ),
                )
            if kept.action != "sell":
                return  # it only sells: the bot holds, so a BUY would be skipped anyway
            signal = kept
            decided_by = (keeper.strategy_config_id, keeper.signals)
        if signal.action == "hold":
            return
        side: OrderSide = "buy" if signal.action == "buy" else "sell"
        base, quote_currency = bot.symbol.split("/")
        balances = await self._risk.balances(bot.id)
        quote = await self._quotes.latest(bot.exchange, bot.symbol)
        cash = balances[quote_currency].free if quote_currency in balances else Decimal(0)
        window = running.signals.window
        stops = None
        if bot.stops != NO_STOPS and side == "buy":
            # The ATR at the signal's candle, as a backtest places them by.
            stops_atr = (
                atr_of(window, bot.stops.atr_period, bot.timeframe) if bot.stops.uses_atr else None
            )
            stops = StopPlan(bot.stops, stops_atr)
        planned = order_for(
            signal,
            position,
            cash,
            quote,
            await self._market.market_rules(bot.symbol),
            sizing=bot.sizing,
            equity=_equity(balances, base, quote_currency, quote),
            atr=atr_for(bot.sizing, window, bot.timeframe),
            client_order_id=client_order_id(bot.id, signal.ts, side),
            now=self._clock(),
            fee_rate=self._fee_rate,
            slippage=default_slippage(bot.symbol),
            stops=bot.stops,
            stops_atr=None if stops is None else stops.atr,
        )
        fields = {
            "bot_id": bot.id,
            "name": bot.name,
            "symbol": bot.symbol,
            "signal_ts": signal.ts.isoformat(),
            "side": side,
        }
        if isinstance(planned, Skip):
            log.info("bot.skipped", **fields, reason=planned.reason)
            return
        outcome = await self._risk.submit(
            bot.id, planned.request, _reason(bot, decided_by, signal, planned.sizing, stops)
        )
        order = outcome.order
        log.info(
            "bot.order",
            **fields,
            client_order_id=planned.request.client_order_id,
            decision=outcome.decision.decision,
            amount=None if order is None else str(order.amount),  # smaller if a limit shrank it
            status=None if order is None else order.status,
            filled=None if order is None else str(order.filled),
        )
        if stops is not None and order is not None and order.filled > 0:
            # Protected from its first fill, not from the next price on.
            held = await load_held(self._db, bot.id, bot.symbol, mode=self._risk.mode)
            if held is not None and held.plan is not None:
                await self._levels(bot, held)

    def _regime(self, running: _Running, candle: Candle) -> None:
        """Check an auto-switching bot's regime at `candle`, and send the switch it calls
        for (module docstring). No await: the switch is made in the background."""
        bot = running.bot
        auto = bot.auto
        if auto is None:
            return
        check = auto.check(running.signals.window, bot.strategy, STEP[bot.timeframe])
        switch = check.switch
        fields = {
            "bot_id": bot.id,
            "name": bot.name,
            "candle_ts": candle.ts.isoformat(),
            "regime": check.reading.regime,
            "held": check.held,
            "min_hold": auto.settings.min_hold,
            "reason": check.reading.reason,
            "indicators": check.reading.indicators,
            "strategy_config_id": bot.strategy_config_id,
            "switch_to": None if switch is None else switch.target.name,
        }
        # Every candle while a regime builds up or calls for a switch; once it has held,
        # the same line every candle would only crowd the Logs page (D49).
        settled = check.held >= auto.settings.min_hold and switch is None
        (log.debug if settled else log.info)("bot.regime", **fields)
        if switch is None or self._on_regime is None or self._tasks is None:
            return
        if bot.id in self._switching or bot.id in self._switches:
            return  # a switch is warming up, or waiting for the next candle: it decides
        if bot.id in self._regime_switching:
            return  # the last candle's is still being made
        self._regime_switching[bot.id] = self._tasks.create_task(self._send_switch(bot, check))

    async def _send_switch(self, bot: Bot, check: RegimeCheck) -> None:
        assert self._on_regime is not None  # _regime checked
        try:
            await self._on_regime(bot, check)
        except Exception as exc:
            # The next candle checks again; one failure must not end the runner's tasks.
            log.error(
                "bot.auto_switch_failed",
                bot_id=bot.id,
                name=bot.name,
                candle_ts=check.reading.ts.isoformat(),
                error=type(exc).__name__,
                detail=str(exc),
            )
        finally:
            self._regime_switching.pop(bot.id, None)

    async def _watch(self, bot: Bot, update: PriceUpdate, bid: Decimal) -> None:
        held = await load_held(self._db, bot.id, bot.symbol, mode=self._risk.mode)
        if held is None or held.plan is None:
            return  # flat, or a position bought without stops
        levels = await self._levels(bot, held)
        if levels is None:
            return
        action = on_bid(levels, held.plan.stops, bid, held.exits)
        if isinstance(action, Trail):
            entry = held.position.avg_entry
            saved = await save_levels(self._db, held.position_id, entry, action.levels)
            if saved and action.levels.stop != levels.stop:
                log.info(
                    "bot.stop_raised",
                    bot_id=bot.id,
                    name=bot.name,
                    symbol=bot.symbol,
                    bid=str(bid),
                    old=str(levels.stop),
                    new=str(action.levels.stop),
                    peak=str(action.levels.peak),
                )
        elif isinstance(action, Sell):
            await self._exit(bot, held, levels, action, update)

    async def _levels(self, bot: Bot, held: Held) -> ExitState | None:
        """The position's levels, placed (again) first if its average entry moved."""
        if held.levels_current:
            return held.levels
        plan = held.plan
        assert plan is not None  # the callers checked
        entry = held.position.avg_entry
        placed = (
            levels_at_entry(plan.stops, entry, plan.atr)
            if held.levels is None
            else reentry(held.levels, plan.stops, entry)
        )
        fields = {
            "bot_id": bot.id,
            "name": bot.name,
            "symbol": bot.symbol,
            "buy": held.buy_id,
            "entry": str(entry),
            "amount": str(held.position.amount),
        }
        if isinstance(placed, NoLevels):
            # The buy checked they could be placed, so this is a bug: say so every time.
            log.error("bot.levels_failed", **fields, reason=placed.reason)
            return None
        if not await save_levels(self._db, held.position_id, entry, placed):
            return None  # another fill moved the entry meanwhile: the next price places them
        again = held.levels is not None
        log.info("bot.levels_placed", **fields, again=again, **_levels_fields(placed))
        return placed

    async def _exit(
        self, bot: Bot, held: Held, levels: ExitState, sell: Sell, update: PriceUpdate
    ) -> None:
        """Sell the whole position: a stop fired, or its last exit needs replacing."""
        fields = {
            "bot_id": bot.id,
            "name": bot.name,
            "symbol": bot.symbol,
            "kind": sell.kind,
            "again": sell.again,
            "bid": str(update.bid),
        }
        if bot.id in self._exit_skipped:
            return  # said once this candle already
        # First its other orders: a buy still filling, a resting SELL, or an exit that no
        # longer reaches the bid. Then nothing is reserved, and nothing sells twice.
        canceled = await self._cancel_leftovers(bot)
        position = await self._risk.position(bot.id, bot.symbol)  # what's left to sell
        if position is None:
            return
        attempt = len(held.exits) + 1
        planned = exit_order(
            position,
            update,
            await self._market.market_rules(bot.symbol),
            client_order_id=exit_order_id(held.buy_id, attempt),
            now=self._clock(),
            slippage=default_slippage(bot.symbol),
        )
        if isinstance(planned, Skip):
            self._exit_skipped[bot.id] = planned.reason
            log.warning("bot.exit_skipped", **fields, reason=planned.reason, canceled=canceled)
            return
        reason = _exit_reason(bot, sell.kind, update, levels, attempt)
        outcome = await self._risk.submit(bot.id, planned.request, reason)
        order = outcome.order
        if order is None or order.status == "rejected":
            # Placing it again on the next price would only add rejected orders.
            self._exit_skipped[bot.id] = "blocked" if order is None else "rejected"
        log.info(
            "bot.exit",
            **fields,
            **_levels_fields(levels),
            client_order_id=planned.request.client_order_id,
            amount=str(planned.request.amount),
            limit=str(planned.request.price),
            decision=outcome.decision.decision,
            status=None if order is None else order.status,
            filled=None if order is None else str(order.filled),
            canceled=canceled,
            quote=update.model_dump(mode="json"),
        )


def _running(bot: Bot) -> _Running:
    """`bot`, with its signal runners, not seeded yet."""
    # ATR sizing, stops or the regime may need more candles than the strategy: it keeps them.
    signals = SignalRunner(bot.strategy, bot.symbol, bot.timeframe, keep=_extra_window(bot))
    keeper = None
    if bot.keeper is not None:
        kept = SignalRunner(bot.keeper.strategy, bot.symbol, bot.timeframe)
        keeper = _Keeper(bot.keeper.strategy_config_id, kept)
    return _Running(bot, signals, keeper)


def _seed(running: _Running, candles: Sequence[Candle]) -> None:
    running.signals.seed(candles)
    if running.keeper is not None:
        running.keeper.signals.seed(candles)


def _candles_needed(running: _Running) -> int:
    """Closed candles the bot needs to warm up: its strategy's, ATR's, the regime's, its
    keeper's."""
    keeper = running.keeper
    return max(
        running.signals.strategy.warmup_candles,
        _extra_window(running.bot),
        0 if keeper is None else keeper.signals.strategy.warmup_candles,
        1,
    )


def _extra_window(bot: Bot) -> int:
    """Closed candles the bot needs besides its strategy's: for its ATR sizing or stops,
    and its auto-switching's regime readings. 0 if it needs none."""
    regime = 0 if bot.auto is None else bot.auto.window
    return max(lookback(bot.sizing), stops_lookback(bot.stops), regime)


def _config_json(strategy_config_id: int, signals: SignalRunner[Any]) -> dict[str, JsonValue]:
    """A strategy config as an order's `reason` and the logs name it."""
    strategy = signals.strategy
    return {
        "id": strategy_config_id,
        "strategy": strategy.name,
        "version": strategy.version,
        "params": strategy.params.model_dump(mode="json"),
    }


def _levels_fields(levels: ExitState) -> dict[str, JsonValue]:
    placed = levels_json(levels)
    assert placed is not None
    return placed


def _equity(
    balances: Mapping[str, Balance], base: str, quote_currency: str, quote: PriceUpdate | None
) -> Decimal:
    """What the bot is worth: its quote currency, plus its coins at the bid (what they'd
    sell for). With no bid, the coins count as nothing; the order is skipped anyway."""
    cash = balances[quote_currency].total if quote_currency in balances else Decimal(0)
    coins = balances[base].total if base in balances else Decimal(0)
    bid = None if quote is None else quote.bid
    return cash if bid is None else cash + coins * bid


def _reason(
    bot: Bot,
    decided_by: tuple[int, SignalRunner[Any]],
    signal: Signal,
    sizing: str | None,
    stops: StopPlan | None,
) -> dict[str, JsonValue]:
    """What an order records about why it was placed: the signal, the bot and the config
    whose signal it was (the bot's, or its keeper's), and for a buy how it was sized and
    the stops it gets once it fills."""
    reason: dict[str, JsonValue] = {
        "signal": signal.model_dump(mode="json"),
        "bot": {"id": bot.id, "name": bot.name},
        "strategy_config": _config_json(*decided_by),
    }
    if sizing is not None:
        reason["sizing"] = {
            "settings": SIZING.dump_python(bot.sizing, mode="json"),
            "detail": sizing,
        }
    if stops is not None:
        reason["stops"] = stops.to_json()
    return reason


def _exit_reason(
    bot: Bot, kind: ExitCause, update: PriceUpdate, levels: ExitState, attempt: int
) -> dict[str, JsonValue]:
    """What a stop's exit records: which stop, the levels and price it fired at."""
    return {
        "exit": {
            "kind": kind,
            "attempt": attempt,
            "bid": str(update.bid),
            "price_ts": update.ts.isoformat(),
            "levels": _levels_fields(levels),
        },
        **_bot_reason(bot),
    }


def _bot_reason(bot: Bot) -> dict[str, JsonValue]:
    return {
        "bot": {"id": bot.id, "name": bot.name},
        "strategy_config": {
            "id": bot.strategy_config_id,
            "strategy": bot.strategy.name,
            "version": bot.strategy.version,
            "params": bot.strategy.params.model_dump(mode="json"),
        },
    }
