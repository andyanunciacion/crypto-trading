"""The engine's end of the command list (shared/commands.py, D43).

Commands are popped one at a time, oldest first, and each gets a reply once it's been
carried out. At startup `drain()` applies whatever was sent while the engine was down,
before any bot is loaded, so a kill sent then still stops them. Then `run()` waits for
more until cancelled.

A command that doesn't parse is logged (`command.invalid`) and dropped: with no id,
nobody can be answered. One that fails gets a reply with `ok: false` and the error. If
Redis goes away, the listener tries again after a backoff; commands sent meanwhile wait
in the list.
"""

import asyncio
import time

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis

from engine.backoff import Backoff
from engine.risk.kill import KillSwitch
from shared.commands import (
    BLOCK_SECONDS,
    COMMANDS_KEY,
    REPLY_TTL_SECONDS,
    KillCommand,
    KillReply,
    reply_key,
)

log = structlog.get_logger()


class CommandListener:
    def __init__(
        self, redis: Redis, kill_switch: KillSwitch, *, backoff: Backoff | None = None
    ) -> None:
        self._redis = redis
        self._kill_switch = kill_switch
        self._backoff = backoff or Backoff()

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
            command = KillCommand.model_validate_json(raw)
        except ValidationError as exc:
            log.error("command.invalid", error="ValidationError", detail=str(exc))
            return
        try:
            reply = await self._kill_switch.kill(command)
        except Exception as exc:
            log.error(
                "command.error",
                id=str(command.id),
                type=command.type,
                error=type(exc).__name__,
                detail=str(exc),
            )
            reply = KillReply(
                id=command.id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                took_ms=round((time.monotonic() - started) * 1000),
            )
        await self._reply(reply)

    async def _reply(self, reply: KillReply) -> None:
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
