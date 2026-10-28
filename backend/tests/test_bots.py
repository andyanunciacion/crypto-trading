"""The Bots page's API (backend/bots/, D47): the list and the catalog are read from the
database and Redis, as is a bot's detail with its config history (D52); create, start,
pause, stop, a strategy switch (D51) and a rollback (D52) are commands to the engine,
over the Redis command list, answered with its reply.

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
    RollbackCommand,
    SwitchStrategyCommand,
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


async def test_a_switch_sends_the_new_strategy_and_policy(api: Api) -> None:
    await api.login()
    closing = Closing(bot_id=3, symbol="BTC/USDT", amount=Decimal("0.5"))
    engine = asyncio.create_task(
        engine_replies(api, bot_id=3, status="running", strategy_config_id=8, closing=[closing])
    )

    response = await api.client.post(
        "/bots/3/strategy",
        json={"strategy": "rsi_mean_reversion", "params": {"period": 7}, "policy": "close_first"},
    )
    command = await engine

    assert response.status_code == 200
    assert (response.json()["strategy_config_id"], response.json()["closing"][0]["amount"]) == (
        8,
        "0.5",
    )
    assert isinstance(command, SwitchStrategyCommand)
    assert (command.bot_id, command.strategy, command.params, command.policy) == (
        3,
        "rsi_mean_reversion",
        {"period": 7},
        "close_first",
    )
    assert (command.source, command.expires_at) == ("dashboard", T0 + timedelta(seconds=1))


async def test_a_switch_with_an_unknown_policy_or_field_never_reaches_the_engine(
    api: Api,
) -> None:
    await api.login()
    good = {"strategy": "rsi_mean_reversion", "params": {}, "policy": "keep"}

    for body in ({**good, "policy": "sell_all"}, {**good, "x": 1}, {"strategy": "ema_crossover"}):
        assert (await api.client.post("/bots/3/strategy", json=body)).status_code == 422, body
    assert await api.redis.llen(COMMANDS_KEY) == 0


async def test_the_engines_refusal_of_a_switch_is_a_422(api: Api) -> None:
    await api.login()
    why = "it's still warming up: switch it once it's in the engine"
    engine = asyncio.create_task(engine_replies(api, ok=False, error=why))

    response = await api.client.post(
        "/bots/3/strategy", json={"strategy": "ema_crossover", "policy": "hand_over"}
    )
    await engine

    assert (response.status_code, response.json()["detail"]) == (422, why)


async def test_a_rollback_sends_the_config_and_policy(api: Api) -> None:
    await api.login()
    engine = asyncio.create_task(
        engine_replies(api, bot_id=3, status="running", strategy_config_id=9)
    )

    response = await api.client.post("/bots/3/rollback", json={"config_id": 5, "policy": "keep"})
    command = await engine

    assert (response.status_code, response.json()["strategy_config_id"]) == (200, 9)
    assert isinstance(command, RollbackCommand)
    assert (command.bot_id, command.config_id, command.policy) == (3, 5, "keep")
    assert (command.source, command.expires_at) == ("dashboard", T0 + timedelta(seconds=1))


async def test_a_bad_rollback_never_reaches_the_engine(api: Api) -> None:
    await api.login()
    good = {"config_id": 5, "policy": "keep"}

    for body in (
        {**good, "policy": "sell_all"},
        {**good, "strategy": "ema_crossover"},  # the engine reads the config itself
        {"policy": "keep"},
        {**good, "config_id": "five"},
    ):
        assert (await api.client.post("/bots/3/rollback", json=body)).status_code == 422, body
    assert await api.redis.llen(COMMANDS_KEY) == 0


async def test_the_engines_refusal_of_a_rollback_is_a_422(api: Api) -> None:
    await api.login()
    why = "can't roll back to config 1: ema_crossover is now version 2, not version 1"
    engine = asyncio.create_task(engine_replies(api, ok=False, error=why))

    response = await api.client.post("/bots/3/rollback", json={"config_id": 1, "policy": "keep"})
    await engine

    assert (response.status_code, response.json()["detail"]) == (422, why)


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
    assert (first["switch_policy"], first["active_strategy_config_id"]) == ("keep", 1)
    assert first["paused_reason"] == "paused from dashboard"
    assert (second["name"], second["status"], second["strategy"], second["cash"]) == (
        "bare",
        "stopped",
        None,
        "0",
    )


async def test_a_bots_detail_has_its_config_history(api: Api) -> None:
    await clean(api)
    ema = {"strategy": "ema_crossover", "version": 1, "params": {"fast": 9, "slow": 21}}
    async with api.db.begin() as conn:
        bot = {"exchange": "mexc", "symbol": "BTC/USDT", "timeframe": "1h"}
        await conn.execute(insert(BotRow), [{**bot, "name": "btc-ema"}, {**bot, "name": "other"}])
        await conn.execute(
            insert(StrategyConfigRow),
            [
                {"bot_id": 1, **ema, "created_by": "cli"},  # 1
                {"bot_id": 2, **ema, "created_by": "cli"},  # 2: the other bot's
                {
                    "bot_id": 1,
                    "strategy": "rsi_mean_reversion",
                    "version": 1,
                    "params": {"period": 7},
                    "created_by": "dashboard",
                },  # 3
            ],
        )
        await conn.execute(
            insert(StrategyConfigRow).values(
                bot_id=1, **ema, created_by="dashboard", restored_from=1
            )
        )  # 4: the rollback
        await conn.execute(update(BotRow).where(BotRow.id == 1).values(active_strategy_config_id=4))
    await api.login()

    response = await api.client.get("/bots/1")

    assert response.status_code == 200
    body = response.json()
    assert (body["bot"]["name"], body["bot"]["params"]) == ("btc-ema", {"fast": 9, "slow": 21})
    configs = [
        (c["id"], c["number"], c["strategy"], c["created_by"], c["restored_from"], c["active"])
        for c in body["configs"]
    ]
    assert configs == [
        (4, 3, "ema_crossover", "dashboard", 1, True),
        (3, 2, "rsi_mean_reversion", "dashboard", None, False),
        (1, 1, "ema_crossover", "cli", None, False),
    ]
    assert body["configs"][1]["params"] == {"period": 7}
    assert (await api.client.get("/bots/99")).status_code == 404


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
