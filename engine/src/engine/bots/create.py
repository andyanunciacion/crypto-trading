"""Create a paper bot, give it virtual cash, and set it running.

Run inside the stack (Postgres has no host port):
    docker compose run --rm engine python -m engine.bots.create btc-ema ema_crossover BTC/USDT 1h
Add `--params '{"fast": 9, "slow": 21}'` to change the strategy's settings, `--cash` for
the starting quote currency. The engine loads running bots when it starts, so restart
it afterwards: `docker compose restart engine`.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, get_args

import structlog
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.backtest.cli import decimal_arg, json_object_arg
from engine.backtest.models import DEFAULT_INITIAL_CASH
from engine.bots.store import create_bot
from engine.exchange.adapter import ExchangeAdapter
from engine.exchange.errors import ExchangeError
from engine.exchange.models import Timeframe
from engine.main import configure_logging
from engine.marketdata.quotes import RedisQuotes
from engine.risk.paper import PaperDesk
from engine.strategies.base import Strategy
from engine.strategies.registry import STRATEGIES, build, strategy_class
from shared.config import Settings, load_settings
from shared.db.session import make_engine

log = structlog.get_logger()


async def create_paper_bot(
    db: AsyncEngine,
    desk: PaperDesk,
    exchange: ExchangeAdapter,
    *,
    name: str,
    strategy: Strategy[Any],
    symbol: str,
    timeframe: Timeframe,
    cash: Decimal,
) -> int:
    """Add a running paper bot and fund it with `cash` of the quote currency. Returns its id."""
    if cash <= 0:
        raise ValueError(f"cash must be positive, not {cash}")
    await exchange.market_rules(symbol)  # refuses a symbol the exchange doesn't list
    bot_id = await create_bot(
        db,
        name=name,
        exchange=exchange.exchange_id,
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        created_by="cli",
    )
    _, currency = symbol.split("/")
    await desk.fund(bot_id, currency, cash)
    log.info(
        "bot.created",
        bot_id=bot_id,
        name=name,
        exchange=exchange.exchange_id,
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy.name,
        version=strategy.version,
        params=strategy.params.model_dump(mode="json"),
        cash=str(cash),
        currency=currency,
    )
    return bot_id


async def run(args: argparse.Namespace, settings: Settings) -> None:
    strategy = build(args.strategy, strategy_class(args.strategy).version, args.params)
    db = make_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url)
    try:
        # Public market data only: no key, trading off (D22).
        async with ExchangeAdapter(args.exchange) as exchange:
            desk = PaperDesk(db, RedisQuotes(redis), exchange)
            bot_id = await create_paper_bot(
                db,
                desk,
                exchange,
                name=args.name,
                strategy=strategy,
                symbol=args.symbol,
                timeframe=args.timeframe,
                cash=args.cash,
            )
    finally:
        await redis.aclose()
        await db.dispose()
    print(
        f"Created paper bot {bot_id} ({args.name}): {strategy.name} on {args.symbol} "
        f"{args.timeframe} with {args.cash} {args.symbol.split('/')[1]}.\n"
        "Restart the engine to run it: docker compose restart engine"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="a unique name, e.g. btc-ema")
    parser.add_argument("strategy", choices=sorted(STRATEGIES))
    parser.add_argument("symbol", help="CCXT format, e.g. BTC/USDT")
    parser.add_argument("timeframe", choices=get_args(Timeframe))
    parser.add_argument(
        "--params", type=json_object_arg, default={}, help="strategy params as JSON"
    )
    parser.add_argument(
        "--cash",
        type=decimal_arg,
        default=DEFAULT_INITIAL_CASH,
        help=f"starting paper cash in the quote currency (default {DEFAULT_INITIAL_CASH})",
    )
    parser.add_argument("--exchange", default="mexc")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        asyncio.run(run(args, settings))
    except IntegrityError:
        print(f"create failed: a bot named {args.name!r} already exists", file=sys.stderr)
        raise SystemExit(1) from None
    except (ExchangeError, ValueError) as e:  # ValueError includes pydantic's ValidationError
        print(f"create failed: {e}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
