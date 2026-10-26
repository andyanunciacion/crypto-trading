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
"""

import asyncio
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
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
from engine.marketdata.candle_stream import CandleSource, CandleStream, fetch_range
from engine.marketdata.quotes import MAX_QUOTE_AGE, QuoteSource
from engine.marketdata.store import CandleStore, Series
from engine.marketdata.timeframes import STEP, floor
from engine.risk.manager import RiskManager
from engine.risk.sizing import SIZING, atr_for, atr_of, lookback
from engine.risk.stops import NO_STOPS, ExitKind, ExitState, NoLevels, levels_at_entry, reentry
from engine.risk.stops import lookback as stops_lookback
from engine.strategies.base import Signal
from engine.strategies.runner import SignalRunner
from shared.prices import PriceUpdate

log = structlog.get_logger()

# Closed candles of a series after the given open time, as CandleStream.closed() yields them.
StreamFactory = Callable[[str, Timeframe, datetime], AsyncGenerator[Candle]]


class MarketSource(CandleSource, Protocol):
    """The part of ExchangeAdapter the runner uses: public market data only."""

    async def market_rules(self, symbol: str) -> MarketRules: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Running:
    bot: Bot
    signals: SignalRunner[Any]


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
        clock: Callable[[], datetime] = _utcnow,
        fee_rate: Decimal = DEFAULT_FEE_RATE,
    ) -> None:
        self._risk = risk
        self._market = market
        self._quotes = quotes
        self._candles = candles
        self._db = db
        self._stream = stream or self._live_stream
        self._clock = clock
        self._fee_rate = fee_rate  # the fee the PaperBroker charges, left room for when buying
        self._series: dict[Series, list[_Running]] = {}
        self._watched: dict[str, list[Bot]] = {}  # bots with stops, by symbol
        self._locks: dict[int, asyncio.Lock] = {}  # a bot's candle and prices take turns
        self._exit_skipped: dict[int, str] = {}  # bots whose exit can't be placed this candle
        for bot in bots:
            # ATR sizing or stops may need more candles than the strategy: the runner keeps them.
            signals = SignalRunner(bot.strategy, bot.symbol, bot.timeframe, keep=_atr_window(bot))
            self._series.setdefault(bot.series, []).append(_Running(bot, signals))
            self._locks[bot.id] = asyncio.Lock()
            if bot.stops != NO_STOPS:
                self._watched.setdefault(bot.symbol, []).append(bot)

    async def run(self) -> None:
        """Run every bot until cancelled."""
        async with asyncio.TaskGroup() as tasks:
            for series in self._series:
                tasks.create_task(self._run_series(series))

    async def warm_up(self, series: Series) -> datetime:
        """Seed the bots on `series` with their warm-up candles from REST.

        Returns the open time of the newest one: the live stream carries on after it.
        """
        bots = self._series[series]
        step = STEP[series.timeframe]
        last_closed = floor(self._clock(), series.timeframe) - step
        needed = max(max(b.signals.strategy.warmup_candles, _atr_window(b.bot), 1) for b in bots)
        history = await fetch_range(
            self._market,
            series.symbol,
            series.timeframe,
            last_closed - (needed - 1) * step,
            last_closed + step,
        )
        await self._candles.upsert(series, history)
        for running in bots:
            running.signals.seed(history)
        # REST may not have the newest candle yet; then the stream fetches it first.
        last = history[-1].ts if history else last_closed
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
        step = STEP[series.timeframe]
        closed_ago = self._clock() - (candle.ts + step)
        for running in self._series[series]:
            try:
                async with self._locks[running.bot.id]:
                    self._exit_skipped.pop(running.bot.id, None)  # a new candle tries again
                    if closed_ago < step:
                        await self._step(running, candle)
                    else:
                        await self._catch_up(running, candle, closed_ago)
            except Exception as exc:
                log.error(
                    "bot.error",
                    bot_id=running.bot.id,
                    name=running.bot.name,
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
        for bot in self._watched.get(update.symbol, []):
            lock = self._locks[bot.id]
            if bot.exchange != update.exchange or lock.locked():
                continue  # acting on its candle; the next price checks again
            try:
                async with lock:
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

    # --- Internals ----------------------------------------------------------------------

    async def _run_series(self, series: Series) -> None:
        last = await self._warm_up_until_done(series)
        async with aclosing(self._stream(series.symbol, series.timeframe, last)) as closed:
            async for candle in closed:
                await self.on_closed(series, candle)

    async def _warm_up_until_done(self, series: Series) -> datetime:
        # The adapter already retried network errors; this covers a longer outage at startup.
        backoff = Backoff()
        while True:
            try:
                return await self.warm_up(series)
            except Exception as exc:
                delay = backoff.next_delay()
                log.warning(
                    "bots.warm_up_failed",
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
        return CandleStream(self._market, symbol, timeframe, last_closed).closed()

    async def _cancel_leftovers(self, bot: Bot) -> list[str]:
        canceled = await open_orders(self._db, bot.id, mode=self._risk.mode)
        for stale in canceled:
            await self._risk.cancel(stale)
        return canceled

    async def _catch_up(self, running: _Running, candle: Candle, closed_ago: timedelta) -> None:
        """A candle missed while the engine was down or offline: into the window, no signal."""
        canceled = await self._cancel_leftovers(running.bot)
        running.signals.seed([candle])
        log.info(
            "bot.caught_up",
            bot_id=running.bot.id,
            name=running.bot.name,
            candle_ts=candle.ts.isoformat(),
            closed_ago_s=round(closed_ago.total_seconds()),
            canceled=canceled,
        )

    async def _step(self, running: _Running, candle: Candle) -> None:
        bot = running.bot
        await self._cancel_leftovers(bot)
        position = await self._risk.position(bot.id, bot.symbol)
        signal = running.signals.on_closed(candle, position)
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
            bot.id, planned.request, _reason(bot, signal, planned.sizing, stops)
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


def _atr_window(bot: Bot) -> int:
    """Closed candles the bot needs for its ATR sizing or stops: 0 if neither uses ATR."""
    return max(lookback(bot.sizing), stops_lookback(bot.stops))


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
    bot: Bot, signal: Signal, sizing: str | None, stops: StopPlan | None
) -> dict[str, JsonValue]:
    """What an order records about why it was placed: the signal, the bot and its config,
    and for a buy how it was sized and the stops it gets once it fills."""
    reason: dict[str, JsonValue] = {"signal": signal.model_dump(mode="json"), **_bot_reason(bot)}
    if sizing is not None:
        reason["sizing"] = {
            "settings": SIZING.dump_python(bot.sizing, mode="json"),
            "detail": sizing,
        }
    if stops is not None:
        reason["stops"] = stops.to_json()
    return reason


def _exit_reason(
    bot: Bot, kind: ExitKind, update: PriceUpdate, levels: ExitState, attempt: int
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
