"""The Market chart and its trade explanations (D46).

All of it is on the `protected` router (app.py): a session is checked before anything
here runs.

- `GET /bots/{bot_id}/chart?limit=`: the bot's newest `limit` candles, its strategy's
  indicators, the forming candle and its trades (chart.py).
- `WS /ws/bots/{bot_id}/chart?after=&since=`: what changed, every `feed_seconds`: candles
  closed after `after` (the browser's newest) with their indicators, the forming candle,
  and the trades and strategy switches from `since` (its oldest candle) when they
  changed. The session is checked again before each message, as on `/ws/overview`.
- `GET /orders/{order_id}/explain`: why a trade happened (explain.py).
"""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, status
from pydantic import AwareDatetime
from redis.asyncio import Redis
from starlette.datastructures import State
from starlette.websockets import WebSocketDisconnect

from backend.auth.deps import signed_in
from backend.market.chart import (
    DEFAULT_CANDLES,
    MAX_CANDLES,
    Chart,
    SwitchMarker,
    TradeMarker,
    lines_of,
    read_bot,
    read_chart,
    read_update,
)
from backend.market.explain import TradeExplanation, read_trade
from backend.settings import ApiSettings

router = APIRouter(tags=["market"])

# A WebSocket close code of our own (4000-4999 are for applications), like HTTP's 404.
NO_SUCH_BOT = 4404


def _now(state: State) -> datetime:
    return cast(Callable[[], datetime], state.clock)()


@router.get("/bots/{bot_id}/chart")
async def chart(
    bot_id: int,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=MAX_CANDLES)] = DEFAULT_CANDLES,
) -> Chart:
    state = request.app.state
    async with state.db.connect() as conn:
        bot = await read_bot(conn, bot_id)
        if bot is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no bot {bot_id}")
        return await read_chart(conn, cast(Redis, state.redis), bot, limit=limit, now=_now(state))


@router.websocket("/ws/bots/{bot_id}/chart")
async def chart_feed(
    websocket: WebSocket,
    bot_id: int,
    after: AwareDatetime | None = None,
    since: AwareDatetime | None = None,
) -> None:
    state = websocket.app.state
    await websocket.accept()
    async with state.db.connect() as conn:
        bot = await read_bot(conn, bot_id)
    if bot is None:
        # Not 1008, which tells the browser to sign in again.
        await websocket.close(NO_SUCH_BOT, reason=f"no bot {bot_id}")
        return
    lines = lines_of(bot)
    sent: list[TradeMarker] | None = None
    sent_switches: list[SwitchMarker] | None = None
    try:
        while True:
            if await signed_in(websocket) is None:
                await websocket.close(status.WS_1008_POLICY_VIOLATION, reason="sign in")
                return
            async with state.db.connect() as conn:
                update = await read_update(
                    conn,
                    cast(Redis, state.redis),
                    bot,
                    lines,
                    after=after,
                    since=since,
                    sent=sent,
                    sent_switches=sent_switches,
                    now=_now(state),
                )
            await websocket.send_text(update.model_dump_json())
            if update.candles:
                after = update.candles[-1].ts
                since = since or update.candles[0].ts
            if update.trades is not None:
                sent = update.trades
            if update.switches is not None:
                sent_switches = update.switches
            # Waits out the interval, but returns at once if the browser goes away.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(cast(ApiSettings, state.settings).feed_seconds):
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
    except WebSocketDisconnect:
        return  # the browser went away mid-send


@router.get("/orders/{order_id}/explain")
async def explain(order_id: int, request: Request) -> TradeExplanation:
    async with request.app.state.db.connect() as conn:
        trade = await read_trade(conn, order_id)
    if trade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no order {order_id}")
    return trade
