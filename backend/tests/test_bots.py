"""The Bots page's API (backend/bots/, D47): the list and the catalog are read from the
database and Redis; create, start, pause and stop are commands to the engine, over the
Redis command list, answered with its reply.

A stand-in for the engine pops each command and replies; the engine's side is tested in
engine/tests/test_bot_control.py.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import insert, text, update

from backend.testing import T0, Api
from shared.catalog import CATALOG_KEY, Catalog, Choice, FieldSpec
from shared.commands import (
    COMMAND,
    COMMANDS_KEY,
    BotCommand,
    BotReply,
    Closing,
    Command,
    CreateBotCommand,
    reply_key,
)
from shared.db.models import BotRow, PaperBalanceRow, StrategyConfigRow


async def engine_replies(api: Api, **reply: Any) -> Command:
    """Pop one command, as the engine's CommandListener does, and reply to it."""
    popped = await api.redis.brpop([COMMANDS_KEY], timeout=5)
    assert popped is not None, "no command was sent"
    command = COMMAND.validate_json(popped[1])
    answer = BotReply(id=command.id, took_ms=7, **{"ok": True, **reply})
    await api.redis.lpush(reply_key(command.id), answer.model_dump_json())
    return command


async def clean(api: Api) -> None:
    async with api.db.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " risk_events RESTART IDENTITY"
            )
        )


NEW_BOT = {
    "name": "eth-ema",
    "strategy": "ema_crossover",
    "params": {"fast": 9, "slow": 21},
    "symbol": "ETH/USDT",
    "timeframe": "1h",
    "cash": "500",
    "sizing": {"mode": "percent_of_equity", "percent": "25"},
    "stops": {"stop_loss": {"kind": "percent", "percent": "3"}},
}


async def test_create_sends_the_engine_the_bot_and_answers_with_its_reply(api: Api) -> None:
    await api.login()
    engine = asyncio.create_task(engine_replies(api, bot_id=4, status="stopped"))

    response = await api.client.post("/bots", json=NEW_BOT)
    command = await engine

    assert response.status_code == 201
    assert (response.json()["bot_id"], response.json()["status"]) == (4, "stopped")
    assert isinstance(command, CreateBotCommand)
    assert (command.name, command.symbol, command.cash, command.params) == (
        "eth-ema",
        "ETH/USDT",
        Decimal("500"),
        {"fast": 9, "slow": 21},
    )
    assert command.sizing == {"mode": "percent_of_equity", "percent": "25"}
    assert (command.source, command.sent_at, command.expires_at) == (
        "dashboard",
        T0,
        T0 + timedelta(seconds=1),  # the test settings wait 1 s
    )


async def test_the_engines_refusal_is_a_422_with_its_reason(api: Api) -> None:
    await api.login()
    why = "fast (21) must be shorter than slow (9)"
    engine = asyncio.create_task(engine_replies(api, ok=False, error=why))

    response = await api.client.post("/bots", json={**NEW_BOT, "params": {"fast": 21, "slow": 9}})
    await engine

    assert (response.status_code, response.json()["detail"]) == (422, why)


async def test_a_bad_name_or_unknown_field_never_reaches_the_engine(api: Api) -> None:
    await api.login()

    for body in ({**NEW_BOT, "name": "has space"}, {**NEW_BOT, "cash": "0"}, {**NEW_BOT, "x": 1}):
        assert (await api.client.post("/bots", json=body)).status_code == 422, body
    assert await api.redis.llen(COMMANDS_KEY) == 0


async def test_start_pause_and_stop_send_their_command(api: Api) -> None:
    await api.login()
    closing = Closing(bot_id=3, symbol="BTC/USDT", amount=Decimal("0.5"))
    for path, body, action, close in [
        ("/bots/3/start", None, "start", False),
        ("/bots/3/pause", None, "pause", False),
        ("/bots/3/stop", {}, "stop", False),
        ("/bots/3/stop", {"close_position": True}, "stop", True),
    ]:
        engine = asyncio.create_task(engine_replies(api, bot_id=3, closing=[closing]))
        response = await api.client.post(path, json=body)
        command = await engine

        assert response.status_code == 200, path
        assert isinstance(command, BotCommand)
        assert (command.action, command.bot_id, command.close_position) == (action, 3, close)
        assert response.json()["closing"][0]["amount"] == "0.5"


async def test_with_no_reply_the_command_expires(api: Api) -> None:
    await api.login()

    response = await api.client.post("/bots/1/start")

    assert response.status_code == 504
    assert "dropped" in response.json()["detail"]
    [queued] = await api.redis.lrange(COMMANDS_KEY, 0, -1)
    command = COMMAND.validate_json(queued)
    # The engine drops it unread from now on.
    assert isinstance(command, BotCommand) and command.expired(T0 + timedelta(seconds=1))


async def test_the_list_has_each_bots_setup(api: Api) -> None:
    await clean(api)
    async with api.db.begin() as conn:
        await conn.execute(
            insert(BotRow).values(
                name="btc-ema",
                exchange="mexc",
                symbol="BTC/USDT",
                timeframe="1h",
                status="paused",
                paused_reason="paused from dashboard",
                paused_at=T0,
                stops={"stop_loss": {"kind": "percent", "percent": "3"}},
            )
        )
        await conn.execute(
            insert(StrategyConfigRow).values(
                bot_id=1, strategy="ema_crossover", version=1, params={"fast": 9}, created_by="cli"
            )
        )
        await conn.execute(update(BotRow).values(active_strategy_config_id=1))
        await conn.execute(
            insert(PaperBalanceRow).values(
                [
                    {
                        "bot_id": 1,
                        "currency": "USDT",
                        "free": Decimal("900"),
                        "used": Decimal("50"),
                    },
                    {"bot_id": 1, "currency": "BTC", "free": Decimal("1"), "used": Decimal("0")},
                ]
            )
        )
        await conn.execute(
            insert(BotRow).values(name="bare", exchange="mexc", symbol="ETH/USDT", timeframe="4h")
        )
    await api.login()

    response = await api.client.get("/bots")

    assert response.status_code == 200
    first, second = response.json()
    assert {k: first[k] for k in ("name", "status", "strategy", "params", "cash", "currency")} == {
        "name": "btc-ema",
        "status": "paused",
        "strategy": "ema_crossover",
        "params": {"fast": 9},
        "cash": "950",
        "currency": "USDT",
    }
    assert (first["sizing"], first["stops"]["stop_loss"]["percent"]) == ({"mode": "all_in"}, "3")
    assert first["paused_reason"] == "paused from dashboard"
    assert (second["name"], second["status"], second["strategy"], second["cash"]) == (
        "bare",
        "stopped",
        None,
        "0",
    )


async def test_the_catalog_is_the_engines(api: Api) -> None:
    await api.login()
    assert (await api.client.get("/strategies")).status_code == 503  # never written

    catalog = Catalog(
        strategies=[
            Choice(
                name="ema_crossover",
                title="EMA crossover",
                version=1,
                fields=[FieldSpec(name="fast", type="integer", default="9", minimum="2")],
            )
        ],
        sizing=[Choice(name="all_in", title="All in", fields=[])],
        stops={"stop_loss": []},
        stops_fields=[],
        timeframes=["1m", "1h"],
        written_at=T0,
    )
    await api.redis.set(CATALOG_KEY, catalog.model_dump_json())

    response = await api.client.get("/strategies")

    assert response.status_code == 200
    assert Catalog.model_validate(response.json()) == catalog
