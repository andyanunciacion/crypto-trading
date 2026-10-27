"""The engine's end of the command list (shared/commands.py, D43).

Commands are popped one at a time, oldest first, and each gets a reply once it's been
carried out: the kill switch's by the KillSwitch (risk/kill.py), the Bots page's by
BotControl (control/bots.py, D47). One at a time means a kill waits at most for the
command before it, and none of them takes long: a started bot warms up in the
background. At startup `drain()` applies whatever was sent while the engine was down,
before any bot is loaded, so a kill sent then still stops them. Then `run()` waits for
more until cancelled.

A command that doesn't parse is logged (`command.invalid`) and dropped: with no id,
nobody can be answered. A bot command past its `expires_at` is dropped too
(`command.expired`), unanswered: its sender stopped waiting. One that fails gets a reply
with `ok: false` and the error. If Redis goes away, the listener tries again after a
backoff; commands sent meanwhile wait in the list.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis

from engine.backoff import Backoff
from engine.control.bots import BotControl
from engine.risk.kill import KillSwitch
from shared.commands import (
    BLOCK_SECONDS,
    COMMAND,
    COMMANDS_KEY,
    REPLY_TTL_SECONDS,
    BotReply,
    KillCommand,
    KillReply,
    reply_key,
)

log = structlog.get_logger()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CommandListener:
    def __init__(
        self,
        redis: Redis,
        kill_switch: KillSwitch,
        bots: BotControl | None = None,
        *,
        backoff: Backoff | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._redis = redis
        self._kill_switch = kill_switch
        self._bots = bots
        self._backoff = backoff or Backoff()
        self._clock = clock

    async def drain(self) -> int:
        """Carry out every command already waiting, without waiting for more. Returns how
        many there were."""
        count = 0
        while (raw := await self._redis.rpop(COMMANDS_KEY)) is not None:
            assert not isinstance(raw, list)  # RPOP returns a list only when given a count
            count += 1
            await self._handle(raw)
        if count:
            log.info("commands.drained", count=count)
        return count

    async def run(self) -> None:
        """Carry out commands as they come, until cancelled."""
        log.info("commands.listening", key=COMMANDS_KEY)
        while True:
            try:
                # Returns at once when a command comes; else asks again (BLOCK_SECONDS).
                popped = await self._redis.brpop([COMMANDS_KEY], timeout=BLOCK_SECONDS)
                self._backoff.reset()
                if popped is not None:
                    await self._handle(popped[1])
            except Exception as exc:  # cancelling raises CancelledError, which isn't caught
                delay = self._backoff.next_delay()
                log.warning(
                    "commands.reconnecting",
                    attempt=self._backoff.attempts,
                    delay_s=round(delay, 2),
                    error=type(exc).__name__,
                    detail=str(exc),
                )
                await asyncio.sleep(delay)

    async def _handle(self, raw: bytes | str) -> None:
        started = time.monotonic()
        try:
            command = COMMAND.validate_json(raw)
        except ValidationError as exc:
            log.error("command.invalid", error="ValidationError", detail=str(exc))
            return
        reply: KillReply | BotReply
        if isinstance(command, KillCommand):
            try:
                reply = await self._kill_switch.kill(command)
            except Exception as exc:
                self._failed(command.id, command.type, exc)
                reply = KillReply(
                    id=command.id, ok=False, error=_error(exc), took_ms=_ms_since(started)
                )
        else:
            now = self._clock()
            if command.expired(now):
                log.warning(
                    "command.expired",
                    id=str(command.id),
                    type=command.type,
                    source=command.source,
                    age_s=round((now - command.sent_at).total_seconds(), 3),
                )
                return
            try:
                if self._bots is None:
                    raise RuntimeError("this engine takes no bot commands")
                reply = await self._bots.handle(command)
            except Exception as exc:
                self._failed(command.id, command.type, exc)
                reply = BotReply(
                    id=command.id, ok=False, error=_error(exc), took_ms=_ms_since(started)
                )
        await self._reply(reply)

    def _failed(self, command_id: object, kind: str, exc: Exception) -> None:
        log.error(
            "command.error",
            id=str(command_id),
            type=kind,
            error=type(exc).__name__,
            detail=str(exc),
        )

    async def _reply(self, reply: KillReply | BotReply) -> None:
        key = reply_key(reply.id)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.lpush(key, reply.model_dump_json())
                pipe.expire(key, REPLY_TTL_SECONDS)  # gone in an hour if nobody reads it
                await pipe.execute()
        except Exception as exc:
            # The command was carried out; only its answer is lost (the sender times out).
            log.error(
                "command.reply_failed", id=str(reply.id), error=type(exc).__name__, detail=str(exc)
            )


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _ms_since(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
