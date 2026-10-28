"""History (P5-6, D49): every order and every fill, filtered and paged, and their CSV
exports, which must match the database's rows for the same filters.

The expected rows come from raw SQL written here, independent of backend/history/:
Postgres itself turns each value into text (NUMERIC's exact digits, never an exponent;
timestamps in UTC to the microsecond), and every CSV cell must equal it.

alpha trades BTC/USDT and beta ETH/USDT. Their orders, by id:
1. alpha's signal buy (24 Oct), filled in two slices, one with a fee of 0.00000001;
2. alpha's stop-loss sell (25 Oct), filled;
3. alpha's rejected buy (25 Oct);
4. beta's buy at 23:59:59.999999 on 25 Oct, its fills in two currencies (no total fee),
   the second at midnight;
5. beta's kill switch sell at exactly midnight on 26 Oct, partly filled, cancelled;
6. alpha's open buy (26 Oct), unfilled.
"""

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import insert, text

from backend.testing import Api
from shared.db.models import BotRow, FillRow, OrderRow

D24 = datetime(2026, 10, 24, tzinfo=UTC)
D25 = datetime(2026, 10, 25, tzinfo=UTC)
D26 = datetime(2026, 10, 26, tzinfo=UTC)
D27 = datetime(2026, 10, 27, tzinfo=UTC)


def at(day: datetime, hour: int = 0, minute: int = 0, second: int = 0, us: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, second=second, microsecond=us)


SIGNAL = {"signal": {"action": "buy", "reason": "cross"}, "risk": {"decision": "allowed"}}
STOP = {"exit": {"kind": "stop_loss", "attempt": 1}, "risk": {"decision": "allowed"}}
KILL = {"exit": {"kind": "kill_switch", "attempt": 1}, "risk": {"decision": "allowed"}}


def order(
    bot_id: int,
    created: datetime,
    side: str,
    amount: str,
    filled: str,
    status: str,
    reason: object,
    reject_reason: str | None = None,
) -> dict[str, object]:
    return {
        "bot_id": bot_id,
        "created_at": created,
        "updated_at": created,
        "mode": "paper",
        "exchange": "mexc",
        "symbol": "BTC/USDT" if bot_id == 1 else "ETH/USDT",
        "client_order_id": f"o{bot_id}-{created.isoformat()}-{side}",
        "side": side,
        "type": "limit",
        "price": Decimal("104.05" if bot_id == 1 else "2502.5"),
        "amount": Decimal(amount),
        "filled": Decimal(filled),
        "status": status,
        "reject_reason": reject_reason,
        "reason": reason,
    }


def fill(
    order_id: int, ts: datetime, price: str, amount: str, fee: str, currency: str = "USDT"
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "ts": ts,
        "price": Decimal(price),
        "amount": Decimal(amount),
        "fee": Decimal(fee),
        "fee_currency": currency,
    }


@pytest.fixture
async def seeded(api: Api) -> Api:
    async with api.db.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " risk_events, strategy_switches RESTART IDENTITY"
            )
        )
        await conn.execute(
            insert(BotRow),
            [
                {"name": "alpha", "exchange": "mexc", "symbol": "BTC/USDT", "timeframe": "1h"},
                {"name": "beta", "exchange": "mexc", "symbol": "ETH/USDT", "timeframe": "1h"},
            ],
        )
        await conn.execute(
            insert(OrderRow),
            [
                order(1, at(D24, 6), "buy", "0.5", "0.5", "closed", SIGNAL),
                order(1, at(D25, 9, 30), "sell", "0.5", "0.5", "closed", STOP),
                order(1, at(D25, 12), "buy", "0.4", "0", "rejected", SIGNAL, "not enough USDT"),
                order(2, at(D25, 23, 59, 59, 999_999), "buy", "0.2", "0.2", "closed", SIGNAL),
                order(2, D26, "sell", "0.2", "0.05", "canceled", KILL),
                order(1, at(D26, 10), "buy", "0.3", "0", "open", SIGNAL),
            ],
        )
        await conn.execute(
            insert(FillRow),
            [
                fill(1, at(D24, 6, 0, 1), "104", "0.25", "0.026"),
                fill(1, at(D24, 6, 0, 2, 500_000), "104.5", "0.25", "0.00000001"),
                fill(2, at(D25, 9, 30, 1), "101.5", "0.5", "0.05075"),
                fill(4, at(D25, 23, 59, 59, 999_999), "2500", "0.1", "0.0001", "ETH"),
                fill(4, D26, "2501", "0.1", "0.2501"),
                fill(5, at(D26, 0, 0, 1), "2490", "0.05", "0.1245"),
            ],
        )
    await api.login()
    return api


# Postgres's own text for each column, in the CSV's order.
TS = """to_char({} AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"')"""
ORDERS_SQL = f"""
SELECT o.id::text, {TS.format("o.created_at")}, {TS.format("o.updated_at")},
       o.bot_id::text, b.name, o.mode, o.exchange, o.symbol, o.client_order_id, o.side,
       coalesce(o.reason->'exit'->>'kind', 'signal'), o.status,
       o.price::text, o.amount::text, o.filled::text,
       (SELECT trim_scale(sum(f.price * f.amount) / sum(f.amount))::text
          FROM fills f WHERE f.order_id = o.id),
       (SELECT CASE WHEN count(DISTINCT f.fee_currency) = 1 THEN sum(f.fee)::text END
          FROM fills f WHERE f.order_id = o.id),
       (SELECT CASE WHEN count(DISTINCT f.fee_currency) = 1 THEN min(f.fee_currency) END
          FROM fills f WHERE f.order_id = o.id),
       o.reject_reason
  FROM orders o JOIN bots b ON b.id = o.bot_id
 WHERE {{where}}
 ORDER BY o.id DESC
"""
FILLS_SQL = f"""
SELECT f.id::text, {TS.format("f.ts")}, f.order_id::text, o.client_order_id,
       o.bot_id::text, b.name, o.symbol, o.side,
       coalesce(o.reason->'exit'->>'kind', 'signal'),
       f.price::text, f.amount::text, (f.price * f.amount)::text, f.fee::text, f.fee_currency
  FROM fills f JOIN orders o ON o.id = f.order_id JOIN bots b ON b.id = o.bot_id
 WHERE {{where}}
 ORDER BY f.id DESC
"""
ORDER_COLUMNS = [
    "id",
    "created_at",
    "updated_at",
    "bot_id",
    "bot_name",
    "mode",
    "exchange",
    "symbol",
    "client_order_id",
    "side",
    "kind",
    "status",
    "price",
    "amount",
    "filled",
    "avg_fill_price",
    "fees",
    "fee_currency",
    "reject_reason",
]
FILL_COLUMNS = [
    "id",
    "ts",
    "order_id",
    "client_order_id",
    "bot_id",
    "bot_name",
    "symbol",
    "side",
    "kind",
    "price",
    "amount",
    "value",
    "fee",
    "fee_currency",
]


async def expected(api: Api, sql: str, where: str, params: dict[str, object]) -> list[list[str]]:
    async with api.db.connect() as conn:
        rows = (await conn.execute(text(sql.format(where=where)), params)).all()
    return [["" if value is None else value for value in row] for row in rows]


def parse(body: str) -> tuple[list[str], list[list[str]]]:
    header, *rows = list(csv.reader(io.StringIO(body)))
    return header, rows


# (query, the same filter in SQL, its parameters)
ORDER_CASES = [
    ({}, "true", {}),
    (
        {"start": D25.isoformat(), "end": D26.isoformat()},
        "o.created_at >= :start AND o.created_at < :end",
        {"start": D25, "end": D26},
    ),
    (
        {"start": D25.isoformat(), "end": D27.isoformat(), "bot_id": 2},
        "o.created_at >= :start AND o.created_at < :end AND o.bot_id = 2",
        {"start": D25, "end": D27},
    ),
    ({"status": "rejected"}, "o.status = 'rejected'", {}),
    ({"kind": "stop_loss"}, "o.reason->'exit'->>'kind' = 'stop_loss'", {}),
    ({"kind": "signal"}, "o.reason->'exit' IS NULL", {}),
    ({"side": "sell", "symbol": "ETH/USDT"}, "o.side = 'sell' AND o.symbol = 'ETH/USDT'", {}),
]


@pytest.mark.parametrize(("query", "where", "params"), ORDER_CASES)
async def test_the_orders_csv_matches_the_database_rows_for_the_same_filters(
    seeded: Api, query: dict[str, str | int], where: str, params: dict[str, object]
) -> None:
    response = await seeded.client.get("/history/orders.csv", params=query)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == "attachment"
    header, rows = parse(response.text)
    assert header == ORDER_COLUMNS
    assert rows == await expected(seeded, ORDERS_SQL, where, params)
    assert rows  # every case finds something, so a filter that drops all can't pass


FILL_CASES = [
    ({}, "true", {}),
    (
        {"start": D25.isoformat(), "end": D26.isoformat()},
        "f.ts >= :start AND f.ts < :end",
        {"start": D25, "end": D26},
    ),
    ({"bot_id": 1, "side": "buy"}, "o.bot_id = 1 AND o.side = 'buy'", {}),
    ({"kind": "kill_switch"}, "o.reason->'exit'->>'kind' = 'kill_switch'", {}),
]


@pytest.mark.parametrize(("query", "where", "params"), FILL_CASES)
async def test_the_fills_csv_matches_the_database_rows_for_the_same_filters(
    seeded: Api, query: dict[str, str | int], where: str, params: dict[str, object]
) -> None:
    response = await seeded.client.get("/history/fills.csv", params=query)

    assert response.status_code == 200
    header, rows = parse(response.text)
    assert header == FILL_COLUMNS
    assert rows == await expected(seeded, FILLS_SQL, where, params)
    assert rows


async def test_the_csv_writes_exact_digits_and_utc_times(seeded: Api) -> None:
    orders = {
        row[0]: dict(zip(ORDER_COLUMNS, row, strict=True))
        for row in parse((await seeded.client.get("/history/orders.csv")).text)[1]
    }
    fills = {
        row[0]: dict(zip(FILL_COLUMNS, row, strict=True))
        for row in parse((await seeded.client.get("/history/fills.csv")).text)[1]
    }

    assert fills["2"]["fee"] == "0.00000001"  # never 1E-8
    assert orders["1"]["fees"] == "0.02600001"
    assert orders["1"]["avg_fill_price"] == "104.25"
    assert orders["4"]["fees"] == "" and orders["4"]["fee_currency"] == ""  # ETH + USDT
    assert orders["3"]["reject_reason"] == "not enough USDT"
    assert orders["6"]["avg_fill_price"] == ""  # no fills
    assert orders["4"]["created_at"] == "2026-10-25T23:59:59.999999+00:00"
    assert orders["5"]["created_at"] == "2026-10-26T00:00:00.000000+00:00"
    assert fills["2"]["value"] == "26.125"


async def test_an_empty_range_gives_just_the_header(seeded: Api) -> None:
    query = {"start": D27.isoformat(), "end": datetime(2026, 10, 28, tzinfo=UTC).isoformat()}

    for path, columns in (("orders", ORDER_COLUMNS), ("fills", FILL_COLUMNS)):
        response = await seeded.client.get(f"/history/{path}.csv", params=query)
        assert parse(response.text) == (columns, [])


async def test_orders_page_newest_first(seeded: Api) -> None:
    first = await seeded.client.get("/history/orders", params={"limit": 4})
    rest = await seeded.client.get(
        "/history/orders", params={"limit": 4, "before": first.json()[-1]["id"]}
    )

    assert [o["id"] for o in first.json()] == [6, 5, 4, 3]
    assert [o["id"] for o in rest.json()] == [2, 1]
    newest = first.json()[0]
    assert newest["bot_name"] == "alpha" and newest["kind"] == "signal"
    assert newest["avg_fill_price"] is None and newest["status"] == "open"
    # Money as strings (D9), exact.
    kill = first.json()[1]
    assert (kill["kind"], kill["filled"], kill["avg_fill_price"], kill["fees"]) == (
        "kill_switch",
        "0.05",
        "2490",
        "0.1245",
    )


async def test_fills_page_newest_first_with_their_order(seeded: Api) -> None:
    response = await seeded.client.get(
        "/history/fills", params={"limit": 2, "before": 6, "bot_id": 2}
    )

    assert [(f["id"], f["order_id"], f["fee_currency"]) for f in response.json()] == [
        (5, 4, "USDT"),
        (4, 4, "ETH"),
    ]
    assert response.json()[0]["value"] == "250.1"


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/history/orders", {"status": "filled"}),
        ("/history/orders", {"kind": "magic"}),
        ("/history/orders", {"side": "short"}),
        ("/history/orders", {"limit": 501}),
        ("/history/orders", {"start": "2026-10-25T00:00:00"}),  # no time zone
        ("/history/orders", {"start": D26.isoformat(), "end": D25.isoformat()}),
        ("/history/orders.csv", {"limit": 10}),  # an export is every row
        ("/history/fills", {"status": "open"}),  # fills have no status
        ("/history/fills.csv", {"wat": "1"}),
    ],
)
async def test_a_bad_filter_is_a_422(seeded: Api, path: str, query: dict[str, str | int]) -> None:
    assert (await seeded.client.get(path, params=query)).status_code == 422
