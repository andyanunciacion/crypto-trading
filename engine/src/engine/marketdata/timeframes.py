"""How long each timeframe's candle lasts, and whether a candle has closed."""

from datetime import UTC, datetime, timedelta
from typing import get_args

from engine.exchange.models import Candle, Timeframe
from shared.candles import TIMEFRAME_STEPS

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# The steps are in shared, so the dashboard's chart can tell a gap too (D46).
STEP: dict[Timeframe, timedelta] = {tf: TIMEFRAME_STEPS[tf] for tf in get_args(Timeframe)}


def floor(ts: datetime, timeframe: Timeframe) -> datetime:
    """The open time of the candle `ts` falls in (candles start on UTC multiples of their step)."""
    return ts - (ts - _EPOCH) % STEP[timeframe]


def is_closed(candle: Candle, timeframe: Timeframe, now: datetime) -> bool:
    """A candle is closed once its whole period has passed; until then its prices still move."""
    return candle.ts + STEP[timeframe] <= now
