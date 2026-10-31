"""How the alerts service is doing, for the Settings page: one JSON value in Redis.

Written at startup, after each message and every 10 s, so its `ts` also says whether
the service is running.
"""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict
from redis.asyncio import Redis

ALERTS_STATUS_KEY = "alerts:status"


class AlertsStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: AwareDatetime  # when it was written
    configured: bool  # False: no Telegram bot set up, messages are only logged
    last_sent_at: AwareDatetime | None
    last_error: str | None  # why the last message didn't go, until one does


async def write_status(redis: Redis, status: AlertsStatus) -> None:
    await redis.set(ALERTS_STATUS_KEY, status.model_dump_json())


async def read_status(redis: Redis) -> AlertsStatus | None:
    raw = await redis.get(ALERTS_STATUS_KEY)
    return None if raw is None else AlertsStatus.model_validate_json(raw)


def is_stale(status: AlertsStatus, now: datetime, *, after_seconds: float = 30) -> bool:
    return (now - status.ts).total_seconds() > after_seconds
