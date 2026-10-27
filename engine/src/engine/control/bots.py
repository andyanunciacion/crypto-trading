"""The Bots page's commands (D47): create a bot, and start, pause or stop one.

They come over the Redis command list, like the kill switch (listener.py), so the API
never writes a bot or reaches the exchange; the engine checks everything and refuses
with a reason the dashboard shows. Each gets a `BotReply`:

- **create**: the strategy and its params, the sizing, the stops and the timeframe are
  checked as the CLI checks them, and the exchange must list the symbol. The bot is
  added **stopped** and funded with its paper cash: starting it is a separate click.
- **start** (a stopped or paused bot): it must still build (its strategy version and
  params), and its position mustn't be one the kill switch (or a stop + close) is still
  selling. It's set running, and the bot runner starts it (runner.add: warm-up in the
  background, then it trades from the next candle).
- **pause** (a running bot): set paused, as the error breaker pauses one (D42). The
  runner keeps it: its sells and stops carry on, and the RiskManager blocks its buys.
- **stop**: set stopped; the runner stops it, once it's done with what it's doing; its
  open orders are cancelled. With `close_position`, its position is sold until it's
  gone, as the kill switch would (risk/kill.py, `close_bot`). Without, the position is
  left open with nobody watching its stops, and the dashboard says so. Stopping a bot
  that's stopped already still cancels and closes, so it can close a stopped bot's
  position.

While the engine drains the commands sent while it was down (before any bot loads),
there's no runner: only the database changes, and `load_bots` then picks them up. A bot
command expires, though (shared/commands.py), so only one sent seconds ago runs then.
"""

import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, cast, get_args

import structlog
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.bots.create import Listings, create_paper_bot
from engine.bots.store import Bot, UnknownBot, load_bot, open_orders
from engine.exchange.errors import ExchangeError
from engine.exchange.models import Timeframe
from engine.risk.kill import KillSwitch
from engine.risk.paper import PaperDesk
from engine.risk.sizing import SIZING
from engine.risk.stops import STOPS
from engine.strategies.registry import build, strategy_class
from shared.commands import BotCommand, BotReply, Closing, CreateBotCommand, NotClosed
from shared.db.models import BotRow

log = structlog.get_logger()


class Runner(Protocol):
    """The bot runner, as the Bots page's commands see it."""

    def add(self, bot: Bot) -> bool: ...

    async def stop(self, bot_ids: Collection[int]) -> list[int]: ...


class Refused(Exception):
    """A command the engine won't carry out, and why (shown to whoever sent it)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Done:
    bot_id: int
    status: str
    canceled: list[str] = field(default_factory=list)
    closing: list[Closing] = field(default_factory=list)
    not_closed: list[NotClosed] = field(default_factory=list)


class BotControl:
    def __init__(
        self,
        db: AsyncEngine,
        desk: PaperDesk,
        exchange: Listings,
        kill_switch: KillSwitch,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._db = db
        self._desk = desk
        self._exchange = exchange
        self._kill_switch = kill_switch
        self._clock = clock
        # Set once the bots are loaded; until then, commands change only the database.
        self.runner: Runner | None = None

    async def handle(self, command: CreateBotCommand | BotCommand) -> BotReply:
        """Carry out `command` (module docstring) and say what came of it.

        A refusal is a reply with `ok: false`. Anything else that goes wrong raises,
        and the listener answers with the error.
        """
        started = time.monotonic()
        log.info("bot_command.received", **command.model_dump(mode="json"))
        bot_id = command.bot_id if isinstance(command, BotCommand) else None
        try:
            if isinstance(command, CreateBotCommand):
                done = await self._create(command)
            elif command.action == "start":
                done = await self._start(command)
            elif command.action == "pause":
                done = await self._pause(command)
            else:
                done = await self._stop(command)
        except Refused as exc:
            reply = BotReply(
                id=command.id,
                ok=False,
                error=str(exc),
                bot_id=bot_id,
                took_ms=_ms_since(started),
            )
            log.warning("bot_command.refused", **reply.model_dump(mode="json"))
            return reply
        reply = BotReply(
            id=command.id,
            ok=True,
            bot_id=done.bot_id,
            status=done.status,
            canceled=done.canceled,
            closing=done.closing,
            not_closed=done.not_closed,
            took_ms=_ms_since(started),
        )
        log.info("bot_command.done", **reply.model_dump(mode="json"))
        return reply

    # --- The commands ---------------------------------------------------------------------

    async def _create(self, command: CreateBotCommand) -> _Done:
        try:
            strategy = build(
                command.strategy, strategy_class(command.strategy).version, command.params
            )
            sizing = SIZING.validate_python(command.sizing)
            stops = STOPS.validate_python(command.stops)
        except ValidationError as exc:
            raise Refused(describe(exc)) from None
        except ValueError as exc:  # an unknown strategy
            raise Refused(str(exc)) from None
        timeframes = get_args(Timeframe)
        if command.timeframe not in timeframes:
            raise Refused(f"timeframe {command.timeframe!r} isn't one of {', '.join(timeframes)}")
        if "/" not in command.symbol:
            raise Refused(f"{command.symbol!r} isn't a symbol like BTC/USDT")
        try:
            bot_id = await create_paper_bot(
                self._db,
                self._desk,
                self._exchange,
                name=command.name,
                strategy=strategy,
                symbol=command.symbol,
                timeframe=cast(Timeframe, command.timeframe),  # checked above
                cash=command.cash,
                sizing=sizing,
                stops=stops,
                status="stopped",
                created_by=command.source,
            )
        except IntegrityError:
            raise Refused(f"a bot named {command.name!r} already exists") from None
        except ExchangeError as exc:
            raise Refused(f"{command.symbol}: {exc}") from None
        return _Done(bot_id, "stopped")

    async def _start(self, command: BotCommand) -> _Done:
        try:
            bot, _ = await load_bot(self._db, self._exchange.exchange_id, command.bot_id)
        except UnknownBot as exc:
            raise Refused(str(exc)) from None
        except ValueError as exc:  # includes pydantic's ValidationError for stored params
            raise Refused(f"this engine can't run it: {exc}") from None
        if any(bot_id == bot.id for bot_id, _ in self._kill_switch.selling):
            raise Refused(
                "its position is still being sold (kill switch, or stop + close): "
                "start it once that's done"
            )
        async with self._db.begin() as conn:
            await conn.execute(
                update(BotRow)
                .where(BotRow.id == bot.id)
                .values(
                    status="running",
                    paused_reason=None,
                    paused_at=None,
                    stopped_reason=None,
                    stopped_at=None,
                )
            )
        if self.runner is not None:
            self.runner.add(bot)  # False if it runs already: a paused bot does
        return _Done(bot.id, "running")

    async def _pause(self, command: BotCommand) -> _Done:
        status = await self._status(command.bot_id)
        if status == "paused":
            return _Done(command.bot_id, status)
        if status != "running":
            raise Refused(f"it's {status}: only a running bot can be paused")
        async with self._db.begin() as conn:
            await conn.execute(
                update(BotRow)
                .where(BotRow.id == command.bot_id, BotRow.status == "running")
                .values(
                    status="paused",
                    paused_reason=f"paused from {command.source}",
                    paused_at=self._clock(),
                )
            )
        return _Done(command.bot_id, "paused")

    async def _stop(self, command: BotCommand) -> _Done:
        bot_id = command.bot_id
        if await self._status(bot_id) != "stopped":
            reason = f"stopped from {command.source}"
            if command.close_position:
                reason += ", closing its position"
            async with self._db.begin() as conn:
                await conn.execute(
                    update(BotRow)
                    .where(BotRow.id == bot_id)
                    .values(
                        status="stopped",
                        stopped_reason=reason,
                        stopped_at=self._clock(),
                        paused_reason=None,
                        paused_at=None,
                    )
                )
        # From that commit the RiskManager blocks its buys (D42); then it stops acting.
        if self.runner is not None:
            await self.runner.stop([bot_id])
        canceled = []
        for cid in await open_orders(self._db, bot_id, mode=self._desk.risk.mode):
            await self._desk.risk.cancel(cid)
            canceled.append(cid)
        done = _Done(bot_id, "stopped", canceled)
        if command.close_position:
            done.closing, done.not_closed = await self._kill_switch.close_bot(bot_id, command.id)
        return done

    async def _status(self, bot_id: int) -> str:
        async with self._db.connect() as conn:
            status = await conn.scalar(select(BotRow.status).where(BotRow.id == bot_id))
        if status is None:
            raise Refused(f"there's no bot {bot_id}")
        return status


def describe(exc: ValidationError) -> str:
    """pydantic's errors as one line: `field: problem; ...`, without the input values."""
    problems = []
    for error in exc.errors():
        message = error["msg"].removeprefix("Value error, ")
        where = ".".join(str(part) for part in error["loc"])
        problems.append(f"{where}: {message}" if where else message)
    return "; ".join(problems)


def _ms_since(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
