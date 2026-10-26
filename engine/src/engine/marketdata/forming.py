"""Writes each series' forming candle to Redis for the dashboard's chart (D46).

`CandleStream` calls it with the newest candle of every kline update (its `on_kline`
hook). Display only: nothing here is stored or traded on (shared/candles.py).
"""

from collections.abc import Callable
from datetime import UTC, datetime

from redis.asyncio import Redis

from engine.exchange.models import Candle, Timeframe
from shared.candles import FormingCandle, forming_key


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RedisFormingSink:
    def __init__(
        self, redis: Redis, exchange: str, *, clock: Callable[[], datetime] = _utcnow
    ) -> None:
        self._redis = redis
        self._exchange = exchange
        self._clock = clock

    async def __call__(self, symbol: str, timeframe: Timeframe, candle: Candle) -> None:
        forming = FormingCandle(
            exchange=self._exchange,
            symbol=symbol,
            timeframe=timeframe,
            ts=candle.ts,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            received_at=self._clock(),
        )
        await self._redis.set(
            forming_key(self._exchange, symbol, timeframe), forming.model_dump_json()
        )
