"""The Logs page's reads (P5-6, D49): the mirrored log lines from the Redis stream, and
the `risk_events` rows.

The stream holds, oldest first: 1 bots.loaded, 2 risk.decision (bot 1), 3 a warning,
4 the API's kill, 5 an error (bot 2), 6 a line that doesn't read, 7 the backtester's
job, 8 risk.repeat (bot 1).
"""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, text

import backend.logs.store as store
from backend.testing import Api
from shared.db.models import BotRow, RiskEventRow
from shared.logs import LOGS_KEY

LINES: list[dict[str, object] | str] = [
    {"event": "bots.loaded", "level": "info", "service": "engine", "count": 2},
    {"event": "risk.decision", "level": "info", "service": "engine", "bot_id": 1},
    {"event": "candles.live.late", "level": "warning", "service": "engine"},
    {"event": "api.kill.sent", "level": "info", "service": "api", "user": "andy"},
    {"event": "bot.error", "level": "error", "service": "engine", "bot_id": 2},
    "not json",
    {"event": "backtest.job.done", "level": "info", "service": "backtester"},
    {"event": "risk.repeat", "level": "info", "service": "engine", "bot_id": 1},
]


@pytest.fixture
async def logged(api: Api) -> Api:
    for n, line in enumerate(LINES, start=1):
        payload = line if isinstance(line, str) else json.dumps(line | {"n": n})
        await api.redis.xadd(LOGS_KEY, {"json": payload}, id=f"1000-{n}")
    await api.login()
    return api


def numbers(page: dict[str, object]) -> list[object]:
    entries = page["entries"]
    assert isinstance(entries, list)
    return [entry["fields"]["n"] for entry in entries]


async def test_the_lines_page_back_newest_first_skipping_what_does_not_read(
    logged: Api,
) -> None:
    pages = []
    before: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 3} | ({"before": before} if before else {})
        page = (await logged.client.get("/logs", params=params)).json()
        pages.append(numbers(page))
        before = page["next"]
        if before is None:
            break

    assert pages == [[8, 7, 5], [4, 3, 2], [1]]


async def test_an_entry_has_its_parts(logged: Api) -> None:
    newest = (await logged.client.get("/logs", params={"limit": 1})).json()

    assert newest["entries"] == [
        {
            "id": "1000-8",
            "ts": None,
            "service": "engine",
            "level": "info",
            "event": "risk.repeat",
            "bot_id": 1,
            "fields": {"n": 8},
        }
    ]
    assert newest["next"] == "1000-8"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"level": "warning"}, [5, 3]),
        ({"level": "error"}, [5]),
        ({"event": "risk."}, [8, 2]),
        ({"bot_id": 1}, [8, 2]),
        ({"service": "api"}, [4]),
        ({"service": "engine", "level": "info", "event": "b"}, [5, 1]),
    ],
)
async def test_the_lines_filter(
    logged: Api, params: dict[str, str | int], expected: list[int]
) -> None:
    page = (await logged.client.get("/logs", params=params)).json()

    assert numbers(page) == expected
    assert page["next"] is None


async def test_an_event_may_start_with_any_of_several_prefixes(logged: Api) -> None:
    page = (await logged.client.get("/logs", params={"event": ["risk.", "api."]})).json()

    assert numbers(page) == [8, 4, 2]


async def test_filtering_carries_on_across_batches(
    logged: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "SCAN_BATCH", 2)

    page = (await logged.client.get("/logs", params={"bot_id": 1, "limit": 1})).json()
    rest = (await logged.client.get("/logs", params={"bot_id": 1, "before": page["next"]})).json()

    assert (numbers(page), numbers(rest), rest["next"]) == ([8], [2], None)


async def test_an_empty_stream_has_no_lines(api: Api) -> None:
    await api.login()

    assert (await api.client.get("/logs")).json() == {"entries": [], "next": None}


@pytest.mark.parametrize(
    "params",
    [{"level": "debug"}, {"before": "nope"}, {"limit": 0}, {"service": "db"}, {"x": 1}],
)
async def test_a_bad_log_filter_is_a_422(logged: Api, params: dict[str, str | int]) -> None:
    assert (await logged.client.get("/logs", params=params)).status_code == 422


@pytest.fixture
async def risky(api: Api) -> Api:
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

        def event(bot_id: int, day: int, rule: str, decision: str) -> dict[str, object]:
            return {
                "ts": datetime(2026, 10, day, 3, tzinfo=UTC),
                "mode": "paper",
                "bot_id": bot_id,
                "client_order_id": f"b{bot_id}-{day}",
                "rule": rule,
                "decision": decision,
                "detail": f"{rule} on the {day}th",
                "inputs": {"order": {"amount": "0.5"}, "settings": {"max_open_positions": 3}},
            }

        await conn.execute(
            insert(RiskEventRow),
            [
                event(1, 24, "max_open_positions", "blocked"),
                event(2, 25, "max_coin_exposure", "resized"),
                event(1, 26, "stale_data", "blocked"),
            ],
        )
    await api.login()
    return api


async def test_risk_events_newest_first_with_their_bot_and_inputs(risky: Api) -> None:
    events = (await risky.client.get("/risk-events")).json()

    assert [e["id"] for e in events] == [3, 2, 1]
    assert events[1] | {"ts": None} == {
        "id": 2,
        "ts": None,
        "mode": "paper",
        "bot_id": 2,
        "bot_name": "beta",
        "client_order_id": "b2-25",
        "rule": "max_coin_exposure",
        "decision": "resized",
        "detail": "max_coin_exposure on the 25th",
        "inputs": {"order": {"amount": "0.5"}, "settings": {"max_open_positions": 3}},
    }


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"decision": "blocked"}, [3, 1]),
        ({"bot_id": 1}, [3, 1]),
        ({"rule": "stale_data"}, [3]),
        ({"start": "2026-10-25T00:00:00Z", "end": "2026-10-26T03:00:00Z"}, [2]),
        ({"limit": 1, "before": 3}, [2]),
    ],
)
async def test_risk_events_filter_and_page(
    risky: Api, params: dict[str, str | int], expected: list[int]
) -> None:
    events = (await risky.client.get("/risk-events", params=params)).json()

    assert [e["id"] for e in events] == expected


@pytest.mark.parametrize(
    "params",
    [{"decision": "maybe"}, {"start": "2026-10-26T00:00:00Z", "end": "2026-10-25T00:00:00Z"}],
)
async def test_a_bad_risk_event_filter_is_a_422(risky: Api, params: dict[str, str]) -> None:
    assert (await risky.client.get("/risk-events", params=params)).status_code == 422
