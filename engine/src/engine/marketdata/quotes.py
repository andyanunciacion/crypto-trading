"""The latest price, as the PaperBroker and the bot runner read it: the `PriceUpdate` the
stream keeps in Redis."""

from datetime import timedelta
from typing import Protocol

from redis.asyncio import Redis

from shared.prices import PriceUpdate, price_key

# MEXC's book ticker updates many times a second on a liquid pair, and the price stream
# polls REST every 2 s while it reconnects, so a price this old means the feed is down.
MAX_QUOTE_AGE = timedelta(seconds=10)


class QuoteSource(Protocol):
    async def latest(self, exchange: str, symbol: str) -> PriceUpdate | None:
        """The newest price for `symbol`, or None if there has never been one."""
        ...


class RedisQuotes:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def latest(self, exchange: str, symbol: str) -> PriceUpdate | None:
        payload = await self._redis.get(price_key(exchange, symbol))
        return None if payload is None else PriceUpdate.model_validate_json(payload)
