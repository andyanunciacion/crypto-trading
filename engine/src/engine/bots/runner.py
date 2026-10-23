"""Runs the bots: on each closed candle, each bot's signal becomes an order (ARCHITECTURE §7, D36).

Bots on the same symbol and timeframe share one live candle stream (D33). At startup
each bot's SignalRunner is seeded with its warm-up candles from REST; then, for every
closed candle, each bot in turn:

1. cancels what's left of its open orders. An order lives at most one candle, like a
   backtest signal that fills at the next open or not at all, so a stale order never
   sits on reserved cash;
2. asks its strategy for a signal, given its position (SignalRunner logs it);
3. on BUY or SELL, sizes and prices an order (orders.py) and hands it to the
   RiskManager, which decides on it and places it. The client order ID comes from the
   bot, the candle and the side, so a retried candle returns the first order (D18).

An error stays with the bot and candle it happened on: it's logged (`bot.error`) and
the bot carries on at the next candle, so one bot can't stop the others or the engine.
Each closed candle is also stored, keeping the database current, so trades can be
checked against the candles they traded on.
"""

import asyncio
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import structlog
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.backoff import Backoff
from engine.backtest.models import DEFAULT_FEE_RATE, default_slippage
from engine.bots.orders import Skip, order_for
from engine.bots.store import Bot, open_orders
from engine.brokers.base import client_order_id
from engine.exchange.models import Candle, MarketRules, OrderSide, Timeframe
from engine.marketdata.candle_stream import CandleSource, CandleStream, fetch_range
from engine.marketdata.quotes import QuoteSource
from engine.marketdata.store import CandleStore, Series
from engine.marketdata.timeframes import STEP, floor
from engine.risk.manager import RiskManager
from engine.strategies.base import Signal
from engine.strategies.runner import SignalRunner

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
        for bot in bots:
            running = _Running(bot, SignalRunner(bot.strategy, bot.symbol, bot.timeframe))
            self._series.setdefault(bot.series, []).append(running)

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
        needed = max(max(b.signals.strategy.warmup_candles, 1) for b in bots)
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

    async def on_closed(self, series: Series, candle: Candle) -> None:
        """Run every bot on `series` on `candle`, which has just closed."""
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
        for running in self._series[series]:
            try:
                await self._step(running, candle)
            except Exception as exc:
                log.error(
                    "bot.error",
                    bot_id=running.bot.id,
                    name=running.bot.name,
                    candle_ts=candle.ts.isoformat(),
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

    async def _step(self, running: _Running, candle: Candle) -> None:
        bot = running.bot
        for stale in await open_orders(self._db, bot.id):
            await self._risk.cancel(stale)
        position = await self._risk.position(bot.id, bot.symbol)
        signal = running.signals.on_closed(candle, position)
        if signal.action == "hold":
            return
        side: OrderSide = "buy" if signal.action == "buy" else "sell"
        _, quote_currency = bot.symbol.split("/")
        balances = await self._risk.balances(bot.id)
        cash = balances[quote_currency].free if quote_currency in balances else Decimal(0)
        planned = order_for(
            signal,
            position,
            cash,
            await self._quotes.latest(bot.exchange, bot.symbol),
            await self._market.market_rules(bot.symbol),
            client_order_id=client_order_id(bot.id, signal.ts, side),
            now=self._clock(),
            fee_rate=self._fee_rate,
            slippage=default_slippage(bot.symbol),
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
        outcome = await self._risk.submit(bot.id, planned, _reason(bot, signal))
        order = outcome.order
        log.info(
            "bot.order",
            **fields,
            client_order_id=planned.client_order_id,
            decision=outcome.decision.decision,
            status=None if order is None else order.status,
            filled=None if order is None else str(order.filled),
        )


def _reason(bot: Bot, signal: Signal) -> dict[str, JsonValue]:
    """What an order records about why it was placed: the signal, the bot and its config."""
    return {
        "signal": signal.model_dump(mode="json"),
        "bot": {"id": bot.id, "name": bot.name},
        "strategy_config": {
            "id": bot.strategy_config_id,
            "strategy": bot.strategy.name,
            "version": bot.strategy.version,
            "params": bot.strategy.params.model_dump(mode="json"),
        },
    }
