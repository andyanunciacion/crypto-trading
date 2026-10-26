"""The Market chart (`GET /bots/{id}/chart`, `WS /ws/bots/{id}/chart`) and trade
explanations (`GET /orders/{id}/explain`), read from the database and Redis (D46).

alpha runs ema_crossover with fast 2 and slow 3, so each indicator point needs a
10-candle window. It has 30 stored hourly candles, 0 to 29, and these orders:
1. a signal buy, resized by the coin exposure limit, filled in two slices;
2. a stop-loss exit, filled;
3. a kill switch exit, partly filled, then cancelled;
4. a signal buy that never filled (no marker).
beta has a filled buy of its own, which never shows on alpha's chart.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, insert, text, update
from starlette.websockets import WebSocketDisconnect

from backend.app import create_app
from backend.testing import PASSWORD, T0, Api
from shared.candles import FormingCandle, forming_key
from shared.chart import chart_lines, line_points
from shared.db.models import (
    BotRow,
    CandleRow,
    FillRow,
    OrderRow,
    RiskEventRow,
    StrategyConfigRow,
)

BTC = "BTC/USDT"
HOUR = timedelta(hours=1)
C0 = datetime(2026, 10, 25, 6, tzinfo=T0.tzinfo)  # candle 0's open; candle 29 opens at 11:00 today
PARAMS = {"fast": 2, "slow": 3}


def d(value: str) -> Decimal:
    return Decimal(value)


def at(i: int) -> datetime:
    return C0 + i * HOUR


def close(i: int) -> Decimal:
    return Decimal(100 + (i * 7) % 11) + Decimal("0.25")


def candle_row(i: int) -> dict[str, object]:
    c = close(i)
    return {
        "exchange": "mexc",
        "symbol": BTC,
        "timeframe": "1h",
        "ts": at(i),
        "open": c - 1,
        "high": c + 2,
        "low": c - 2,
        "close": c,
        "volume": d("3.5"),
    }


SIGNAL = {
    "action": "buy",
    "ts": "2026-10-26T08:00:00Z",
    "reason": "fast EMA crossed above slow EMA",
    "indicators": {
        "prev_fast_ema": 101.5,
        "prev_slow_ema": 102.25,
        "fast_ema": 103.125,
        "slow_ema": 102.75,
    },
    "size_hint": None,
    "stop": None,
}
CONFIG = {"id": 1, "strategy": "ema_crossover", "version": 1, "params": PARAMS}
BUY_REASON = {
    "signal": SIGNAL,
    "bot": {"id": 1, "name": "alpha"},
    "strategy_config": CONFIG,
    "sizing": {"settings": {"mode": "all_in"}, "detail": "all the free cash: 0.6 BTC"},
    "stops": {"settings": {"stop_loss": {"kind": "percent", "percent": "2"}}, "atr": None},
    "risk": {
        "decision": "resized",
        "rule": "max_coin_exposure",
        "detail": "shrunk to 25% of equity",
        "requested_amount": "0.6",
    },
}
STOP_REASON = {
    "exit": {
        "kind": "stop_loss",
        "attempt": 1,
        "bid": "101.5",
        "price_ts": "2026-10-26T09:30:00Z",
        "levels": {
            "stop": "101.5",
            "take_profit": None,
            "stop_loss": "101.5",
            "peak": "104",
            "atr": None,
        },
    },
    "bot": {"id": 1, "name": "alpha"},
    "strategy_config": CONFIG,
    "risk": {"decision": "allowed", "rule": "reduces_exposure", "detail": "a sell"},
}
KILL_REASON = {
    "exit": {
        "kind": "kill_switch",
        "attempt": 2,
        "bid": "100",
        "price_ts": "2026-10-26T10:15:00Z",
        "command": "c0ffee",
    },
    "bot": {"id": 1, "name": "alpha"},
    "risk": {"decision": "allowed", "rule": "reduces_exposure", "detail": "a sell"},
}


def order(
    bot_id: int, coid: str, side: str, amount: str, filled: str, status: str, reason: object
) -> dict[str, object]:
    return {
        "bot_id": bot_id,
        "mode": "paper",
        "exchange": "mexc",
        "symbol": BTC,
        "client_order_id": coid,
        "side": side,
        "type": "limit",
        "price": d("104"),
        "amount": d(amount),
        "filled": d(filled),
        "status": status,
        "reason": reason,
    }


def fill(order_id: int, ts: datetime, price: str, amount: str) -> dict[str, object]:
    return {
        "order_id": order_id,
        "ts": ts,
        "price": d(price),
        "amount": d(amount),
        "fee": d(price) * d(amount) / 1000,
        "fee_currency": "USDT",
    }


@pytest.fixture
async def seeded(api: Api) -> Api:
    async with api.db.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " risk_events, candles RESTART IDENTITY"
            )
        )
        bot = {"exchange": "mexc", "symbol": BTC, "timeframe": "1h", "mode": "paper"}
        await conn.execute(
            insert(BotRow),
            [
                {**bot, "name": "alpha", "status": "running"},
                {**bot, "name": "beta", "status": "stopped"},
            ],
        )
        await conn.execute(
            insert(StrategyConfigRow).values(
                bot_id=1, strategy="ema_crossover", version=1, params=PARAMS, created_by="test"
            )
        )
        await conn.execute(update(BotRow).where(BotRow.id == 1).values(active_strategy_config_id=1))
        await conn.execute(insert(CandleRow), [candle_row(i) for i in range(30)])
        await conn.execute(
            insert(OrderRow),
            [
                order(1, "b1-202610260800-b", "buy", "0.5", "0.5", "closed", BUY_REASON),
                order(1, "b1-202610260800-x1", "sell", "0.5", "0.5", "closed", STOP_REASON),
                order(1, "b1-202610260800-x2", "sell", "0.5", "0.2", "canceled", KILL_REASON),
                order(1, "b1-202610261000-b", "buy", "0.5", "0", "canceled", BUY_REASON),
                order(2, "b2-202610260800-b", "buy", "1", "1", "closed", {}),
            ],
        )
        await conn.execute(
            insert(FillRow),
            [
                fill(1, at(27) + timedelta(minutes=1), "104", "0.2"),
                fill(1, at(27) + timedelta(minutes=2), "103", "0.3"),
                fill(2, at(27) + timedelta(minutes=30), "101.4", "0.5"),
                fill(3, at(28) + timedelta(minutes=15), "99.9", "0.2"),
                fill(5, at(27), "104", "1"),
            ],
        )
        await conn.execute(
            insert(RiskEventRow).values(
                ts=at(27),
                mode="paper",
                bot_id=1,
                client_order_id="b1-202610260800-b",
                rule="max_coin_exposure",
                decision="resized",
                detail="shrunk to 25% of equity",
                inputs={"account": {"equity": "1000"}, "settings": {"max_coin_exposure_pct": "25"}},
            )
        )
    return api


async def set_forming(api: Api, i: int, close_: str) -> None:
    forming = FormingCandle(
        exchange="mexc",
        symbol=BTC,
        timeframe="1h",
        ts=at(i),
        open=d("100"),
        high=d("106"),
        low=d("99"),
        close=d(close_),
        volume=d("1.25"),
        received_at=T0 - timedelta(seconds=1),
    )
    await api.redis.set(forming_key("mexc", BTC, "1h"), forming.model_dump_json())


async def get_json(api: Api, path: str) -> Any:
    await api.login()
    response = await api.client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


def expected_points(first: int, last: int) -> list[dict[str, object]]:
    """The points the strategy's own windows give for candles first..last."""
    lines = chart_lines("ema_crossover", PARAMS)
    closes = [float(close(i)) for i in range(30)]
    values = line_points(lines, [at(i) for i in range(30)], closes, HOUR)
    return [
        {"ts": at(i).isoformat().replace("+00:00", "Z"), "values": values[i]}
        for i in range(first, last + 1)
        if values[i] is not None
    ]


async def test_the_chart_has_the_newest_candles_and_the_strategy_s_lines(seeded: Api) -> None:
    body = await get_json(seeded, "/bots/1/chart?limit=20")

    assert body["server_time"] == "2026-10-26T12:00:05Z"
    assert body["bot"] == {
        "id": 1,
        "name": "alpha",
        "exchange": "mexc",
        "symbol": BTC,
        "timeframe": "1h",
        "status": "running",
        "strategy": "ema_crossover",
        "version": 1,
        "params": PARAMS,
    }
    candles = body["candles"]
    assert [c["ts"] for c in candles] == [
        at(i).isoformat().replace("+00:00", "Z") for i in range(10, 30)
    ]
    assert isinstance(candles[-1]["close"], str)  # money is a string (D9)
    assert d(candles[-1]["close"]) == close(29)
    indicators = body["indicators"]
    assert (indicators["pane"], indicators["names"], indicators["guides"]) == (
        "price",
        ["fast_ema", "slow_ema"],
        {},
    )
    # Candle 10's window reaches back to candle 1, before the first one shown.
    assert indicators["points"] == expected_points(10, 29)


async def test_candles_too_early_for_a_whole_window_have_no_point(seeded: Api) -> None:
    body = await get_json(seeded, "/bots/1/chart?limit=30")

    assert len(body["candles"]) == 30
    points = body["indicators"]["points"]
    assert points[0]["ts"] == "2026-10-25T15:00:00Z"  # candle 9, the 10th
    assert points == expected_points(0, 29)


async def test_the_forming_candle_only_while_it_s_newer_than_the_last_closed(
    seeded: Api,
) -> None:
    await set_forming(seeded, 30, "105.5")
    body = await get_json(seeded, "/bots/1/chart")
    forming = body["forming"]
    assert (forming["ts"], d(forming["close"])) == ("2026-10-26T12:00:00Z", d("105.5"))
    assert forming["received_at"] == "2026-10-26T12:00:04Z"

    await set_forming(seeded, 29, "105.5")  # its last state, from before it closed
    assert (await seeded.client.get("/bots/1/chart")).json()["forming"] is None


async def test_trades_are_the_bot_s_filled_orders_at_their_first_fill(seeded: Api) -> None:
    body = await get_json(seeded, "/bots/1/chart")

    trades = body["trades"]
    assert [(t["order_id"], t["side"], t["kind"], t["status"]) for t in trades] == [
        (1, "buy", "signal", "closed"),
        (2, "sell", "stop_loss", "closed"),
        (3, "sell", "kill_switch", "canceled"),
    ]
    buy = trades[0]
    assert buy["client_order_id"] == "b1-202610260800-b"
    assert buy["ts"] == "2026-10-26T09:01:00Z"
    assert d(buy["price"]) == d("103.4")  # (104 x 0.2 + 103 x 0.3) / 0.5
    assert d(buy["amount"]) == d("0.5")
    assert d(trades[2]["amount"]) == d("0.2")  # what filled before the cancel


async def test_trades_before_the_first_candle_shown_are_left_out(seeded: Api) -> None:
    body = await get_json(seeded, "/bots/1/chart?limit=1")  # candle 29 only

    assert [t["order_id"] for t in body["trades"]] == []


async def test_a_bot_whose_lines_can_t_be_drawn_still_has_its_candles(seeded: Api) -> None:
    async with seeded.db.begin() as conn:
        await conn.execute(
            update(StrategyConfigRow).values(strategy="martingale")  # no chart lines for it
        )
    body = await get_json(seeded, "/bots/1/chart")

    assert body["indicators"] is None
    assert len(body["candles"]) == 30


async def test_an_unknown_bot_or_limit_out_of_range(seeded: Api) -> None:
    await seeded.login()
    assert (await seeded.client.get("/bots/99/chart")).status_code == 404
    assert (await seeded.client.get("/bots/1/chart?limit=0")).status_code == 422
    assert (await seeded.client.get("/bots/1/chart?limit=5001")).status_code == 422


async def test_a_buy_s_explanation_has_its_signal_risk_check_and_fills(seeded: Api) -> None:
    body = await get_json(seeded, "/orders/1/explain")

    assert (body["order_id"], body["bot_id"], body["side"], body["status"]) == (
        1,
        1,
        "buy",
        "closed",
    )
    assert body["signal"] == SIGNAL
    assert body["strategy_config"] == CONFIG
    assert body["sizing"] == BUY_REASON["sizing"]
    assert body["stops"] == {
        "settings": {"stop_loss": {"kind": "percent", "percent": "2"}},
        "atr": None,
    }
    assert body["exit"] is None
    assert body["risk"] == {
        "decision": "resized",
        "rule": "max_coin_exposure",
        "detail": "shrunk to 25% of equity",
        "requested_amount": "0.6",
    }
    [event] = body["risk_events"]
    assert event["inputs"]["account"] == {"equity": "1000"}
    assert [(f["ts"], d(f["price"]), d(f["amount"])) for f in body["fills"]] == [
        ("2026-10-26T09:01:00Z", d("104"), d("0.2")),
        ("2026-10-26T09:02:00Z", d("103"), d("0.3")),
    ]
    assert body["reason"] == BUY_REASON  # all of it, as stored


async def test_an_exit_s_explanation_has_its_stop_and_levels(seeded: Api) -> None:
    body = await get_json(seeded, "/orders/2/explain")

    assert body["signal"] is None
    assert body["sizing"] is None
    assert body["exit"]["kind"] == "stop_loss"
    assert d(body["exit"]["bid"]) == d("101.5")
    assert body["exit"]["levels"]["take_profit"] is None
    assert d(body["exit"]["levels"]["peak"]) == d("104")
    assert body["risk"]["rule"] == "reduces_exposure"
    assert body["risk_events"] == []

    kill = (await seeded.client.get("/orders/3/explain")).json()
    assert (kill["exit"]["kind"], kill["exit"]["command"]) == ("kill_switch", "c0ffee")


async def test_a_reason_part_that_doesn_t_fit_is_only_in_the_raw_reason(seeded: Api) -> None:
    odd = {"signal": {"action": "buy"}, "risk": BUY_REASON["risk"], "note": "from long ago"}
    async with seeded.db.begin() as conn:
        await conn.execute(update(OrderRow).where(OrderRow.id == 1).values(reason=odd))
    body = await get_json(seeded, "/orders/1/explain")

    assert body["signal"] is None
    assert body["risk"]["decision"] == "resized"
    assert body["reason"] == odd


async def test_an_unknown_order_has_no_explanation(seeded: Api) -> None:
    await seeded.login()
    assert (await seeded.client.get("/orders/99/explain")).status_code == 404


def sign_in(client: TestClient, api: Api) -> dict[str, str]:
    """Sign in; returns the headers that carry the session to a WebSocket (as in
    test_overview.py: the TestClient won't send a Secure cookie to wss://)."""
    body = {"username": "andy", "password": PASSWORD, "code": api.code()}
    assert client.post("/auth/login", json=body).status_code == 204
    return {"cookie": f"session={client.cookies['session']}"}


def receive_until(socket: Any, done: Any) -> dict[str, Any]:
    """The first message `done` accepts, among the next 30 (feed_seconds is 0.1 in tests)."""
    for _ in range(30):
        message: dict[str, Any] = socket.receive_json()
        if done(message):
            return message
    raise AssertionError("no such message")


async def test_the_websocket_sends_new_candles_forming_and_trades(seeded: Api, db_url: URL) -> None:
    await set_forming(seeded, 30, "105.5")
    app = create_app(seeded.settings, database_url=db_url, clock=seeded.clock)
    after = "2026-10-26T11:00:00Z"  # candle 29, the newest the browser has
    since = "2026-10-25T16:00:00Z"  # candle 10, its oldest
    with TestClient(app, base_url="https://testserver") as client:
        session = sign_in(client, seeded)
        url = f"/ws/bots/1/chart?after={after}&since={since}"
        with client.websocket_connect(url, headers=session) as socket:
            first = socket.receive_json()
            assert first["candles"] == [] and first["points"] == []
            assert d(first["forming"]["close"]) == d("105.5")
            # Trades are sent first, then only when they change.
            assert [t["order_id"] for t in first["trades"]] == [1, 2, 3]
            assert socket.receive_json()["trades"] is None

            # Candle 30 closes: stored, then the forming one is a candle later.
            async with seeded.db.begin() as conn:
                await conn.execute(insert(CandleRow).values(candle_row(30)))
            await set_forming(seeded, 31, "99.5")
            closed = receive_until(socket, lambda m: m["candles"])
            assert [c["ts"] for c in closed["candles"]] == ["2026-10-26T12:00:00Z"]
            [point] = closed["points"]
            lines = chart_lines("ema_crossover", PARAMS)
            window = [float(close(i)) for i in range(21, 31)]
            assert point == {"ts": "2026-10-26T12:00:00Z", "values": lines.at(window)}
            assert closed["forming"]["ts"] == "2026-10-26T13:00:00Z"
            # The cursor moved on: candle 30 isn't sent again.
            assert socket.receive_json()["candles"] == []

            async with seeded.db.begin() as conn:
                await conn.execute(
                    update(OrderRow).where(OrderRow.id == 4).values(filled=d("0.1"), status="open")
                )
                await conn.execute(insert(FillRow).values(fill(4, at(30), "99", "0.1")))
            changed = receive_until(socket, lambda m: m["trades"] is not None)
            assert [t["order_id"] for t in changed["trades"]] == [1, 2, 3, 4]


async def test_the_websocket_closes_for_an_unknown_bot_and_after_sign_out(
    seeded: Api, db_url: URL
) -> None:
    app = create_app(seeded.settings, database_url=db_url, clock=seeded.clock)
    with TestClient(app, base_url="https://testserver") as client:
        session = sign_in(client, seeded)
        with (
            pytest.raises(WebSocketDisconnect) as unknown,
            client.websocket_connect("/ws/bots/99/chart", headers=session) as socket,
        ):
            socket.receive_json()
        assert unknown.value.code == 4404  # not 1008, which would send the browser to sign in

        with client.websocket_connect("/ws/bots/1/chart", headers=session) as socket:
            socket.receive_json()
            assert client.post("/auth/logout").status_code == 204
            with pytest.raises(WebSocketDisconnect) as closed:
                for _ in range(20):
                    socket.receive_json()
    assert closed.value.code == 1008
