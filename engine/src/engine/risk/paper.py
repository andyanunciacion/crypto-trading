"""Paper trading for the bot runner: one PaperBroker behind one RiskManager (D35, D36).

Only engine/brokers/ and engine/risk/ may name a broker (test_broker_boundary.py), so
the code that builds the PaperBroker, funds paper bots and feeds it live prices lives
here. The bot runner only ever gets `desk.risk`.

One broker serves every paper bot: all its state is in the database, keyed by bot.
"""

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.backoff import Backoff
from engine.brokers.paper import PaperBroker, RulesSource
from engine.exchange.models import Balance
from engine.marketdata.quotes import QuoteSource
from engine.risk.manager import RiskManager
from shared.prices import PriceUpdate, price_channel

log = structlog.get_logger()


class PaperDesk:
    def __init__(
        self,
        db: AsyncEngine,
        quotes: QuoteSource,
        rules: RulesSource,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if clock is None:
            self._broker = PaperBroker(db, quotes, rules)
        else:
            self._broker = PaperBroker(db, quotes, rules, clock=clock)
        self.risk = RiskManager(self._broker)

    async def fund(self, bot_id: int, currency: str, amount: Decimal) -> Balance:
        """Give a paper bot virtual money."""
        return await self._broker.deposit(bot_id, currency, amount)

    async def feed_prices(
        self,
        redis: Redis,
        exchange: str,
        symbols: Sequence[str],
        *,
        backoff: Backoff | None = None,
    ) -> None:
        """Fill resting paper orders from every price update of `symbols`, until cancelled.

        Listens on the price stream's pub/sub channels (shared/prices.py). A bad update is
        logged and skipped. If Redis goes away it subscribes again after a backoff;
        updates sent meanwhile are missed, which only delays a fill to a later one.
        """
        backoff = backoff or Backoff()
        channels = [price_channel(exchange, symbol) for symbol in symbols]
        while True:
            try:
                async with redis.pubsub() as pubsub:
                    await pubsub.subscribe(*channels)
                    log.info("paper.feed.subscribed", channels=channels)
                    backoff.reset()
                    async for message in pubsub.listen():
                        if message["type"] == "message":
                            await self._on_message(message)
            except Exception as exc:  # cancelling raises CancelledError, which isn't caught
                delay = backoff.next_delay()
                log.warning(
                    "paper.feed.reconnecting",
                    attempt=backoff.attempts,
                    delay_s=round(delay, 2),
                    error=type(exc).__name__,
                    detail=str(exc),
                )
                await asyncio.sleep(delay)

    async def _on_message(self, message: dict[str, Any]) -> None:
        try:
            update = PriceUpdate.model_validate_json(message["data"])
            await self._broker.on_price(update)
        except Exception as exc:
            channel = message["channel"]
            log.warning(
                "paper.feed.error",
                channel=channel.decode() if isinstance(channel, bytes) else channel,
                error=type(exc).__name__,
                detail=str(exc),
            )
