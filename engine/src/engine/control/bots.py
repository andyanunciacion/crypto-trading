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
- **switch** (`SwitchStrategyCommand`, P6-1, D51): a new strategy config, checked as
  create checks one, becomes the bot's active config, and the policy its switch policy.
  A running or paused bot switches at its next candle (runner.switch), with no restart;
  a stopped one starts with it. The policy says what happens to an open position:
  `keep` (the config that bought it still sells it), `close_first` (sold now, until
  it's gone, as kind `switch`) or `hand_over` (the new config sells it). Refused while
  the bot's position is being sold or the bot is still starting, and when nothing
  changes. A change of policy alone adds no config and sells nothing; it only decides
  whether a trade bought before an earlier switch is still its buyer's to sell.
- **rollback** (`RollbackCommand`, P6-2, D52): back to one of the bot's earlier configs.
  Configs are append-only, so it adds a config copying that one's strategy and params
  (`restored_from` names it), built at the version it was made with, then switches to
  it exactly as a switch does, with the command's policy. Refused when the strategy's
  version has moved on, and when the bot already runs those params.

While the engine drains the commands sent while it was down (before any bot loads),
there's no runner: only the database changes, and `load_bots` then picks them up. A bot
command expires, though (shared/commands.py), so only one sent seconds ago runs then.
"""

import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast, get_args

import structlog
from pydantic import JsonValue, ValidationError
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.bots.create import Listings, create_paper_bot
from engine.bots.store import Bot, UnknownBot, load_bot, open_orders
from engine.exchange.errors import ExchangeError
from engine.exchange.models import Timeframe
from engine.risk.kill import SWITCH, KillSwitch
from engine.risk.paper import PaperDesk
from engine.risk.sizing import SIZING
from engine.risk.stops import STOPS
from engine.strategies.base import Strategy
from engine.strategies.registry import build, strategy_class
from shared.commands import (
    BotCommand,
    BotCommands,
    BotReply,
    Closing,
    CreateBotCommand,
    NotClosed,
    RollbackCommand,
    SwitchStrategyCommand,
)
from shared.db.models import BotRow, StrategyConfigRow

log = structlog.get_logger()


class Runner(Protocol):
    """The bot runner, as the Bots page's commands see it."""

    def add(self, bot: Bot) -> bool: ...

    def starting(self, bot_id: int) -> bool: ...

    def switch(self, bot: Bot) -> bool: ...

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
    strategy_config_id: int | None = None


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

    async def handle(self, command: BotCommands) -> BotReply:
        """Carry out `command` (module docstring) and say what came of it.

        A refusal is a reply with `ok: false`. Anything else that goes wrong raises,
        and the listener answers with the error.
        """
        started = time.monotonic()
        log.info("bot_command.received", **command.model_dump(mode="json"))
        bot_id = None if isinstance(command, CreateBotCommand) else command.bot_id
        try:
            if isinstance(command, CreateBotCommand):
                done = await self._create(command)
            elif isinstance(command, SwitchStrategyCommand):
                done = await self._switch(command)
            elif isinstance(command, RollbackCommand):
                done = await self._rollback(command)
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
            strategy_config_id=done.strategy_config_id,
            took_ms=_ms_since(started),
        )
        log.info("bot_command.done", **reply.model_dump(mode="json"))
        return reply

    # --- The commands ---------------------------------------------------------------------

    async def _create(self, command: CreateBotCommand) -> _Done:
        try:
            strategy = _strategy(command.strategy, command.params)
            sizing = SIZING.validate_python(command.sizing)
            stops = STOPS.validate_python(command.stops)
        except ValidationError as exc:
            raise Refused(describe(exc)) from None
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
        if self._kill_switch.sells_for(bot.id):
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

    async def _switch(self, command: SwitchStrategyCommand) -> _Done:
        strategy = _strategy(command.strategy, command.params)
        return await self._apply_switch(command, strategy)

    async def _rollback(self, command: RollbackCommand) -> _Done:
        bot_id, config_id = command.bot_id, command.config_id
        async with self._db.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        StrategyConfigRow.bot_id,
                        StrategyConfigRow.strategy,
                        StrategyConfigRow.version,
                        StrategyConfigRow.params,
                    ).where(StrategyConfigRow.id == config_id)
                )
            ).one_or_none()
        if row is None or row.bot_id != bot_id:
            raise Refused(f"bot {bot_id} has no strategy config {config_id}")
        # At the version it was made with: one whose strategy has moved on would no
        # longer give the signals it gave then, so it isn't the same config.
        try:
            strategy = build(row.strategy, row.version, row.params)
        except ValidationError as exc:
            raise Refused(f"can't roll back to config {config_id}: {describe(exc)}") from None
        except ValueError as exc:
            raise Refused(f"can't roll back to config {config_id}: {exc}") from None
        return await self._apply_switch(command, strategy, restored_from=config_id)

    async def _apply_switch(
        self,
        command: SwitchStrategyCommand | RollbackCommand,
        strategy: Strategy[Any],
        *,
        restored_from: int | None = None,
    ) -> _Done:
        """Make `strategy` bot `command.bot_id`'s active config, with `command.policy`.

        A rollback (`restored_from`, the config it copies) always adds a config: one that
        changes nothing is refused rather than read as a change of policy alone.
        """
        bot_id, policy = command.bot_id, command.policy
        if self._kill_switch.sells_for(bot_id):
            raise Refused(
                "its position is still being sold (kill switch, stop + close, or a switch "
                "that closes first): switch it once that's done"
            )
        if self.runner is not None and self.runner.starting(bot_id):
            raise Refused("it's still warming up: switch it once it's in the engine")
        params: dict[str, JsonValue] = strategy.params.model_dump(mode="json")
        async with self._db.begin() as conn:
            # Locked: two switches of one bot can't both read the same old config.
            row = (
                await conn.execute(
                    select(
                        BotRow.status,
                        BotRow.mode,
                        BotRow.exchange,
                        BotRow.symbol,
                        BotRow.switch_policy,
                        BotRow.active_strategy_config_id,
                        StrategyConfigRow.strategy,
                        StrategyConfigRow.version,
                        StrategyConfigRow.params,
                    )
                    .outerjoin(
                        StrategyConfigRow,
                        StrategyConfigRow.id == BotRow.active_strategy_config_id,
                    )
                    .where(BotRow.id == bot_id)
                    .with_for_update(of=BotRow)
                )
            ).one_or_none()
            if row is None:
                raise Refused(f"there's no bot {bot_id}")
            if row.mode != "paper":
                raise Refused(f"{row.mode} bots can't be switched yet: only paper bots until P9")
            if row.exchange != self._exchange.exchange_id:
                raise Refused(
                    f"the engine trades on {self._exchange.exchange_id}, not {row.exchange}"
                )
            same = (row.strategy, row.version, row.params) == (
                strategy.name,
                strategy.version,
                params,
            )
            if same and restored_from is not None:
                raise Refused(
                    "nothing to change: it already runs that config's strategy and params"
                )
            if same and row.switch_policy == policy:
                raise Refused(
                    "nothing to change: it already runs that strategy with those params, "
                    f"and its switch policy is {policy}"
                )
            config_id = None
            values: dict[str, object] = {"switch_policy": policy}
            if not same:
                config_id = await conn.scalar(
                    insert(StrategyConfigRow)
                    .values(
                        bot_id=bot_id,
                        strategy=strategy.name,
                        version=strategy.version,
                        params=params,
                        created_by=command.source,
                        restored_from=restored_from,
                    )
                    .returning(StrategyConfigRow.id)
                )
                values["active_strategy_config_id"] = config_id
            await conn.execute(update(BotRow).where(BotRow.id == bot_id).values(**values))
        position = await self._desk.risk.position(bot_id, row.symbol)
        fields: dict[str, JsonValue] = {
            "bot_id": bot_id,
            "policy": policy,
            "old_policy": row.switch_policy,
            "old": {
                "id": row.active_strategy_config_id,
                "strategy": row.strategy,
                "version": row.version,
                "params": row.params,
            },
            "position": None if position is None else position.model_dump(mode="json"),
        }
        switching = False
        if self.runner is not None and row.status != "stopped":
            # Loaded as a restart would load it. A new policy alone can still start or
            # end a keeper for a trade bought before an earlier switch (store.py).
            bot, _ = await load_bot(self._db, self._exchange.exchange_id, bot_id)
            switching = self.runner.switch(bot)
        if config_id is None:
            log.info("bot.switch_policy_set", **fields, switching=switching)
            return _Done(bot_id, row.status)
        log.info(
            "bot.switch",
            **fields,
            new={
                "id": config_id,
                "strategy": strategy.name,
                "version": strategy.version,
                "params": params,
            },
            restored_from=restored_from,  # a rollback's: the config it copies (D52)
            # False: the database alone changed; a Start or a restart loads it.
            switching=switching,
        )
        done = _Done(bot_id, row.status, strategy_config_id=config_id)
        if policy == "close_first" and position is not None:
            done.closing, done.not_closed = await self._kill_switch.close_bot(
                bot_id, command.id, kind=SWITCH
            )
        return done

    async def _status(self, bot_id: int) -> str:
        async with self._db.connect() as conn:
            status = await conn.scalar(select(BotRow.status).where(BotRow.id == bot_id))
        if status is None:
            raise Refused(f"there's no bot {bot_id}")
        return status


def _strategy(name: str, params: dict[str, JsonValue]) -> Strategy[Any]:
    """Strategy `name`, at its version now, with `params`; Refused with what's wrong."""
    try:
        return build(name, strategy_class(name).version, params)
    except ValidationError as exc:
        raise Refused(describe(exc)) from None
    except ValueError as exc:  # an unknown strategy
        raise Refused(str(exc)) from None


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
