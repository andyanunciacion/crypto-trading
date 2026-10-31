"""Paper trading for the bot runner: one PaperBroker behind one RiskManager (D35, D36, D41).

Only engine/brokers/ and engine/risk/ may name a broker (test_broker_boundary.py), so
the code that builds the PaperBroker, funds paper bots and feeds it live prices lives
here. The bot runner only ever gets `desk.risk`. Each price fed is also noted in
`desk.swings`, for the spike breaker (D42).

One broker serves every paper bot: all its state is in the database, keyed by bot.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.brokers.paper import PaperBroker, RulesSource
from engine.exchange.models import Balance
from engine.marketdata.quotes import QuoteSource
from engine.risk.account import PaperAccounts
from engine.risk.breakers import PriceSwings
from engine.risk.manager import RiskManager
from shared.backoff import Backoff
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
        # Every price the feed brings, for the spike breaker (D42).
        self.swings = PriceSwings()
        if clock is None:
            self._broker = PaperBroker(db, quotes, rules)
            accounts = PaperAccounts(db, quotes, swings=self.swings)
            self.risk = RiskManager(self._broker, accounts, rules)
        else:
            self._broker = PaperBroker(db, quotes, rules, clock=clock)
            accounts = PaperAccounts(db, quotes, clock=clock, swings=self.swings)
            self.risk = RiskManager(self._broker, accounts, rules, clock=clock)
        # The limits count every paper bot together, as one account (D41).

    async def fund(self, bot_id: int, currency: str, amount: Decimal) -> Balance:
        """Give a paper bot virtual money."""
        return await self._broker.deposit(bot_id, currency, amount)

    async def on_price(self, update: PriceUpdate) -> None:
        """Note `update` for the spike breaker, then fill the resting paper orders it reaches."""
        self.swings.observe(update)  # first, so a price that fails to fill still counts
        await self._broker.on_price(update)

    async def feed_prices(
        self,
        redis: Redis,
        exchange: str,
        symbols: Sequence[str],
        *,
        then: Callable[[PriceUpdate], Awaitable[None]] | None = None,
        backoff: Backoff | None = None,
    ) -> None:
        """Fill resting paper orders from every price update of `symbols`, until cancelled.

        Listens on the price stream's pub/sub channels (shared/prices.py). Each update
        then goes to `then` (the bot runner's stops), after its fills, so it sees the
        positions as that price left them. A bad update, or an error in `then`, is
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
                            await self._on_message(message, then)
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

    async def _on_message(
        self, message: dict[str, Any], then: Callable[[PriceUpdate], Awaitable[None]] | None
    ) -> None:
        channel = message["channel"]
        fields = {"channel": channel.decode() if isinstance(channel, bytes) else channel}
        try:
            update = PriceUpdate.model_validate_json(message["data"])
            await self.on_price(update)
        except Exception as exc:
            log.warning("paper.feed.error", **fields, error=type(exc).__name__, detail=str(exc))
            return
        if then is None:
            return
        try:
            await then(update)
        except Exception as exc:
            log.error(
                "paper.feed.listener_error", **fields, error=type(exc).__name__, detail=str(exc)
            )
