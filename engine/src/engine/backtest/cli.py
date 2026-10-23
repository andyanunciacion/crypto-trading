"""What the backtest CLIs share: their market and cost arguments, and a config built from them.

Used by run, sweep and walk_forward, so all three build runs the same way.
"""

import argparse
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, get_args

from engine.backtest.models import (
    DEFAULT_FEE_RATE,
    DEFAULT_INITIAL_CASH,
    BacktestConfig,
    default_slippage,
)
from engine.backtest.simulator import BacktestError
from engine.exchange.adapter import ExchangeAdapter
from engine.exchange.models import Timeframe
from engine.marketdata.store import CandleStore, Series
from engine.marketdata.timeframes import STEP, floor
from engine.strategies.base import Strategy


def decimal_arg(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None


def json_object_arg(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"not valid JSON: {e}") from None
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("must be a JSON object, like '{\"fast\": 9}'")
    return value


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    """The symbol, timeframe and range, and the costs: everything but the strategy."""
    parser.add_argument("symbol", help="CCXT format, e.g. BTC/USDT")
    parser.add_argument("timeframe", choices=get_args(Timeframe))
    parser.add_argument("--days", type=int, default=365, help="how far back (default 365)")
    parser.add_argument(
        "--cash", type=decimal_arg, default=DEFAULT_INITIAL_CASH, help="starting quote currency"
    )
    parser.add_argument(
        "--fee-rate",
        type=decimal_arg,
        default=DEFAULT_FEE_RATE,
        help=f"fee per fill, as a fraction (default {DEFAULT_FEE_RATE} = 0.1%%)",
    )
    parser.add_argument(
        "--slippage",
        type=decimal_arg,
        help="slippage per fill, as a fraction (default 0.0005 for BTC/ETH, 0.003 otherwise)",
    )
    parser.add_argument("--exchange", default="mexc")


async def run_settings(
    args: argparse.Namespace, candles: CandleStore, strategy: Strategy[Any]
) -> BacktestConfig:
    """A config for `strategy` over the last `--days`, ending at the last stored candle.

    The market's step sizes and minimums come from the exchange's public data.
    """
    series = Series(args.exchange, args.symbol, args.timeframe)
    bounds = await candles.bounds(series)
    if bounds is None:
        raise BacktestError(f"no candles stored for {series}; run the backfill first")
    end = bounds.last + STEP[args.timeframe]
    # Public data only: no key, trading off (D22).
    async with ExchangeAdapter(args.exchange) as exchange:
        rules = await exchange.market_rules(args.symbol)
    return BacktestConfig(
        strategy=strategy.name,
        strategy_version=strategy.version,
        params=strategy.params.model_dump(mode="json"),
        exchange=args.exchange,
        symbol=args.symbol,
        timeframe=args.timeframe,
        start=floor(end - timedelta(days=args.days), args.timeframe),
        end=end,
        initial_cash=args.cash,
        fee_rate=args.fee_rate,
        slippage_rate=default_slippage(args.symbol) if args.slippage is None else args.slippage,
        market_rules=rules,
    )
