"""What the backtest CLIs share: their market and cost arguments, and a config built from them.

Used by run, sweep and walk_forward, so all three build runs the same way.
"""

import argparse
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, get_args

from pydantic import ValidationError

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
from engine.risk.sizing import ALL_IN, SIZING, Sizing
from engine.risk.stops import NO_STOPS, STOPS, Stops
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


def sizing_arg(text: str) -> Sizing:
    """A sizing mode as JSON, e.g. '{"mode": "percent_of_equity", "percent": 25}'."""
    try:
        return SIZING.validate_python(json_object_arg(text))
    except ValidationError as e:
        raise argparse.ArgumentTypeError(f"bad sizing: {_problems(e)}") from None


def stops_arg(text: str) -> Stops:
    """Stops as JSON, e.g. '{"stop_loss": {"kind": "percent", "percent": 3}}'."""
    try:
        return STOPS.validate_python(json_object_arg(text))
    except ValidationError as e:
        raise argparse.ArgumentTypeError(f"bad stops: {_problems(e)}") from None


def _problems(e: ValidationError) -> str:
    # One line per problem, without pydantic's links to its docs.
    return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())


SIZING_HELP = (
    "how much each buy spends, as JSON (default all-in): "
    '{"mode": "fixed", "amount": 100}, {"mode": "percent_of_equity", "percent": 25} or '
    '{"mode": "atr_risk", "risk_percent": 1, "period": 14, "multiple": 2}'
)
STOPS_HELP = (
    "stops, as JSON (default none): any of stop_loss and trailing "
    '({"kind": "percent", "percent": 3} or {"kind": "atr", "multiple": 2}) and take_profit '
    '({"kind": "percent", "percent": 6} or {"kind": "r", "multiple": 2}), plus atr_period '
    '(default 14), e.g. {"stop_loss": {"kind": "atr", "multiple": 2}, '
    '"take_profit": {"kind": "r", "multiple": 2}}'
)


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
    parser.add_argument("--sizing", type=sizing_arg, default=ALL_IN, help=SIZING_HELP)
    parser.add_argument("--stops", type=stops_arg, default=NO_STOPS, help=STOPS_HELP)
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
        sizing=args.sizing,
        stops=args.stops,
    )
