"""The dashboard's Overview (REST and a live WebSocket) and its kill switch (D45).

All of it is on the `protected` router (app.py): a session is checked before anything
here runs.

- `GET /overview`: the Overview once (overview.py).
- `WS /ws/overview`: the Overview again every `feed_seconds`, until the browser goes
  away. The session is checked again before each one, so signing out (which ends every
  session) or the session expiring closes the socket with 1008, as a refused one is.
- `POST /engine/kill`: the kill switch. It sends the engine the same `KillCommand` as
  the CLI, over the Redis command list (shared/commands.py, D43), and waits for its
  reply. With none in time (the engine is down or busy), it answers 504: the command
  stays queued, and the engine carries it out when it next starts, before any bot loads.
"""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import cast

import structlog
from fastapi import APIRouter, HTTPException, Request, WebSocket, status
from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis
from starlette.datastructures import State
from starlette.websockets import WebSocketDisconnect

from backend.auth.deps import CurrentUser, signed_in
from backend.dashboard.overview import Overview, read_overview
from backend.settings import ApiSettings
from shared.commands import KillCommand, KillReply, send

log = structlog.get_logger()

router = APIRouter(tags=["dashboard"])


class KillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    close_positions: bool = False  # also sell every open position


def _settings(state: State) -> ApiSettings:
    return cast(ApiSettings, state.settings)


def _now(state: State) -> datetime:
    return cast(Callable[[], datetime], state.clock)()


async def _overview(state: State) -> Overview:
    return await read_overview(
        state.db, cast(Redis, state.redis), mode=_settings(state).mode, now=_now(state)
    )


@router.get("/overview")
async def overview(request: Request) -> Overview:
    return await _overview(request.app.state)


@router.websocket("/ws/overview")
async def overview_feed(websocket: WebSocket) -> None:
    state = websocket.app.state
    await websocket.accept()
    try:
        while True:
            if await signed_in(websocket) is None:
                await websocket.close(status.WS_1008_POLICY_VIOLATION, reason="sign in")
                return
            await websocket.send_text((await _overview(state)).model_dump_json())
            # Waits out the interval, but returns at once if the browser goes away. The
            # browser sends nothing else, so anything it does send is ignored.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_settings(state).feed_seconds):
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
    except WebSocketDisconnect:
        return  # the browser went away mid-send


@router.post("/engine/kill")
async def kill(body: KillRequest, request: Request, user: CurrentUser) -> KillReply:
    state = request.app.state
    command = KillCommand(
        close_positions=body.close_positions, source="dashboard", sent_at=_now(state)
    )
    fields = {"id": str(command.id), "close_positions": command.close_positions}
    log.warning("api.kill.sent", user=user.username, **fields)
    wait = _settings(state).kill_reply_seconds
    reply = await send(cast(Redis, state.redis), command, reply_within=wait)
    if reply is None:
        log.error("api.kill.no_reply", waited_s=wait, **fields)
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                f"No reply from the engine in {wait:g} s. The kill stays queued: it runs "
                "when the engine starts, before any bot loads."
            ),
        )
    log.warning("api.kill.reply", reply=reply.model_dump(mode="json"), **fields)
    return reply
