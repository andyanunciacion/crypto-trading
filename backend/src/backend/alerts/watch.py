"""Is the engine alive? Telegram is told when its heartbeat stops, and when it's back
(P7-3, D61).

The engine `SET`s `engine:heartbeat` every 10 s (shared/heartbeat.py), and the key never
expires, so a dead engine leaves an old beat behind. The alerts service reads it every
`WATCH_SECONDS`. The engine is down when the beat hasn't *changed* for
`ENGINE_DOWN_SECONDS`, timed on this service's own monotonic clock: the beat's `ts`
is never compared with a clock. Docker Desktop's clock can drift after the PC sleeps
(found in P7-2), and a monotonic clock doesn't run during a sleep, so waking the PC
can't look like an outage.

Redis is watched too, by the same rule. If it can't be read for as long, the engine
can't beat and no other alert can be read, so that is said instead.

The rules (`EngineWatch`, `RedisWatch`) are pure. `EngineWatcher` reads Redis, keeps the
engine's state in `alerts:engine` (so a restart of this service mid-outage neither
alerts again nor forgets to say it's back) and hands each change to the service to send.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError
from redis.asyncio import Redis

from shared.alerts import ENGINE_DOWN_SECONDS, WATCH_SECONDS
from shared.heartbeat import ENGINE_HEARTBEAT_KEY, EngineHeartbeat

STATE_KEY = "alerts:engine"

log = structlog.get_logger()


# --- What changed -----------------------------------------------------------------------


@dataclass(frozen=True)
class EngineDown:
    last: EngineHeartbeat | None  # the last beat seen; None: none since this service started
    silent_s: float  # how long it hasn't changed


@dataclass(frozen=True)
class EngineUp:
    beat: EngineHeartbeat  # the first new beat
    previous: EngineHeartbeat | None  # the last one before the outage
    announced: bool  # whether the down alert went (only then is "back up" worth saying)


@dataclass(frozen=True)
class RedisDown:
    silent_s: float
    error: str


@dataclass(frozen=True)
class RedisUp:
    down_s: float
    announced: bool


Change = EngineDown | EngineUp | RedisDown | RedisUp


# --- The rules (pure) -------------------------------------------------------------------


class WatchState(BaseModel):
    """What `alerts:engine` holds while the engine is down."""

    model_config = ConfigDict(frozen=True)

    down: bool
    announced: bool  # the down alert was sent
    last: EngineHeartbeat | None


def _identity(beat: EngineHeartbeat | None) -> tuple[object, int] | None:
    # A restarted engine counts from 1 again, so its start time is part of who it is.
    return None if beat is None else (beat.started_at, beat.beat)


class EngineWatch:
    """Down once the beat hasn't changed for `after` seconds; up at the next new beat."""

    def __init__(
        self,
        now: float,
        *,
        after: float = ENGINE_DOWN_SECONDS,
        state: WatchState | None = None,
    ) -> None:
        self._after = after
        self._changed_at = now  # when a new beat was last seen (or this watch started)
        self._started = False  # nothing observed yet
        self._last: EngineHeartbeat | None = None
        self._down = False
        self._announced = False
        if state is not None and state.down:
            # Carry on an outage the last run saw: the same beat isn't news, a new one is.
            self._started = True
            self._last = state.last
            self._down = True
            self._announced = state.announced

    @property
    def down(self) -> bool:
        return self._down

    def state(self) -> WatchState:
        return WatchState(down=self._down, announced=self._announced, last=self._last)

    def observe(self, beat: EngineHeartbeat | None, now: float) -> EngineDown | EngineUp | None:
        """What `beat` (the key's value, None if missing) says at monotonic time `now`."""
        if self._started and _identity(beat) == _identity(self._last):
            if not self._down and now - self._changed_at >= self._after:
                self._down = True
                self._announced = False
                return EngineDown(last=self._last, silent_s=now - self._changed_at)
            return None
        first = not self._started
        self._started = True
        previous, self._last = self._last, beat
        self._changed_at = now
        # The first look only says where things stand: an old beat left by an engine that
        # was already down still has to stay unchanged for a minute. A missing key (Redis
        # emptied) isn't a beat either: the minute counts from now.
        if first or beat is None or not self._down:
            return None
        self._down = False
        return EngineUp(beat=beat, previous=previous, announced=self._announced)

    def announced(self, sent: bool) -> None:
        """Whether the down alert just returned went."""
        self._announced = sent

    def give_time(self, now: float) -> None:
        """Start the minute again: Redis was unreachable, so the engine couldn't beat."""
        if not self._down:
            self._changed_at = now


class RedisWatch:
    """Down once every read for `after` seconds failed; up at the next one that works."""

    def __init__(self, *, after: float = ENGINE_DOWN_SECONDS) -> None:
        self._after = after
        self._failing_since: float | None = None
        self._down = False
        self._announced = False

    @property
    def failing(self) -> bool:
        return self._failing_since is not None

    def failed(self, now: float, error: str) -> RedisDown | None:
        if self._failing_since is None:
            self._failing_since = now
        if not self._down and now - self._failing_since >= self._after:
            self._down = True
            self._announced = False
            return RedisDown(silent_s=now - self._failing_since, error=error)
        return None

    def ok(self, now: float) -> RedisUp | None:
        since, self._failing_since = self._failing_since, None
        if not self._down or since is None:
            return None
        self._down = False
        return RedisUp(down_s=now - since, announced=self._announced)

    def announced(self, sent: bool) -> None:
        self._announced = sent


# --- The loop ---------------------------------------------------------------------------


class Announcer(Protocol):
    async def send_engine(self, change: Change) -> bool:
        """Send the message for `change` if `engine_down` is on; True if it was sent."""
        ...


class EngineWatcher:
    def __init__(
        self,
        announcer: Announcer,
        redis: Redis,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        after: float = ENGINE_DOWN_SECONDS,
    ) -> None:
        self._announcer = announcer
        self._redis = redis
        self._monotonic = monotonic
        self._after = after
        self._engine: EngineWatch | None = None  # made at the first read that works
        self._redis_watch = RedisWatch(after=after)

    async def run(self, stop: asyncio.Event, *, interval: float = WATCH_SECONDS) -> None:
        log.info("alerts.watching_engine", after_s=self._after, every_s=interval)
        while not stop.is_set():
            try:
                await self.step()
            except Exception as exc:  # a bug here mustn't take the other alerts down
                log.warning("alerts.watch_failed", error=type(exc).__name__, detail=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)

    async def step(self) -> None:
        """One look at the heartbeat."""
        now = self._monotonic()
        try:
            raw = cast(bytes | None, await self._redis.get(ENGINE_HEARTBEAT_KEY))
            engine = self._engine or await self._restore(now)
        except Exception as exc:  # cancelling raises CancelledError, which isn't caught
            down = self._redis_watch.failed(now, f"{type(exc).__name__}: {exc}")
            if down is not None:
                log.warning("alerts.redis_down", silent_s=round(down.silent_s), error=down.error)
                self._redis_watch.announced(await self._announcer.send_engine(down))
            return
        if self._redis_watch.failing:
            engine.give_time(now)
            up = self._redis_watch.ok(now)
            if up is not None:
                log.info("alerts.redis_up", down_s=round(up.down_s))
                if up.announced:
                    await self._announcer.send_engine(up)
        change = engine.observe(_beat(raw), now)
        if change is None:
            return
        if isinstance(change, EngineDown):
            log.warning(
                "alerts.engine_down",
                silent_s=round(change.silent_s),
                last_beat=None if change.last is None else change.last.ts.isoformat(),
            )
            engine.announced(await self._announcer.send_engine(change))
        else:
            log.info(
                "alerts.engine_up",
                started_at=change.beat.started_at.isoformat(),
                previous=None if change.previous is None else change.previous.ts.isoformat(),
            )
            if change.announced:
                await self._announcer.send_engine(change)
        await self._save(engine)

    async def _restore(self, now: float) -> EngineWatch:
        raw = cast(bytes | None, await self._redis.get(STATE_KEY))
        state = None
        if raw is not None:
            try:
                state = WatchState.model_validate_json(raw)
            except ValidationError:
                log.warning("alerts.engine_state_unreadable")
        if state is not None and state.down:
            log.info("alerts.engine_still_down", announced=state.announced)
        self._engine = EngineWatch(now, after=self._after, state=state)
        return self._engine

    async def _save(self, engine: EngineWatch) -> None:
        try:
            await self._redis.set(STATE_KEY, engine.state().model_dump_json())
        except Exception as exc:
            log.warning("alerts.engine_state_unsaved", error=type(exc).__name__, detail=str(exc))


def _beat(raw: bytes | None) -> EngineHeartbeat | None:
    if raw is None:
        return None
    try:
        return EngineHeartbeat.model_validate_json(raw)
    except ValidationError:
        log.warning("alerts.heartbeat_unreadable")
        return None
