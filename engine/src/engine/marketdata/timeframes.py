"""How long each timeframe's candle lasts, and whether a candle has closed."""

from datetime import UTC, datetime, timedelta

from engine.exchange.models import Candle, Timeframe

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

STEP: dict[Timeframe, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


def floor(ts: datetime, timeframe: Timeframe) -> datetime:
    """The open time of the candle `ts` falls in (candles start on UTC multiples of their step)."""
    return ts - (ts - _EPOCH) % STEP[timeframe]


def is_closed(candle: Candle, timeframe: Timeframe, now: datetime) -> bool:
    """A candle is closed once its whole period has passed; until then its prices still move."""
    return candle.ts + STEP[timeframe] <= now
