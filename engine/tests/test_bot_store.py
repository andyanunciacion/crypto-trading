"""Bots in the database: created with their first config, loaded only when the engine can
run them. Against a real TimescaleDB (see shared.testing)."""

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import URL, insert, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from engine.bots.store import create_bot, load_running_bots, open_orders
from engine.risk.sizing import AllIn
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from engine.strategies.rsi_mean_reversion import RsiMeanReversion, RsiMeanReversionParams
from shared.db.models import BotRow, OrderRow, StrategyConfigRow
from shared.db.session import make_engine

BTC = "BTC/USDT"


@pytest.fixture
async def db(db_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances"
                " RESTART IDENTITY"
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
        assert await load_running_bots(db, "mexc") == []
    return [(log["bot_id"], log["reason"]) for log in logs if log["event"] == "bot.refused"]


async def test_a_created_bot_loads_with_its_strategy(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)

    (bot,) = await load_running_bots(db, "mexc")

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


async def test_a_bot_with_bad_sizing_is_refused(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)
    # Edited by hand, say: a percentage over 100 never reaches an order.
    await set_bot(db, bot_id, sizing={"mode": "percent_of_equity", "percent": "150"})

    ((refused, reason),) = await refusals(db)
    assert refused == bot_id and "less than or equal to 100" in reason


async def test_names_are_unique(db: AsyncEngine) -> None:
    await ema_bot(db)

    with pytest.raises(IntegrityError):
        await ema_bot(db)


@pytest.mark.parametrize("status", ["paused", "stopped"])
async def test_only_running_bots_load(db: AsyncEngine, status: str) -> None:
    await set_bot(db, await ema_bot(db), status=status)

    assert await load_running_bots(db, "mexc") == []


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


async def test_a_config_from_an_older_strategy_version_is_refused(db: AsyncEngine) -> None:
    bot_id = await ema_bot(db)
    async with db.begin() as conn:
        await conn.execute(update(StrategyConfigRow).values(version=0))

    assert await refusals(db) == [(bot_id, "ema_crossover is now version 1, not version 0")]


async def test_one_bad_bot_doesnt_stop_the_others(db: AsyncEngine) -> None:
    bad = await ema_bot(db, "bad")
    async with db.begin() as conn:
        await conn.execute(update(StrategyConfigRow).values(params={"fast": 30, "slow": 21}))
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
        bots = await load_running_bots(db, "mexc")

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
