"""`POST /engine/kill`: the dashboard's kill switch sends the engine the same command as
the CLI, over the Redis command list, and answers with its reply (D43, D45).

A stand-in for the engine pops the command and replies; the engine's side is tested in
engine/tests/test_kill_switch.py.
"""

import asyncio

from backend.testing import T0, Api
from shared.commands import COMMANDS_KEY, KillCommand, KillReply, NotClosed, reply_key


async def engine_replies(api: Api) -> KillCommand:
    """Pop one command, as the engine's CommandListener does, and reply to it."""
    popped = await api.redis.brpop([COMMANDS_KEY], timeout=5)
    assert popped is not None, "no command was sent"
    command = KillCommand.model_validate_json(popped[1])
    reply = KillReply(
        id=command.id,
        ok=True,
        stopped=[1, 2],
        canceled=["b1-202610261200-b"],
        not_closed=[NotClosed(bot_id=2, symbol="ETH/USDT", reason="below the minimums")],
        took_ms=42,
    )
    await api.redis.lpush(reply_key(command.id), reply.model_dump_json())
    return command


async def test_the_kill_switch_reaches_the_engine_and_answers_with_its_reply(api: Api) -> None:
    await api.login()
    engine = asyncio.create_task(engine_replies(api))

    response = await api.client.post("/engine/kill", json={"close_positions": True})
    command = await engine

    assert (command.source, command.close_positions, command.sent_at) == ("dashboard", True, T0)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(command.id)
    assert (body["ok"], body["stopped"], body["canceled"], body["took_ms"]) == (
        True,
        [1, 2],
        ["b1-202610261200-b"],
        42,
    )
    assert body["not_closed"][0]["reason"] == "below the minimums"


async def test_stopping_bots_only_is_the_default(api: Api) -> None:
    await api.login()
    engine = asyncio.create_task(engine_replies(api))

    response = await api.client.post("/engine/kill", json={})

    assert response.status_code == 200
    assert (await engine).close_positions is False


async def test_with_no_reply_the_kill_stays_queued(api: Api) -> None:
    await api.login()

    # No engine: the test settings wait 1 s.
    response = await api.client.post("/engine/kill", json={})

    assert response.status_code == 504
    assert "stays queued" in response.json()["detail"]
    # Still on the list: the engine carries it out when it starts, before any bot loads.
    [queued] = await api.redis.lrange(COMMANDS_KEY, 0, -1)
    assert KillCommand.model_validate_json(queued).source == "dashboard"


async def test_unknown_fields_are_refused(api: Api) -> None:
    await api.login()

    response = await api.client.post("/engine/kill", json={"close_position": True})  # typo

    assert response.status_code == 422
    assert await api.redis.llen(COMMANDS_KEY) == 0
