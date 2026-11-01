"""structlog set up the same way for the engine, the API and every CLI: JSON lines.

The long-running services (engine, backtester, API) also mirror their lines into a
capped Redis stream, for the dashboard's Logs page (P5-6, D49). `LogMirror` copies each
INFO-or-above line into a bounded buffer as it's logged, and `ship()` sends the buffer
to Redis about twice a second, so logging never waits on Redis: with Redis down, lines
wait in the buffer (the oldest dropped once it's full) and stdout still has them all.
"""

import asyncio
import contextlib
import json
from collections import deque
from datetime import datetime
from decimal import Decimal
from itertools import count
from typing import Any, TextIO

import structlog
from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from structlog.typing import EventDict, Processor, WrappedLogger

# The stream the Logs page reads, newest last; XADD trims it to about LOGS_MAXLEN.
LOGS_KEY = "logs"
LOGS_MAXLEN = 50_000
BUFFER = 10_000  # lines held while Redis is slow or down
SHIP_SECONDS = 0.5
MIRRORED_LEVELS = frozenset({"info", "warning", "error", "critical"})
# Every 10 s from the engine: noise on the Logs page (the header shows the heartbeat).
SKIPPED_EVENTS = frozenset({"engine.heartbeat"})
OWN_EVENTS = "logs.mirror."  # the mirror's own lines: mirroring them could loop

log = structlog.get_logger()


def json_default(value: object) -> object:
    """How a log line's values become JSON (here and for Sentry, shared/errors.py)."""
    if isinstance(value, Decimal):
        return format(value, "f")  # exact, and never "1E-8"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def mirrored_line(event_dict: EventDict, service: str) -> str:
    """A log line as the mirror sends it to the stream: its JSON, with the service."""
    return json.dumps({**event_dict, "service": service}, default=json_default)


class LogMirror:
    """A structlog processor that keeps INFO-or-above lines for `ship()` to send.

    `deque.append` and `popleft` are thread-safe, so lines logged from worker threads
    (`asyncio.to_thread`) are kept too.
    """

    def __init__(
        self,
        service: str,
        *,
        key: str = LOGS_KEY,
        maxlen: int = LOGS_MAXLEN,
        buffer: int = BUFFER,
    ) -> None:
        self.service = service
        self._key = key
        self._maxlen = maxlen
        # (sequence number, line): the number says which lines a flush has sent, even
        # if newer ones pushed older ones out of the full buffer meanwhile.
        self._lines: deque[tuple[int, str]] = deque(maxlen=buffer)
        self._numbers = count()
        self._dropped = 0

    def __call__(self, logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        event = event_dict.get("event")
        if (
            event_dict.get("level") in MIRRORED_LEVELS
            and isinstance(event, str)
            and event not in SKIPPED_EVENTS
            and not event.startswith(OWN_EVENTS)
        ):
            if len(self._lines) == self._lines.maxlen:
                self._dropped += 1
            self._lines.append((next(self._numbers), mirrored_line(event_dict, self.service)))
        return event_dict

    async def flush(self, redis: Redis) -> int:
        """Send every line kept so far; returns how many. Raises if Redis fails, keeping
        the lines for the next try."""
        if self._dropped:
            dropped, self._dropped = self._dropped, 0
            log.warning("logs.mirror.dropped", count=dropped, hint="the buffer was full")
        batch = list(self._lines)
        if not batch:
            return 0
        async with redis.pipeline(transaction=False) as pipe:
            for _, line in batch:
                pipe.xadd(self._key, {"json": line}, maxlen=self._maxlen, approximate=True)
            await pipe.execute()
        last = batch[-1][0]
        while self._lines and self._lines[0][0] <= last:
            self._lines.popleft()
        return len(batch)

    async def ship(self, redis: Redis, stop: asyncio.Event, *, every: float = SHIP_SECONDS) -> None:
        """Flush every `every` seconds until `stop` is set, then once more. A Redis
        failure is logged once per outage (to stdout only) and never raised."""
        failing = False
        while True:
            stopping = stop.is_set()
            try:
                await self.flush(redis)
            except (RedisError, OSError) as exc:
                if not failing:
                    log.warning(
                        "logs.mirror.failed",
                        error=type(exc).__name__,
                        detail=str(exc),
                        waiting=len(self._lines),
                    )
                failing = True
            else:
                if failing:
                    log.info("logs.mirror.recovered")
                failing = False
            if stopping:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=every)


class LogEntry(BaseModel):
    """One mirrored line, as the Logs page shows it."""

    model_config = ConfigDict(frozen=True)

    id: str  # the stream id ("<ms>-<n>"), which orders the entries and pages them
    ts: AwareDatetime | None  # when it was logged
    service: str  # engine, backtester or api
    level: str
    event: str
    bot_id: int | None
    fields: dict[str, JsonValue]  # everything else on the line


_PARTS = frozenset({"event", "level", "timestamp", "service", "bot_id"})


def read_entry(stream_id: str, line: bytes) -> LogEntry | None:
    """A stream entry's line, or None if it doesn't read as one."""
    try:
        data: Any = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return LogEntry.model_validate(
            {
                "id": stream_id,
                "ts": data.get("timestamp"),
                "service": data.get("service", "unknown"),
                "level": data.get("level"),
                "event": data.get("event"),
                "bot_id": data.get("bot_id"),
                "fields": {k: v for k, v in data.items() if k not in _PARTS},
            }
        )
    except ValidationError:
        return None


def configure_logging(
    level: str,
    stream: TextIO | None = None,
    *,
    mirror: LogMirror | None = None,
    errors: Processor | None = None,
) -> None:
    """JSON logs to `stream` (default stdout). CLIs whose stdout is data log to stderr.
    A long-running service passes a `mirror` and runs its `ship()` (D49), and the
    `ErrorForwarder` from `shared.errors.init_errors()`, which sends error lines to
    Sentry (D62)."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            *([mirror] if mirror is not None else []),
            *([errors] if errors is not None else []),
            structlog.processors.JSONRenderer(),
        ],
        # Drops calls below LOG_LEVEL before they do any work.
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(stream),
    )
