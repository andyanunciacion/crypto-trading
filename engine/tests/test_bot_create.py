"""The create CLI: a running, funded paper bot, or nothing at all.

Against a real TimescaleDB (see shared.testing). The exchange is the offline MEXC
replay (conftest.py), so the symbol check runs through CCXT's real market data.
"""

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import URL, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.bots.create import create_paper_bot, parse_args
from engine.bots.store import load_running_bots
from engine.exchange.adapter import ExchangeAdapter
from engine.exchange.errors import ExchangeError
from engine.exchange.models import Balance
from engine.risk.paper import PaperDesk
from engine.risk.sizing import ALL_IN, FixedAmount, PercentOfEquity, Sizing
from engine.risk.stops import NO_STOPS, Percent, Stops
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from shared.db.models import BotRow
from shared.db.session import make_engine
from shared.prices import PriceUpdate

EMA = EmaCrossover(EmaCrossoverParams(fast=9, slow=21))


class NoQuotes:
    async def latest(self, exchange: str, symbol: str) -> PriceUpdate | None:
        return None


@pytest.fixture
async def db(db_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " risk_events RESTART IDENTITY"
            )
        )
    yield engine
    await engine.dispose()


async def bot_count(db: AsyncEngine) -> int:
    async with db.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(BotRow))).scalar_one()


async def create(
    db: AsyncEngine,
    adapter: ExchangeAdapter,
    symbol: str = "BTC/USDT",
    cash: str = "500",
    sizing: Sizing = ALL_IN,
    stops: Stops = NO_STOPS,
) -> int:
    return await create_paper_bot(
        db,
        PaperDesk(db, NoQuotes(), adapter),
        adapter,
        name="btc-ema",
        strategy=EMA,
        symbol=symbol,
        timeframe="1h",
        cash=Decimal(cash),
        sizing=sizing,
        stops=stops,
    )


async def test_creates_a_running_funded_paper_bot(
    db: AsyncEngine, adapter: ExchangeAdapter
) -> None:
    bot_id = await create(db, adapter)

    (bot,) = await load_running_bots(db, "mexc")
    assert (bot.id, bot.name, bot.symbol, bot.timeframe) == (bot_id, "btc-ema", "BTC/USDT", "1h")
    assert bot.strategy.params == EMA.params
    assert bot.sizing == ALL_IN  # the default: as before P4
    desk = PaperDesk(db, NoQuotes(), adapter)
    cash = Decimal(500)
    assert await desk.risk.balances(bot_id) == {
        "USDT": Balance(currency="USDT", free=cash, used=Decimal(0), total=cash)
    }


async def test_a_bot_keeps_its_sizing(db: AsyncEngine, adapter: ExchangeAdapter) -> None:
    await create(db, adapter, sizing=PercentOfEquity(percent=Decimal(25)))

    (bot,) = await load_running_bots(db, "mexc")
    assert bot.sizing == PercentOfEquity(percent=Decimal(25))


async def test_a_bot_keeps_its_stops(db: AsyncEngine, adapter: ExchangeAdapter) -> None:
    stops = Stops(stop_loss=Percent(percent=Decimal(3)))
    await create(db, adapter, stops=stops)

    (bot,) = await load_running_bots(db, "mexc")
    assert bot.stops == stops


async def test_a_symbol_the_exchange_doesnt_list_creates_nothing(
    db: AsyncEngine, adapter: ExchangeAdapter
) -> None:
    with pytest.raises(ExchangeError):
        await create(db, adapter, symbol="NOPE/USDT")

    assert await bot_count(db) == 0


@pytest.mark.parametrize("cash", ["0", "-5"])
async def test_no_cash_creates_nothing(
    db: AsyncEngine, adapter: ExchangeAdapter, cash: str
) -> None:
    with pytest.raises(ValueError, match="cash must be positive"):
        await create(db, adapter, cash=cash)

    assert await bot_count(db) == 0


def test_arguments() -> None:
    args = parse_args(["btc-ema", "ema_crossover", "BTC/USDT", "1h", "--params", '{"fast": 5}'])

    assert (args.name, args.strategy, args.symbol, args.timeframe) == (
        "btc-ema",
        "ema_crossover",
        "BTC/USDT",
        "1h",
    )
    assert (args.params, args.cash, args.exchange) == ({"fast": 5}, Decimal(1000), "mexc")
    assert args.sizing == ALL_IN
    assert args.stops == NO_STOPS


def test_the_stops_argument_is_checked() -> None:
    stopped = ["btc-ema", "ema_crossover", "BTC/USDT", "1h", "--stops"]
    assert parse_args([*stopped, '{"trailing": {"kind": "percent", "percent": 2}}']).stops == Stops(
        trailing=Percent(percent=Decimal(2))
    )
    for bad in ('{"take_profit": {"kind": "r", "multiple": 2}}', '{"stoploss": {}}', "not json"):
        with pytest.raises(SystemExit):
            parse_args([*stopped, bad])


def test_the_sizing_argument_is_checked() -> None:
    sized = ["btc-ema", "ema_crossover", "BTC/USDT", "1h", "--sizing"]
    assert parse_args([*sized, '{"mode": "fixed", "amount": "50"}']).sizing == FixedAmount(
        amount=Decimal(50)
    )
    # argparse exits 2 on a bad argument, before anything is created.
    for bad in ('{"mode": "fixed"}', '{"mode": "yolo"}', "not json"):
        with pytest.raises(SystemExit):
            parse_args([*sized, bad])
