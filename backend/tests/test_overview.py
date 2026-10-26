"""The Overview (`GET /overview`, `WS /ws/overview`): the account, positions, bots,
prices and the engine's heartbeat, read from the database and Redis (D45).

The same account as engine/tests/test_risk_account.py, so the numbers can be checked
against the RiskManager's. The API's clock is T0, 20:00 in Manila on 2026-10-26, whose
midnight was 16:00 UTC on the 25th.
- alpha (running, ema_crossover): 500 USDT, and 5 BTC bought at 100 (0.5 fee so far),
  with a stop at 85 and a take-profit at 120. The bid is 90.
- beta (paused by the error breaker): 1100 USDT, 95.095 of it held by an open buy. Two
  closed trades: +150 just before midnight in Manila, -50 just after.
- gamma (stopped by the kill switch): 1000 USDT.

Cash 2600. Equity 2600 + 5 x 90 = 3050. Deposits 2600 + 500 at cost - 99.5 realized =
3000.5. Peak 3150.5 (after the +150), and so is midnight's equity.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, insert, text, update
from starlette.websockets import WebSocketDisconnect

from backend.app import create_app
from backend.testing import PASSWORD, T0, Api
from shared.db.models import BotRow, PaperBalanceRow, PositionRow, StrategyConfigRow
from shared.heartbeat import ENGINE_HEARTBEAT_KEY, EngineHeartbeat
from shared.prices import PriceUpdate, price_key

MIDNIGHT = T0.replace(day=25, hour=16, minute=0, second=0)  # in Manila
BTC = "BTC/USDT"


def d(value: str) -> Decimal:
    return Decimal(value)


def decimals(fields: dict[str, Any], *other: str) -> dict[str, Decimal | None]:
    """The money fields (all but `other`) as Decimals: 3050.000 and 3050 are equal."""
    return {
        key: None if value is None else d(value)
        for key, value in fields.items()
        if key not in other
    }


def position(bot_id: int, amount: str, pnl: str, **fields: object) -> dict[str, object]:
    return {
        "bot_id": bot_id,
        "mode": "paper",
        "exchange": "mexc",
        "symbol": BTC,
        "side": "long",
        "amount": d(amount),
        "avg_entry": d("100"),
        "fees_paid": d("0.5"),
        "opened_at": T0 - timedelta(days=1),
        "closed_at": None,
        "realized_pnl": d(pnl),
        # Every row with every key: an insert of many rows takes its columns from the first.
        "stop": None,
        "take_profit": None,
        **fields,
    }


@pytest.fixture
async def seeded(api: Api) -> Api:
    async with api.db.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " risk_events RESTART IDENTITY"
            )
        )
        # Every row with every key: an insert of many rows takes its columns from the first.
        bot = {
            "exchange": "mexc",
            "symbol": BTC,
            "timeframe": "1h",
            "mode": "paper",
            "paused_reason": None,
            "paused_at": None,
            "stopped_reason": None,
            "stopped_at": None,
        }
        await conn.execute(
            insert(BotRow),
            [
                {**bot, "name": "alpha", "status": "running"},
                {
                    **bot,
                    "name": "beta",
                    "status": "paused",
                    "paused_reason": "3 failed orders in 10 min",
                    "paused_at": T0 - timedelta(hours=1),
                },
                {
                    **bot,
                    "name": "gamma",
                    "status": "stopped",
                    "stopped_reason": "kill switch from cli (command x)",
                    "stopped_at": T0 - timedelta(hours=2),
                },
            ],
        )
        await conn.execute(
            insert(StrategyConfigRow).values(
                bot_id=1, strategy="ema_crossover", version=1, params={}, created_by="test"
            )
        )
        await conn.execute(update(BotRow).where(BotRow.id == 1).values(active_strategy_config_id=1))
        await conn.execute(
            insert(PaperBalanceRow),
            [
                {"bot_id": 1, "currency": "USDT", "free": d("500"), "used": d("0")},
                {"bot_id": 1, "currency": "BTC", "free": d("5"), "used": d("0")},  # not cash
                {"bot_id": 2, "currency": "USDT", "free": d("1004.905"), "used": d("95.095")},
                {"bot_id": 3, "currency": "USDT", "free": d("1000"), "used": d("0")},
            ],
        )
        await conn.execute(
            insert(PositionRow),
            [
                position(1, "5", "-0.5", stop=d("85"), take_profit=d("120")),
                position(2, "0", "150", closed_at=MIDNIGHT - timedelta(minutes=1)),
                position(2, "0", "-50", closed_at=MIDNIGHT + timedelta(minutes=1)),
            ],
        )
    return api


async def set_price(api: Api, symbol: str, bid: str, age_s: float) -> None:
    ts = T0 - timedelta(seconds=age_s)
    price = PriceUpdate(
        exchange="mexc",
        symbol=symbol,
        bid=d(bid),
        ask=d(bid) + 1,
        ts=ts,
        received_at=ts,
        source="ws",
    )
    await api.redis.set(price_key("mexc", symbol), price.model_dump_json())


async def set_heartbeat(api: Api, *symbols: str) -> None:
    beat = EngineHeartbeat(
        ts=T0 - timedelta(seconds=4),
        started_at=T0 - timedelta(hours=3),
        mode="paper",
        beat=1080,
        exchange="mexc",
        symbols=list(symbols),
    )
    await api.redis.set(ENGINE_HEARTBEAT_KEY, beat.model_dump_json())


async def get_overview(api: Api) -> dict[str, Any]:
    await api.login()
    response = await api.client.get("/overview")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def test_the_account_matches_the_risk_manager_s(seeded: Api) -> None:
    await set_price(seeded, BTC, "90", age_s=2)
    body = await get_overview(seeded)

    account = body["account"]
    assert (account["mode"], account["currency"]) == ("paper", "USDT")
    # Money is a string, never a JSON number (D9).
    assert isinstance(account["equity"], str)
    assert decimals(account, "mode", "currency") == {
        "equity": d("3050"),
        "deposits": d("3000.5"),
        "today_pnl": d("-100.5"),  # 3050 - 3150.5 at midnight in Manila
        "today_pnl_pct": d("-3.19"),
        "total_pnl": d("49.5"),
        "total_pnl_pct": d("1.65"),
        "peak": d("3150.5"),
        "drawdown_pct": d("3.19"),
    }


async def test_positions_show_their_value_and_p_and_l_at_the_bid(seeded: Api) -> None:
    await set_price(seeded, BTC, "90", age_s=2)
    body = await get_overview(seeded)

    [held] = body["positions"]
    labels = ("bot_name", "exchange", "symbol", "price_ts", "opened_at")
    assert {key: held[key] for key in labels} == {
        "bot_name": "alpha",
        "exchange": "mexc",
        "symbol": BTC,
        "price_ts": "2026-10-26T12:00:03Z",
        "opened_at": "2026-10-25T12:00:05Z",
    }
    assert decimals(held, "bot_id", *labels) == {
        "amount": d("5"),
        "avg_entry": d("100"),
        "bid": d("90"),
        "value": d("450"),
        "pnl": d("-50.5"),  # the 0.5 fee, plus 5 x (90 - 100)
        "pnl_pct": d("-10.10"),  # of the 500 it cost
        "stop": d("85"),
        "take_profit": d("120"),
    }


async def test_a_coin_never_priced_is_valued_at_its_entry(seeded: Api) -> None:
    body = await get_overview(seeded)

    [held] = body["positions"]
    assert (held["bid"], held["price_ts"]) == (None, None)
    assert (d(held["value"]), d(held["pnl"])) == (d("500"), d("-0.5"))
    assert d(body["account"]["equity"]) == d("3100")
    assert body["prices"] == [
        {"exchange": "mexc", "symbol": BTC, "bid": None, "ask": None, "ts": None}
    ]


async def test_every_bot_with_its_status_and_why(seeded: Api) -> None:
    body = await get_overview(seeded)

    assert [(b["name"], b["status"], b["strategy"]) for b in body["bots"]] == [
        ("alpha", "running", "ema_crossover"),
        ("beta", "paused", None),
        ("gamma", "stopped", None),
    ]
    beta, gamma = body["bots"][1:]
    assert beta["paused_reason"] == "3 failed orders in 10 min"
    assert gamma["stopped_reason"] == "kill switch from cli (command x)"
    assert gamma["stopped_at"] == "2026-10-26T10:00:05Z"


async def test_the_engine_s_heartbeat_and_the_prices_it_streams(seeded: Api) -> None:
    body = await get_overview(seeded)
    assert body["engine"] is None  # it has never beaten

    await set_heartbeat(seeded, BTC, "ETH/USDT")
    await set_price(seeded, BTC, "90", age_s=2)
    body = (await seeded.client.get("/overview")).json()

    assert body["server_time"] == "2026-10-26T12:00:05Z"
    assert body["mode"] == "paper"
    assert body["engine"]["ts"] == "2026-10-26T12:00:01Z"
    assert body["engine"]["symbols"] == [BTC, "ETH/USDT"]
    # The streamed ones, then the coins held (BTC, already listed).
    assert [(p["symbol"], p["ts"]) for p in body["prices"]] == [
        (BTC, "2026-10-26T12:00:03Z"),
        ("ETH/USDT", None),
    ]


async def test_an_empty_account(api: Api) -> None:
    async with api.db.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " risk_events RESTART IDENTITY"
            )
        )
    body = await get_overview(api)

    assert d(body["account"]["equity"]) == 0
    assert body["account"]["today_pnl_pct"] is None  # no division by zero
    assert d(body["account"]["drawdown_pct"]) == 0
    assert (body["positions"], body["bots"], body["prices"]) == ([], [], [])


def sign_in(client: TestClient, api: Api) -> dict[str, str]:
    """Sign in; returns the headers that carry the session to a WebSocket.

    The TestClient's cookie jar won't send a Secure cookie to a wss:// URL (it only
    counts https as secure), though a browser does: so it's passed by hand.
    """
    body = {"username": "andy", "password": PASSWORD, "code": api.code()}
    assert client.post("/auth/login", json=body).status_code == 204
    return {"cookie": f"session={client.cookies['session']}"}


async def test_the_websocket_sends_the_overview_again_and_again(seeded: Api, db_url: URL) -> None:
    await set_price(seeded, BTC, "90", age_s=2)
    # Its own app, so the TestClient runs its lifespan (TestClient speaks WebSocket).
    app = create_app(seeded.settings, database_url=db_url, clock=seeded.clock)
    with TestClient(app, base_url="https://testserver") as client:
        session = sign_in(client, seeded)
        with client.websocket_connect("/ws/overview", headers=session) as socket:
            first = socket.receive_json()
            await set_price(seeded, BTC, "95", age_s=1)
            # The next one or so (feed_seconds is 0.1 in tests) shows the new bid.
            for _ in range(20):
                later = socket.receive_json()
                if later["positions"][0]["bid"] == "95":
                    break

    assert d(first["account"]["equity"]) == d("3050")
    assert d(later["account"]["equity"]) == d("3075")  # 5 BTC at 95


async def test_signing_out_closes_the_websocket(seeded: Api, db_url: URL) -> None:
    app = create_app(seeded.settings, database_url=db_url, clock=seeded.clock)
    with TestClient(app, base_url="https://testserver") as client:
        session = sign_in(client, seeded)
        with client.websocket_connect("/ws/overview", headers=session) as socket:
            socket.receive_json()
            # Ends every session, this socket's too (its cookie was read at the handshake).
            assert client.post("/auth/logout").status_code == 204
            with pytest.raises(WebSocketDisconnect) as closed:
                for _ in range(20):
                    socket.receive_json()

    assert closed.value.code == 1008
