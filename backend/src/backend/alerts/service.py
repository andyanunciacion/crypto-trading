"""The alerts service: Telegram messages about what the other services do (P7-1, D59).

`python -m backend.alerts`, in its own container from the API's image (compose.yaml).

It reads the `logs` stream every service mirrors its lines to (shared/logs.py, D49),
oldest first, from a cursor kept in Redis (`alerts:cursor`), so a restart carries on
where it stopped. `to_alert()` (shared/alerts.py) says which lines are alerts. Each one
is checked against `alert_settings` (read for each batch, so a toggle applies within a
second), held back if it repeats too soon (`Quieter`), written and sent. The cursor moves
past a line once it's been dealt with, after its message went: a crash in between sends
that message again, never loses it (at least once).

It must keep working when something else is broken, because that's when alerts matter:
- Database reads give up after `DB_SECONDS`. Then the last settings and bot names read
  are used (the defaults before any), and a trade is described from its log line.
- A message Telegram can't take yet is retried until it goes (telegram.py), so later
  alerts wait behind it rather than being lost.
- The daily summary is sent at 08:00 Manila about the day before, once (`alerts:summary:
  last` holds the day it was last sent for), and tried again a minute later if it fails.

The engine's heartbeat is watched beside it too (watch.py, P7-3), and its changes are
sent through `send_engine()` with the same toggles and sender.

The chat's commands (commands.py, P7-2) run beside this loop. A kill sent from there is
in `shown_kills`: its result was the reply, so its `kill_switch` alert isn't sent again.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Container
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, TypeVar, cast

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.alerts.format import render, render_engine, render_summary
from backend.alerts.status import AlertsStatus, write_status
from backend.alerts.summary import read_summary
from backend.alerts.telegram import Sender
from backend.alerts.trades import TradeFacts, read_trade_facts
from backend.alerts.watch import Change
from shared.account import MANILA
from shared.alerts import EVENTS, Alert, AlertEvent, Quieter, to_alert
from shared.backoff import Backoff
from shared.commands import BLOCK_SECONDS
from shared.db.models import AlertSettingRow, BotRow
from shared.logs import LOGS_KEY, read_entry

CURSOR_KEY = "alerts:cursor"
SUMMARY_KEY = "alerts:summary:last"
SUMMARY_AT = time(8, 0)  # Manila
SUMMARY_RETRY = timedelta(minutes=1)
BATCH = 100  # stream entries read at once
DB_SECONDS = 5.0  # a database read gives up after this
# The engine logs a fill before its transaction commits, so the rows can be a moment
# behind the line: a trade's rows are read again until they show what the line says.
TRADE_READS = 5
TRADE_READ_PAUSE = 1.0
HEARTBEAT_SECONDS = 10
# The Docker health check reads this file's modification time (see compose.yaml).
HEARTBEAT_FILE = Path("/tmp/alerts-heartbeat")

log = structlog.get_logger()

T = TypeVar("T")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _alert(stream_id: str, raw: bytes) -> Alert | None:
    entry = read_entry(stream_id, raw)
    return None if entry is None else to_alert(entry)


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


class AlertsService:
    def __init__(
        self,
        redis: Redis,
        db: AsyncEngine,
        sender: Sender,
        *,
        mode: Literal["paper", "live"],
        clock: Callable[[], datetime] = _utcnow,
        backoff: Backoff | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        trade_read_pause: float = TRADE_READ_PAUSE,
        shown_kills: Container[str] = frozenset(),
    ) -> None:
        self._redis = redis
        self._db = db
        self._sender = sender
        self._mode = mode
        self._clock = clock
        self._backoff = backoff or Backoff()
        self._sleep = sleep
        self._trade_read_pause = trade_read_pause
        # Kills sent from Telegram, whose result the chat got as the reply (commands.py).
        self._shown_kills = shown_kills
        self._quieter = Quieter()
        self._enabled: dict[AlertEvent, bool] = {e: info.default for e, info in EVENTS.items()}
        self._names: dict[int, str] = {}
        self._summary_sent: str | None = None  # the day (ISO) last summarised; None: unread
        self._summary_retry_at: datetime | None = None
        self._db_failing = False
        self.cursor: str | None = None  # the last stream id dealt with

    # --- The loop ---------------------------------------------------------------------

    async def run(self) -> None:
        """Send alerts as lines come, until cancelled."""
        log.info("alerts.listening", key=LOGS_KEY, configured=self._sender.configured)
        while True:
            try:
                if self.cursor is None:
                    self.cursor = await self._start_cursor()
                await self.summary_if_due()
                await self.poll()
                self._backoff.reset()
            except Exception as exc:  # cancelling raises CancelledError, which isn't caught
                delay = self._backoff.next_delay()
                log.warning(
                    "alerts.reconnecting",
                    attempt=self._backoff.attempts,
                    delay_s=round(delay, 2),
                    error=type(exc).__name__,
                    detail=str(exc),
                )
                await self._sleep(delay)

    async def _start_cursor(self) -> str:
        """Where the last run stopped; on a first start, the stream's end (nothing old)."""
        saved = await self._redis.get(CURSOR_KEY)
        if saved is not None:
            return cast(bytes, saved).decode()
        newest = cast(list[tuple[bytes, object]], await self._redis.xrevrange(LOGS_KEY, count=1))
        cursor = newest[0][0].decode() if newest else "0-0"
        await self._redis.set(CURSOR_KEY, cursor)
        log.info("alerts.cursor_started", cursor=cursor)
        return cursor

    async def poll(self, *, block_ms: int = int(BLOCK_SECONDS * 1000)) -> int:
        """Deal with the next batch of lines (waiting up to `block_ms` for one); returns
        how many messages went."""
        if self.cursor is None:
            self.cursor = await self._start_cursor()
        read = cast(
            list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]],
            await self._redis.xread({LOGS_KEY: self.cursor}, count=BATCH, block=block_ms),
        )
        if not read:
            return 0
        lines = [(raw_id.decode(), fields.get(b"json", b"")) for raw_id, fields in read[0][1]]
        alerts = [(stream_id, _alert(stream_id, raw)) for stream_id, raw in lines]
        # Only a batch with an alert in it reads the settings: most lines aren't one, and
        # with the database down its own warnings would come back and read again.
        if any(alert is not None for _, alert in alerts):
            await self._refresh()
        sent = 0
        for stream_id, alert in alerts:
            if alert is not None and await self._handle(stream_id, alert):
                sent += 1
                await self._save_cursor(stream_id)
        await self._save_cursor(lines[-1][0])
        return sent

    async def _save_cursor(self, stream_id: str) -> None:
        self.cursor = stream_id
        await self._redis.set(CURSOR_KEY, stream_id)

    # --- One line ---------------------------------------------------------------------

    async def _handle(self, stream_id: str, alert: Alert) -> bool:
        """Send `alert` if it's on and not held back; True if a message was sent (or
        refused by Telegram for good)."""
        if not self._enabled.get(alert.event, False):
            return False
        entry = alert.line
        if alert.event == "kill_switch" and entry.fields.get("id") in self._shown_kills:
            log.info("alerts.kill_shown", line=stream_id, id=entry.fields.get("id"))
            return False  # the reply to /kill said it already
        passed = self._quieter.check(alert, entry.ts or self._clock())
        if passed is None:
            return False  # held back; the next one sent says how many
        trade = await self._trade(alert) if alert.event == "trade" else None
        text = render(alert, names=self._names, passed=passed, trade=trade)
        delivered = await self._sender.send(text)
        log.info(
            "alerts.sent" if delivered else "alerts.dropped",
            alert=alert.event,
            line=stream_id,
            line_event=entry.event,
            bot_id=entry.bot_id,
        )
        await self.write_status()
        return True

    async def _trade(self, alert: Alert) -> TradeFacts | None:
        """The order's stored facts, once they show the line's fill; None if they can't be
        read (the message then says what the line does)."""
        client_order_id = alert.line.fields.get("client_order_id")
        if not isinstance(client_order_id, str):
            return None
        filled = _decimal(alert.line.fields.get("filled"))
        facts = None
        for attempt in range(TRADE_READS):
            facts = await self._db_read(
                read_trade_facts(self._db, client_order_id), "trade", default=None
            )
            if facts is not None and (filled is None or facts.filled >= filled):
                return facts
            if attempt + 1 < TRADE_READS:
                await self._sleep(self._trade_read_pause)
        return facts

    # --- Settings and names, read for each batch --------------------------------------

    async def _refresh(self) -> None:
        enabled = await self._db_read(self._read_enabled(), "alert_settings", default=None)
        if enabled is not None:
            self._enabled = enabled
        names = await self._db_read(self._read_names(), "bot_names", default=None)
        if names is not None:
            self._names = names

    async def _read_enabled(self) -> dict[AlertEvent, bool]:
        async with self._db.connect() as conn:
            rows = await conn.execute(select(AlertSettingRow.event, AlertSettingRow.enabled))
            stored = {cast(AlertEvent, row.event): row.enabled for row in rows}
        return {**{e: info.default for e, info in EVENTS.items()}, **stored}

    async def _read_names(self) -> dict[int, str]:
        async with self._db.connect() as conn:
            return {row.id: row.name for row in await conn.execute(select(BotRow.id, BotRow.name))}

    async def _db_read(self, read: Awaitable[T], what: str, *, default: T) -> T:
        """`read`, or `default` if it fails; said once per outage, and when it's over."""
        try:
            async with asyncio.timeout(DB_SECONDS):
                result = await read
        except Exception as exc:
            if not self._db_failing:
                log.warning(
                    "alerts.db_failed", read=what, error=type(exc).__name__, detail=str(exc)
                )
            self._db_failing = True
            return default
        if self._db_failing:
            log.info("alerts.db_recovered")
        self._db_failing = False
        return result

    # --- The daily summary ------------------------------------------------------------

    async def summary_if_due(self) -> bool:
        """Send yesterday's summary if it's past 08:00 in Manila and not sent yet today;
        True if it went (or was due but is turned off)."""
        now = self._clock()
        local = now.astimezone(MANILA)
        if local.time() < SUMMARY_AT:
            return False
        if self._summary_retry_at is not None and now < self._summary_retry_at:
            return False
        today = local.date().isoformat()
        if self._summary_sent is None:
            saved = await self._redis.get(SUMMARY_KEY)
            self._summary_sent = "" if saved is None else cast(bytes, saved).decode()
        if self._summary_sent == today:
            return False
        await self._refresh()
        if self._enabled.get("daily_summary", False):
            try:
                await self.send_summary(now)
            except Exception as exc:
                self._summary_retry_at = now + SUMMARY_RETRY
                log.warning("alerts.summary_failed", error=type(exc).__name__, detail=str(exc))
                return False
        self._summary_retry_at = None
        self._summary_sent = today
        await self._redis.set(SUMMARY_KEY, today)
        return True

    async def send_summary(self, now: datetime) -> bool:
        async with asyncio.timeout(DB_SECONDS * 2):
            summary = await read_summary(self._db, self._redis, mode=self._mode, now=now)
        delivered = await self._sender.send(render_summary(summary))
        log.info("alerts.summary_sent" if delivered else "alerts.dropped", day=str(summary.day))
        await self.write_status()
        return delivered

    # --- The engine's heartbeat (watch.py) --------------------------------------------

    async def send_engine(self, change: Change) -> bool:
        """The message for `change` if `engine_down` is on; True if it was sent (or
        refused by Telegram for good)."""
        await self._refresh()
        if not self._enabled.get("engine_down", False):
            log.info("alerts.engine_muted", change=type(change).__name__)
            return False
        delivered = await self._sender.send(render_engine(change, self._clock()))
        log.info(
            "alerts.sent" if delivered else "alerts.dropped",
            alert="engine_down",
            change=type(change).__name__,
        )
        await self.write_status()
        return True

    # --- Status -----------------------------------------------------------------------

    async def write_status(self) -> None:
        with contextlib.suppress(Exception):  # the Settings page can do without it
            await write_status(
                self._redis,
                AlertsStatus(
                    ts=self._clock(),
                    configured=self._sender.configured,
                    last_sent_at=self._sender.last_sent_at,
                    last_error=self._sender.last_error,
                ),
            )


async def beat(service: AlertsService, stop: asyncio.Event) -> None:
    """Touch the file Docker's health check watches, and refresh the status."""
    while not stop.is_set():
        await asyncio.to_thread(HEARTBEAT_FILE.touch)  # file I/O in a thread
        await service.write_status()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
