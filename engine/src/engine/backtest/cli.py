"""What the backtest CLIs share: their market and cost arguments, and a config built from them.

Used by run, sweep and walk_forward, and by the backtester service for the dashboard
(worker.py, D48), so they all build runs the same way.
"""

import argparse
import json
import math
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engine.backtest.models import (
    DEFAULT_FEE_RATE,
    DEFAULT_INITIAL_CASH,
    BacktestConfig,
    default_slippage,
)
from engine.backtest.simulator import BacktestError, warmup_needed
from engine.exchange.adapter import ExchangeAdapter
from engine.exchange.models import MarketRules, Timeframe
from engine.marketdata.store import CandleStore, Series
from engine.marketdata.timeframes import STEP, floor
from engine.regime.switcher import MappingRefused, with_versions
from engine.risk.sizing import ALL_IN, SIZING, Sizing
from engine.risk.stops import NO_STOPS, STOPS, Stops
from engine.strategies.base import Strategy
from shared.regime import AutoSwitchSettings


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


class BadAutoSwitch(ValueError):
    """Auto-switching settings that can't run, and why: the CLI's and the dashboard's words."""


def checked_auto_switch(settings: AutoSwitchSettings) -> AutoSwitchSettings:
    """`settings` with each regime's config checked and stored at its strategy's version
    now (regime/switcher.py), as a bot's are (`bots.auto_switch`). Used by `--auto-switch`
    and the backtester service (worker.py), so both refuse alike."""
    try:
        return with_versions(settings)
    except MappingRefused as e:
        cause = e.cause
        detail = _problems(cause) if isinstance(cause, ValidationError) else str(cause)
        raise BadAutoSwitch(f"bad auto-switch: {e.regime}: {detail}") from None


def auto_switch_arg(text: str) -> AutoSwitchSettings:
    """Auto-switching as JSON (`checked_auto_switch`)."""
    try:
        return checked_auto_switch(AutoSwitchSettings.model_validate(json_object_arg(text)))
    except ValidationError as e:
        raise argparse.ArgumentTypeError(f"bad auto-switch: {_problems(e)}") from None
    except BadAutoSwitch as e:
        raise argparse.ArgumentTypeError(str(e)) from None


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
AUTO_SWITCH_HELP = (
    "switch strategy as the regime changes, as an auto-switching bot does (default off; "
    "the strategy above is the one it starts on), as JSON: every regime mapped, plus "
    "min_hold (default 3) and policy (keep, close_first or hand_over; default keep), e.g. "
    '{"mapping": {"trending_up": {"strategy": "ema_crossover", "params": {"fast": 9, '
    '"slow": 21}}, "ranging": {"strategy": "rsi_mean_reversion"}, "trending_down": '
    '{"strategy": "stay_flat"}, "high_volatility": {"strategy": "stay_flat"}}, "min_hold": 3}'
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


class RunSettings(BaseModel):
    """A run's market, range and costs: everything but the strategy. What
    `add_run_arguments` reads, and what the dashboard's Backtests page sends (D48), so
    both build their runs with `build_config`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    timeframe: Timeframe
    days: int = Field(default=365, ge=1)
    cash: Decimal = DEFAULT_INITIAL_CASH
    fee_rate: Decimal = DEFAULT_FEE_RATE
    slippage: Decimal | None = None  # None: default_slippage(symbol)
    sizing: Sizing = ALL_IN
    stops: Stops = NO_STOPS
    exchange: str = "mexc"
    # The run CLI's --auto-switch and the dashboard's (P6-5c), after `checked_auto_switch`.
    auto_switch: AutoSwitchSettings | None = None


class MarketRulesSource(Protocol):
    async def market_rules(self, symbol: str) -> MarketRules: ...


async def build_config(
    settings: RunSettings,
    candles: CandleStore,
    strategy: Strategy[Any],
    exchange: MarketRulesSource,
) -> BacktestConfig:
    """A config for `strategy` over the last `days`, ending at the last stored candle.

    The market's step sizes and minimums come from the exchange's public data.
    """
    series = Series(settings.exchange, settings.symbol, settings.timeframe)
    bounds = await candles.bounds(series)
    if bounds is None:
        raise BacktestError(f"no candles stored for {series}; run the backfill first")
    end = bounds.last + STEP[settings.timeframe]
    rules = await exchange.market_rules(settings.symbol)
    slippage = settings.slippage
    config = BacktestConfig(
        strategy=strategy.name,
        strategy_version=strategy.version,
        params=strategy.params.model_dump(mode="json"),
        exchange=settings.exchange,
        symbol=settings.symbol,
        timeframe=settings.timeframe,
        start=floor(end - timedelta(days=settings.days), settings.timeframe),
        end=end,
        initial_cash=settings.cash,
        fee_rate=settings.fee_rate,
        slippage_rate=default_slippage(settings.symbol) if slippage is None else slippage,
        market_rules=rules,
        sizing=settings.sizing,
        stops=settings.stops,
        auto_switch=settings.auto_switch,
    )
    _check_history(config, warmup_needed(strategy, config), series, bounds.first)
    return config


class NotEnoughHistory(BacktestError):
    """The stored candles don't reach back far enough for the range plus its warm-up.

    `max_days` is the longest range that fits what's stored (None if not even a day).
    """

    def __init__(self, message: str, max_days: int | None) -> None:
        super().__init__(message)
        self.max_days = max_days


def _check_history(config: BacktestConfig, warmup: int, series: Series, first: datetime) -> None:
    """Refuse a range whose warm-up would start before the first stored candle, saying
    how many days do fit, and how far back a backfill must reach for this one."""
    step = STEP[config.timeframe]
    earliest = first + warmup * step  # the earliest start with a whole warm-up before it
    if config.start >= earliest:
        return
    fits = (config.end - earliest).days  # whole days, as --days counts them
    max_days = fits if fits >= 1 else None
    reach = config.end - (config.start - warmup * step)
    backfill = math.ceil(reach / timedelta(days=1))
    advice = f"backfill more (--days {backfill})"
    raise NotEnoughHistory(
        f"needs {warmup} warm-up candles before the start ({config.start:%Y-%m-%d %H:%M} UTC),"
        f" but the stored {series.exchange} {series.symbol} {series.timeframe} candles start"
        f" at {first:%Y-%m-%d %H:%M} UTC: "
        + (advice if max_days is None else f"use at most {max_days} days, or {advice}"),
        max_days,
    )


class PublicRules:
    """The exchange, opened only once the rules are asked for (each time, so they're
    fresh): a series with no candles fails before anything goes over the network."""

    def __init__(self, exchange_id: str) -> None:
        self._exchange_id = exchange_id

    async def market_rules(self, symbol: str) -> MarketRules:
        # Public data only: no key, trading off (D22).
        async with ExchangeAdapter(self._exchange_id) as exchange:
            return await exchange.market_rules(symbol)


async def run_settings(
    args: argparse.Namespace,
    candles: CandleStore,
    strategy: Strategy[Any],
    rules: MarketRulesSource | None = None,
) -> BacktestConfig:
    """`build_config` from the command line's arguments (`add_run_arguments`). The market
    rules come from the exchange unless `rules` is given (tests)."""
    settings = RunSettings(
        symbol=args.symbol,
        timeframe=args.timeframe,
        days=args.days,
        cash=args.cash,
        fee_rate=args.fee_rate,
        slippage=args.slippage,
        sizing=args.sizing,
        stops=args.stops,
        exchange=args.exchange,
        auto_switch=getattr(args, "auto_switch", None),  # run's option, not sweep's
    )
    return await build_config(settings, candles, strategy, rules or PublicRules(args.exchange))
