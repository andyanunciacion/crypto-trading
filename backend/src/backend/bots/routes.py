"""The Bots page (D47): list, create, start, pause and stop bots, and switch their
strategy (P6-1, D51); a bot's detail page, with its config history and rollback (P6-2,
D52).

All of it is on the `protected` router (app.py): a session is checked before anything
here runs.

- `GET /strategies`: what a new bot can be set up with, as the engine described it at
  startup (shared/catalog.py). 503 if it never has: the engine isn't running yet.
- `GET /bots`: every bot with its setup (store.py).
- `GET /bots/{bot_id}`: one bot's setup, every strategy config it has had and every
  switch between them (P6-4); 404 if there's no such bot.
- `POST /bots`, `POST /bots/{bot_id}/start`, `/pause`, `/stop`, `/strategy`, `/rollback`,
  `/auto-switch` (P6-4: its regime -> strategy mapping, or off):
  each sends the engine a command over the Redis command list, as the kill switch does
  (shared/commands.py), and answers with its reply. The API never writes a bot: the
  engine checks everything, and its refusal comes back as a 422 with the reason. With
  no reply in `kill_reply_seconds`, it answers 504; the command expires then, so it
  can't take effect later, when nobody's looking.
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from redis.asyncio import Redis
from starlette.datastructures import State

from backend.auth.deps import CurrentUser
from backend.bots.store import BotDetail, BotSetup, read_bot, read_bots
from backend.settings import ApiSettings
from shared.catalog import CATALOG_KEY, Catalog
from shared.commands import (
    AutoSwitchCommand,
    BotAction,
    BotCommand,
    BotCommands,
    BotReply,
    CreateBotCommand,
    RollbackCommand,
    SwitchPolicy,
    SwitchStrategyCommand,
    send,
)
from shared.regime import AutoSwitchSettings

log = structlog.get_logger()

router = APIRouter(tags=["bots"])

SOURCE = "dashboard"


def _all_in() -> dict[str, JsonValue]:
    return {"mode": "all_in"}


class CreateBotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    strategy: str = Field(min_length=1, max_length=64)
    params: dict[str, JsonValue] = Field(default_factory=dict)
    symbol: str = Field(min_length=3, max_length=32)
    timeframe: str = Field(min_length=1, max_length=8)
    cash: Decimal = Field(gt=0)
    sizing: dict[str, JsonValue] = Field(default_factory=_all_in)
    stops: dict[str, JsonValue] = Field(default_factory=dict)


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    close_position: bool = False  # also sell the bot's position until it's gone


class SwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(min_length=1, max_length=64)
    params: dict[str, JsonValue] = Field(default_factory=dict)
    policy: SwitchPolicy  # what happens to an open position (shared/commands.py)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_id: int  # one of the bot's earlier strategy configs
    policy: SwitchPolicy


class AutoSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The mapping, minimum hold and policy; None turns auto-switching off. The shape is
    # checked here; each strategy's params, by the engine.
    settings: AutoSwitchSettings | None


def _settings(state: State) -> ApiSettings:
    return cast(ApiSettings, state.settings)


def _now(state: State) -> datetime:
    return cast(Callable[[], datetime], state.clock)()


@router.get("/strategies")
async def strategies(request: Request) -> Catalog:
    raw = await cast(Redis, request.app.state.redis).get(CATALOG_KEY)
    if raw is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The engine hasn't said which strategies it has yet: is it running?",
        )
    return Catalog.model_validate_json(raw)


@router.get("/bots")
async def bots(request: Request) -> list[BotSetup]:
    return await read_bots(request.app.state.db)


@router.get("/bots/{bot_id}")
async def bot(bot_id: int, request: Request) -> BotDetail:
    detail = await read_bot(request.app.state.db, bot_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"There's no bot {bot_id}.")
    return detail


@router.post("/bots", status_code=status.HTTP_201_CREATED)
async def create(body: CreateBotRequest, request: Request, user: CurrentUser) -> BotReply:
    state = request.app.state
    now = _now(state)
    command = CreateBotCommand(
        **body.model_dump(),
        source=SOURCE,
        sent_at=now,
        expires_at=now + timedelta(seconds=_settings(state).kill_reply_seconds),
    )
    return await _send(state, command, user.username, {"name": body.name})


@router.post("/bots/{bot_id}/start")
async def start(bot_id: int, request: Request, user: CurrentUser) -> BotReply:
    return await _bot_command(request.app.state, "start", bot_id, user.username)


@router.post("/bots/{bot_id}/pause")
async def pause(bot_id: int, request: Request, user: CurrentUser) -> BotReply:
    return await _bot_command(request.app.state, "pause", bot_id, user.username)


@router.post("/bots/{bot_id}/stop")
async def stop(bot_id: int, body: StopRequest, request: Request, user: CurrentUser) -> BotReply:
    return await _bot_command(
        request.app.state, "stop", bot_id, user.username, close_position=body.close_position
    )


@router.post("/bots/{bot_id}/strategy")
async def switch(bot_id: int, body: SwitchRequest, request: Request, user: CurrentUser) -> BotReply:
    state = request.app.state
    now = _now(state)
    command = SwitchStrategyCommand(
        bot_id=bot_id,
        **body.model_dump(),
        source=SOURCE,
        sent_at=now,
        expires_at=now + timedelta(seconds=_settings(state).kill_reply_seconds),
    )
    fields = {"bot_id": bot_id, "strategy": body.strategy, "policy": body.policy}
    return await _send(state, command, user.username, fields)


@router.post("/bots/{bot_id}/rollback")
async def rollback(
    bot_id: int, body: RollbackRequest, request: Request, user: CurrentUser
) -> BotReply:
    state = request.app.state
    now = _now(state)
    command = RollbackCommand(
        bot_id=bot_id,
        **body.model_dump(),
        source=SOURCE,
        sent_at=now,
        expires_at=now + timedelta(seconds=_settings(state).kill_reply_seconds),
    )
    fields = {"bot_id": bot_id, "config_id": body.config_id, "policy": body.policy}
    return await _send(state, command, user.username, fields)


@router.post("/bots/{bot_id}/auto-switch")
async def auto_switch(
    bot_id: int, body: AutoSwitchRequest, request: Request, user: CurrentUser
) -> BotReply:
    state = request.app.state
    now = _now(state)
    settings = None if body.settings is None else body.settings.model_dump(mode="json")
    command = AutoSwitchCommand(
        bot_id=bot_id,
        settings=settings,
        source=SOURCE,
        sent_at=now,
        expires_at=now + timedelta(seconds=_settings(state).kill_reply_seconds),
    )
    fields = {"bot_id": bot_id, "auto_switch": body.settings is not None}
    return await _send(state, command, user.username, fields)


async def _bot_command(
    state: State, action: BotAction, bot_id: int, username: str, *, close_position: bool = False
) -> BotReply:
    now = _now(state)
    command = BotCommand(
        action=action,
        bot_id=bot_id,
        close_position=close_position,
        source=SOURCE,
        sent_at=now,
        expires_at=now + timedelta(seconds=_settings(state).kill_reply_seconds),
    )
    fields = {"action": action, "bot_id": bot_id, "close_position": close_position}
    return await _send(state, command, username, fields)


async def _send(
    state: State,
    command: BotCommands,
    username: str,
    fields: Mapping[str, object],
) -> BotReply:
    fields = {"id": str(command.id), "type": command.type, **fields}
    log.info("api.bot.sent", user=username, **fields)
    wait = _settings(state).kill_reply_seconds
    reply = await send(cast(Redis, state.redis), command, reply_within=wait)
    if reply is None:
        log.error("api.bot.no_reply", waited_s=wait, **fields)
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                f"No reply from the engine in {wait:g} s, so the command was dropped: is the "
                "engine running? If it was only slow, it may have gone through; the bot's "
                "status shows."
            ),
        )
    log.info("api.bot.reply", reply=reply.model_dump(mode="json"), **fields)
    if not reply.ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=reply.error)
    return reply
