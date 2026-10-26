"""Commands to the engine over Redis, and its replies (D43).

The engine obeys these even when the API is down: anything that can reach Redis can
send one (the engine's own CLI, the dashboard through the API; Telegram later).

A sender (`send()`) `LPUSH`es the command's JSON onto `COMMANDS_KEY`, then `BLPOP`s
`reply_key(command.id)` for the engine's answer, `BLOCK_SECONDS` at a time. The engine
pops commands one at a time (`BRPOP`, oldest first), so a command sent while it's
restarting waits in the list and is applied before any bot is loaded. It pushes the
reply once it has acted, and the reply expires after `REPLY_TTL_SECONDS` if nobody
reads it.

The only command so far is the kill switch: stop every bot, cancel every open order,
and, if asked, sell every open position.
"""

import time
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from redis.asyncio import Redis

COMMANDS_KEY = "engine:commands"
REPLY_TTL_SECONDS = 3600
# The longest one blocking pop (BRPOP/BLPOP) may wait; wait longer by looping. redis-py
# gives up on any read after its socket timeout (5 s by default), so a pop that waits
# that long fails on the client even though Redis would answer.
BLOCK_SECONDS = 1


def reply_key(command_id: UUID) -> str:
    """The Redis list the engine pushes the reply to, e.g. `engine:reply:<uuid>`."""
    return f"engine:reply:{command_id}"


class _Frozen(BaseModel):
    # Same rules as the price updates: immutable, no unknown fields, no NaN/Infinity.
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class KillCommand(_Frozen):
    """Stop every bot and cancel every open order; with `close_positions`, also sell
    every open position."""

    type: Literal["kill"] = "kill"  # so later commands can share the list
    id: UUID = Field(default_factory=uuid4)
    close_positions: bool = False
    source: str = Field(min_length=1, max_length=64)  # who sent it: "cli", "dashboard", ...
    sent_at: AwareDatetime

    def age(self, now: datetime) -> float:
        """Seconds since it was sent."""
        return (now - self.sent_at).total_seconds()


class Closing(_Frozen):
    """A position the kill switch is selling."""

    bot_id: int
    symbol: str
    amount: Decimal  # what was left to sell when the exit was placed
    # The exit placed, and what had happened to it by the reply; None while the
    # position waits for a fresh price to be sold at.
    client_order_id: str | None = None
    status: str | None = None
    filled: Decimal | None = None


class NotClosed(_Frozen):
    """A position the kill switch can't sell, and why. It's left open."""

    bot_id: int
    symbol: str
    reason: str


class KillReply(_Frozen):
    id: UUID  # the command's
    ok: bool
    error: str | None = None  # when not ok: what went wrong; the kill may be partly done
    stopped: list[int] = Field(default_factory=list)  # the bots it stopped
    canceled: list[str] = Field(default_factory=list)  # the orders it cancelled
    closing: list[Closing] = Field(default_factory=list)
    not_closed: list[NotClosed] = Field(default_factory=list)
    took_ms: int  # from receiving the command to this reply


async def send(redis: Redis, command: KillCommand, *, reply_within: float) -> KillReply | None:
    """Queue `command` for the engine and wait up to `reply_within` seconds for its reply
    (Redis does the waiting, in BLPOP, `BLOCK_SECONDS` at a time).

    None if there's none by then; the command stays queued.
    """
    await redis.lpush(COMMANDS_KEY, command.model_dump_json())
    deadline = time.monotonic() + reply_within
    while (left := deadline - time.monotonic()) > 0:
        popped = await redis.blpop([reply_key(command.id)], timeout=min(BLOCK_SECONDS, left))
        if popped is not None:
            return KillReply.model_validate_json(popped[1])
    return None
