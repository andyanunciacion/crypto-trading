"""Commands to the engine over Redis, and its replies (D43).

The engine obeys these even when the API is down: anything that can reach Redis can
send one (the engine's own CLI, the dashboard through the API; Telegram later).

A sender (`send()`) `LPUSH`es the command's JSON onto `COMMANDS_KEY`, then `BLPOP`s
`reply_key(command.id)` for the engine's answer, `BLOCK_SECONDS` at a time. The engine
pops commands one at a time (`BRPOP`, oldest first), so a command sent while it's
restarting waits in the list and is applied before any bot is loaded. It pushes the
reply once it has acted, and the reply expires after `REPLY_TTL_SECONDS` if nobody
reads it.

The commands:
- `KillCommand`: the kill switch. Stop every bot, cancel every open order, and, if
  asked, sell every open position. It never expires: one sent while the engine is down
  runs when it starts (D43).
- `CreateBotCommand` and `BotCommand` (start, pause, stop): the Bots page's (D47). They
  expire at `expires_at`, when the sender stops waiting for the reply: a click nobody
  saw answered must not take effect when the engine next starts, hours later.
"""

import time
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, overload
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
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


class _Expiring(_Frozen):
    id: UUID = Field(default_factory=uuid4)
    source: str = Field(min_length=1, max_length=64)
    sent_at: AwareDatetime
    expires_at: AwareDatetime  # the engine drops it unread after this

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


_ALL_IN: dict[str, JsonValue] = {"mode": "all_in"}


class CreateBotCommand(_Expiring):
    """Add a stopped paper bot and fund it with `cash` of the quote currency. The engine
    checks everything (the strategy's params, sizing, stops, that the exchange lists the
    symbol) and refuses with the reason."""

    type: Literal["create_bot"] = "create_bot"
    name: str = Field(min_length=1, max_length=64)
    strategy: str = Field(min_length=1, max_length=64)
    params: dict[str, JsonValue] = Field(default_factory=dict)
    symbol: str = Field(min_length=3, max_length=32)  # CCXT format, e.g. BTC/USDT
    timeframe: str = Field(min_length=1, max_length=8)
    cash: Decimal = Field(gt=0)
    # As bots.sizing and bots.stops store them; the engine reads them (risk/sizing.py, stops.py).
    sizing: dict[str, JsonValue] = Field(default_factory=lambda: _ALL_IN.copy())
    stops: dict[str, JsonValue] = Field(default_factory=dict)


BotAction = Literal["start", "pause", "stop"]


class BotCommand(_Expiring):
    """Start, pause or stop one bot; a stop with `close_position` also sells its position."""

    type: Literal["bot"] = "bot"
    action: BotAction
    bot_id: int
    close_position: bool = False  # stop only


class BotReply(_Frozen):
    id: UUID  # the command's
    ok: bool
    error: str | None = None  # when not ok: why it was refused, or what went wrong
    bot_id: int | None = None
    status: str | None = None  # the bot's status afterwards
    canceled: list[str] = Field(default_factory=list)  # the orders a stop cancelled
    closing: list[Closing] = Field(default_factory=list)  # a stop + close's sells
    not_closed: list[NotClosed] = Field(default_factory=list)
    took_ms: int


Command = Annotated[KillCommand | CreateBotCommand | BotCommand, Field(discriminator="type")]
COMMAND: TypeAdapter[Command] = TypeAdapter(Command)


@overload
async def send(redis: Redis, command: KillCommand, *, reply_within: float) -> KillReply | None: ...
@overload
async def send(
    redis: Redis, command: CreateBotCommand | BotCommand, *, reply_within: float
) -> BotReply | None: ...
async def send(
    redis: Redis, command: Command, *, reply_within: float
) -> KillReply | BotReply | None:
    """Queue `command` for the engine and wait up to `reply_within` seconds for its reply
    (Redis does the waiting, in BLPOP, `BLOCK_SECONDS` at a time).

    None if there's none by then; the command stays queued (a bot command until it expires).
    """
    await redis.lpush(COMMANDS_KEY, command.model_dump_json())
    reply_type = KillReply if isinstance(command, KillCommand) else BotReply
    deadline = time.monotonic() + reply_within
    while (left := deadline - time.monotonic()) > 0:
        popped = await redis.blpop([reply_key(command.id)], timeout=min(BLOCK_SECONDS, left))
        if popped is not None:
            return reply_type.model_validate_json(popped[1])
    return None
