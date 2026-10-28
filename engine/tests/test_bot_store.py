"""Bots in the database: created with their first config, loaded only when the engine can
run them (running or paused). Against a real TimescaleDB (see shared.testing)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import URL, insert, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from engine.bots.store import create_bot, held_symbols, load_bots, open_orders
from engine.risk.sizing import AllIn
from engine.risk.stops import NO_STOPS, AtrMultiple, RMultiple, Stops
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from engine.strategies.rsi_mean_reversion import RsiMeanReversion, RsiMeanReversionParams
from shared.db.models import BotRow, OrderRow, PositionRow, StrategyConfigRow, StrategySwitchRow
from shared.db.session import make_engine

BTC = "BTC/USDT"


@pytest.fixture
async def db(db_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " risk_events, strategy_switches RESTART IDENTITY"
            )
        )
    yield engine
    await engine.dispose()


async def ema_bot(db: AsyncEngine, name: str = "btc-ema") -> int:
    return await create_bot(
        db,
        name=name,
        exchange="mexc",
        symbol=BTC,
        timeframe="1h",
        strategy=EmaCrossover(EmaCrossoverParams(fast=9, slow=21)),
        created_by="test",
    )


async def set_bot(db: AsyncEngine, bot_id: int, **values: object) -> None:
    async with db.begin() as conn:
        await conn.execute(update(BotRow).where(BotRow.id == bot_id).values(**values))


async def refusals(db: AsyncEngine) -> list[tuple[int, str]]:
    with capture_logs() as logs:
        assert await load_bots(db, "mexc") == []
    return [(log["bot_id"], log["reason"]) for log in logs if log["event"] == "bot.refused"]


async def test_a_created_bot_loads_with_its_strategy(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)

    (bot,) = await load_bots(db, "mexc")

    assert (bot.id, bot.name, bot.exchange, bot.symbol, bot.timeframe) == (
        bot_id,
        "btc-ema",
        "mexc",
        BTC,
        "1h",
    )
    assert isinstance(bot.strategy, EmaCrossover)
    assert bot.strategy.params == EmaCrossoverParams(fast=9, slow=21)
    async with db.connect() as conn:
        config = (await conn.execute(text("SELECT * FROM strategy_configs"))).mappings().one()
        row = (await conn.execute(text("SELECT * FROM bots"))).mappings().one()
    assert bot.strategy_config_id == config["id"] == row["active_strategy_config_id"]
    assert (config["strategy"], config["version"], config["params"], config["created_by"]) == (
        "ema_crossover",
        1,
        {"fast": 9, "slow": 21},
        "test",
    )
    assert (row["mode"], row["status"], row["switch_policy"]) == ("paper", "running", "keep")
    assert (bot.sizing, row["sizing"]) == (AllIn(), {"mode": "all_in"})
    assert (bot.stops, row["stops"]) == (NO_STOPS, NO_STOPS_JSON)  # none, as before P4-2b


NO_STOPS_JSON = {"stop_loss": None, "take_profit": None, "trailing": None, "atr_period": 14}


async def test_a_bot_with_bad_sizing_is_refused(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)
    # Edited by hand, say: a percentage over 100 never reaches an order.
    await set_bot(db, bot_id, sizing={"mode": "percent_of_equity", "percent": "150"})

    ((refused, reason),) = await refusals(db)
    assert refused == bot_id and "less than or equal to 100" in reason


async def test_a_bot_keeps_its_stops(db: AsyncEngine) -> None:
    stops = Stops(
        stop_loss=AtrMultiple(multiple=Decimal(2)), take_profit=RMultiple(multiple=Decimal(2))
    )
    await create_bot(
        db,
        name="btc-ema",
        exchange="mexc",
        symbol=BTC,
        timeframe="1h",
        strategy=EmaCrossover(EmaCrossoverParams(fast=9, slow=21)),
        created_by="test",
        stops=stops,
    )

    (bot,) = await load_bots(db, "mexc")
    assert bot.stops == stops


async def test_a_bot_with_bad_stops_is_refused(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)
    # An R take-profit with no stop-loss to measure R by.
    await set_bot(db, bot_id, stops={"take_profit": {"kind": "r", "multiple": 2}})

    ((refused, reason),) = await refusals(db)
    assert refused == bot_id and "needs a stop_loss" in reason


async def test_names_are_unique(db: AsyncEngine) -> None:
    await ema_bot(db)

    with pytest.raises(IntegrityError):
        await ema_bot(db)


@pytest.mark.parametrize(
    "change",
    [
        'UPDATE strategy_configs SET params = \'{"fast": 5, "slow": 21}\'',
        "DELETE FROM strategy_configs",
    ],
)
async def test_a_config_never_changes(db: AsyncEngine, change: str) -> None:
    # Append-only, in the database itself (migration 0013, D52): a change adds a row.
    bot_id = await ema_bot(db)

    with pytest.raises(DBAPIError, match="strategy_configs is append-only: add a row instead"):
        async with db.begin() as conn:
            await conn.execute(text(change))

    added = await new_config(db, bot_id, version=1, params={"fast": 5, "slow": 21})
    async with db.connect() as conn:
        rows = (
            await conn.execute(text("SELECT id, params FROM strategy_configs ORDER BY id"))
        ).all()
    assert [tuple(r) for r in rows] == [
        (1, {"fast": 9, "slow": 21}),
        (added, {"fast": 5, "slow": 21}),
    ]


@pytest.mark.parametrize(
    "change", ["UPDATE strategy_switches SET reason = 'edited'", "DELETE FROM strategy_switches"]
)
async def test_a_recorded_switch_never_changes(db: AsyncEngine, change: str) -> None:
    # Append-only like the configs (migration 0014, P6-4).
    bot_id = await ema_bot(db)
    added = await new_config(db, bot_id, version=1, params={"fast": 5, "slow": 21})
    async with db.begin() as conn:
        await conn.execute(
            insert(StrategySwitchRow).values(
                bot_id=bot_id,
                from_config=1,
                to_config=added,
                trigger="manual",
                policy="keep",
                reason="switched by hand (test)",
                source="test",
            )
        )

    with pytest.raises(DBAPIError, match="strategy_switches is append-only: add a row instead"):
        async with db.begin() as conn:
            await conn.execute(text(change))


AUTO_SWITCH: dict[str, Any] = {
    "mapping": {
        "trending_up": {"strategy": "ema_crossover", "version": 1, "params": {"fast": 9}},
        "trending_down": {"strategy": "stay_flat", "version": 1, "params": {}},
        "ranging": {"strategy": "rsi_mean_reversion", "version": 1, "params": {}},
        "high_volatility": {"strategy": "stay_flat", "version": 1, "params": {}},
    },
    "min_hold": 4,
    "policy": "hand_over",
}


async def test_a_bot_loads_with_its_auto_switching(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)
    await set_bot(db, bot_id, auto_switch=AUTO_SWITCH)

    (bot,) = await load_bots(db, "mexc")

    assert bot.auto is not None
    assert (bot.auto.settings.min_hold, bot.auto.settings.policy) == (4, "hand_over")
    assert bot.auto.targets["ranging"].name == "rsi_mean_reversion"
    assert bot.auto.window == 199 + 3


async def test_an_auto_switch_mapping_that_no_longer_builds_is_turned_off(
    db: AsyncEngine,
) -> None:
    # As if RSI had moved on to version 2 since the mapping was saved: the bot runs on, on
    # the config it has, without switching.
    bot_id = await ema_bot(db)
    mapping = {**AUTO_SWITCH["mapping"]}
    mapping["ranging"] = {"strategy": "rsi_mean_reversion", "version": 99, "params": {}}
    await set_bot(db, bot_id, auto_switch={**AUTO_SWITCH, "mapping": mapping})

    with capture_logs() as logs:
        (bot,) = await load_bots(db, "mexc")

    assert (bot.id, bot.auto) == (bot_id, None)
    (invalid,) = [log for log in logs if log["event"] == "bot.auto_switch_invalid"]
    assert invalid["reason"] == "rsi_mean_reversion is now version 1, not version 99"


async def test_stopped_bots_dont_load(db: AsyncEngine) -> None:
    await set_bot(db, await ema_bot(db), status="stopped")

    assert await load_bots(db, "mexc") == []


async def test_paused_bots_load_so_their_positions_stay_watched(db: AsyncEngine) -> None:
    # Its buys are the RiskManager's to block (D42); its sells and stops carry on.
    paused = await ema_bot(db)
    await set_bot(db, paused, status="paused", paused_reason="3 failed orders within 10 min")
    running = await ema_bot(db, "other")

    with capture_logs() as logs:
        bots = await load_bots(db, "mexc")

    assert [b.id for b in bots] == [paused, running]
    (loaded,) = [log for log in logs if log["event"] == "bots.loaded"]
    assert [b["status"] for b in loaded["bots"]] == ["paused", "running"]


async def test_held_symbols_are_every_open_positions_coin_in_one_mode(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)
    await set_bot(db, bot_id, status="stopped")  # a stopped bot's position still counts

    def position(symbol: str, closed: bool = False, mode: str = "paper") -> dict[str, object]:
        return {
            "bot_id": bot_id,
            "mode": mode,
            "exchange": "mexc",
            "symbol": symbol,
            "side": "long",
            "amount": Decimal(0) if closed else Decimal(1),
            "avg_entry": Decimal(100),
            "fees_paid": Decimal(0),
            "opened_at": datetime(2026, 10, 25, tzinfo=UTC),
            "closed_at": datetime(2026, 10, 26, tzinfo=UTC) if closed else None,
            "realized_pnl": Decimal(0),
        }

    async with db.begin() as conn:
        await conn.execute(
            insert(PositionRow),
            [
                position("ETH/USDT"),
                position("SOL/USDT", closed=True),
                position("XRP/USDT", mode="live"),
            ],
        )

    assert await held_symbols(db, mode="paper") == ["ETH/USDT"]
    assert await held_symbols(db, mode="live") == ["XRP/USDT"]


async def test_a_live_bot_is_refused(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)
    await set_bot(db, bot_id, mode="live")

    assert await refusals(db) == [(bot_id, "live bots can't run yet: only paper bots until P9")]


async def test_a_bot_on_another_exchange_is_refused(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)
    await set_bot(db, bot_id, exchange="binance")

    assert await refusals(db) == [(bot_id, "the engine trades on mexc, not binance")]


async def test_a_bot_without_a_config_is_refused(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)
    await set_bot(db, bot_id, active_strategy_config_id=None)

    assert await refusals(db) == [(bot_id, "no active strategy config")]


async def test_a_bot_pointing_at_another_bots_config_is_refused(db: AsyncEngine) -> None:
    first = await ema_bot(db, "first")
    second = await ema_bot(db, "second")
    await set_bot(db, first, status="stopped")
    await set_bot(db, second, active_strategy_config_id=first)  # first's config has id 1

    assert await refusals(db) == [
        (second, f"its active strategy config {first} belongs to bot {first}")
    ]


async def new_config(db: AsyncEngine, bot_id: int, *, version: int, params: object) -> int:
    """Add an EMA crossover config to the bot as no command would, and make it active.

    Configs are append-only (D52): a test can't change one, so it adds one.
    """
    async with db.begin() as conn:
        config_id: int = (
            await conn.execute(
                insert(StrategyConfigRow)
                .values(
                    bot_id=bot_id,
                    strategy="ema_crossover",
                    version=version,
                    params=params,
                    created_by="test",
                )
                .returning(StrategyConfigRow.id)
            )
        ).scalar_one()
    await set_bot(db, bot_id, active_strategy_config_id=config_id)
    return config_id


async def test_a_config_from_an_older_strategy_version_is_refused(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)
    await new_config(db, bot_id, version=0, params={"fast": 9, "slow": 21})

    assert await refusals(db) == [(bot_id, "ema_crossover is now version 1, not version 0")]


async def test_one_bad_bot_doesnt_stop_the_others(db: AsyncEngine) -> None:
    bad = await ema_bot(db, "bad")
    await new_config(db, bad, version=1, params={"fast": 30, "slow": 21})
    good = await create_bot(
        db,
        name="good",
        exchange="mexc",
        symbol=BTC,
        timeframe="1h",
        strategy=RsiMeanReversion(RsiMeanReversionParams()),
        created_by="test",
    )

    with capture_logs() as logs:
        bots = await load_bots(db, "mexc")

    assert [b.id for b in bots] == [good]
    assert [log["bot_id"] for log in logs if log["event"] == "bot.refused"] == [bad]


async def test_open_orders_lists_the_open_ones_in_one_mode(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)
    other = await ema_bot(db, "other")

    def row(cid: str, status: str, bot: int = bot_id, mode: str = "paper") -> dict[str, object]:
        return {
            "bot_id": bot,
            "mode": mode,
            "exchange": "mexc",
            "symbol": BTC,
            "client_order_id": cid,
            "side": "buy",
            "type": "limit",
            "price": Decimal(100),
            "amount": Decimal(1),
            "filled": Decimal(0),
            "status": status,
            "reason": {},
        }

    async with db.begin() as conn:
        await conn.execute(
            insert(OrderRow),
            [
                row("b1-202610231200-b", "open"),
                row("b1-202610231100-b", "closed"),
                row("b1-202610231300-b", "open"),
                row("b2-202610231200-b", "open", other),
                row("b1-202610231400-b", "open", mode="live"),
            ],
        )

    assert await open_orders(db, bot_id, mode="paper") == [
        "b1-202610231200-b",
        "b1-202610231300-b",
    ]
    # Every bot's, as restart recovery cancels them.
    assert await open_orders(db, mode="paper") == [
        "b1-202610231200-b",
        "b1-202610231300-b",
        "b2-202610231200-b",
    ]
    assert await open_orders(db, mode="live") == ["b1-202610231400-b"]
