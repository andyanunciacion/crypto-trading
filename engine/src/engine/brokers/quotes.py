"""The latest price, as the PaperBroker reads it: the `PriceUpdate` the stream keeps in Redis."""

from typing import Protocol

from redis.asyncio import Redis

from shared.prices import PriceUpdate, price_key


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
