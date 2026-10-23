"""Replays closed candles through a strategy and simulates its trades (ARCHITECTURE §7, D29).

The fill model:
- A signal on a candle fills at the *next* candle's open. The strategy only sees a
  candle once it has closed, so filling at that close would be look-ahead bias: trading
  at a price that was gone by the time the signal existed.
- Slippage moves every fill price against us; the fee is a rate on each fill's value,
  in the quote currency.
- Spot, long only, one position at a time. Until the RiskManager sizes orders (P4), a
  BUY spends all the cash (times the signal's `size_hint`) and a SELL sells everything.
- Amounts round down to the market's step. Orders below its minimums are skipped,
  never rounded up (D22).

Pure and synchronous: no database, no exchange, no clock. Same inputs, same result.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise

import structlog

from engine.backtest.models import (
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    Fill,
    SkippedOrder,
)
from engine.exchange.models import Candle, MarketRules, OrderSide, Timeframe
from engine.marketdata.timeframes import STEP
from engine.strategies.base import Position, Signal, Strategy, StrategyParams

# Stored with every run. Bump it when the fill model changes, so old runs aren't
# "reproduced" by different rules.
BACKTESTER_VERSION = 1

log = structlog.get_logger()


class BacktestError(Exception):
    """The run can't go ahead: bad or incomplete candles, or a config that doesn't fit."""


def fill_price(side: OrderSide, open_: Decimal, slippage_rate: Decimal) -> Decimal:
    """Buys pay more than the open, sells get less."""
    if side == "buy":
        return open_ * (1 + slippage_rate)
    return open_ * (1 - slippage_rate)


def fee(price: Decimal, amount: Decimal, fee_rate: Decimal) -> Decimal:
    return price * amount * fee_rate


def buy_amount(
    cash: Decimal, price: Decimal, fee_rate: Decimal, fraction: Decimal, step: Decimal
) -> Decimal:
    """How much to buy with `fraction` of `cash`, leaving room for the fee.

    Rounded down to the amount step, so cost + fee never exceeds the cash.
    """
    return cash * fraction / (price * (1 + fee_rate)) // step * step


def _side(signal: Signal) -> OrderSide:
    return "buy" if signal.action == "buy" else "sell"  # callers never pass a HOLD


def below_minimum(amount: Decimal, price: Decimal, rules: MarketRules) -> str | None:
    """Why an order of `amount` at `price` is too small for the market, or None if it isn't."""
    if amount == 0:
        return f"size rounds to 0 at amount step {rules.amount_step}"
    if rules.min_amount is not None and amount < rules.min_amount:
        return f"amount {amount} below min_amount {rules.min_amount}"
    if rules.min_cost is not None and amount * price < rules.min_cost:
        return f"value {amount * price} below min_cost {rules.min_cost}"
    return None


@dataclass
class _Account:
    """The simulated balances, and the record of what happened to them."""

    config: BacktestConfig
    cash: Decimal
    position: Position | None = None
    fills: list[Fill] = field(default_factory=list)
    skipped: list[SkippedOrder] = field(default_factory=list)
    total_fees: Decimal = Decimal(0)

    @property
    def amount(self) -> Decimal:
        return Decimal(0) if self.position is None else self.position.amount

    def skip(self, signal: Signal, side: OrderSide, reason: str) -> None:
        self.skipped.append(SkippedOrder(signal_ts=signal.ts, side=side, reason=reason))
        log.debug("backtest.skipped", signal_ts=signal.ts.isoformat(), side=side, reason=reason)

    def execute(self, signal: Signal, candle: Candle) -> None:
        """Fill `signal` (from the previous candle) at `candle`'s open, if it can be."""
        side = _side(signal)
        price = fill_price(side, candle.open, self.config.slippage_rate)
        rules = self.config.market_rules
        if side == "buy":
            if self.position is not None:
                self.skip(signal, side, "already holding")
                return
            fraction = Decimal(1) if signal.size_hint is None else signal.size_hint
            amount = buy_amount(self.cash, price, self.config.fee_rate, fraction, rules.amount_step)
        else:
            if self.position is None:
                self.skip(signal, side, "nothing to sell")
                return
            amount = self.position.amount
        too_small = below_minimum(amount, price, rules)
        if too_small is not None:
            self.skip(signal, side, too_small)
            return

        paid = fee(price, amount, self.config.fee_rate)
        if side == "buy":
            self.cash -= price * amount + paid
            self.position = Position(amount=amount, avg_entry=price)
        else:
            self.cash += price * amount - paid
            self.position = None
        self.total_fees += paid
        fill = Fill(
            ts=candle.ts, side=side, price=price, amount=amount, fee=paid, cash_after=self.cash
        )
        self.fills.append(fill)
        log.debug("backtest.fill", **fill.model_dump(mode="json"))


def run_backtest[P: StrategyParams](
    strategy: Strategy[P], candles: Sequence[Candle], config: BacktestConfig
) -> BacktestResult:
    """Run `strategy` over `candles`: its warm-up candles, then the range in `config`.

    `candles` must be contiguous (no gaps), with at least `warmup_candles` before
    `config.start` and the last one opening one step before `config.end`.
    """
    _check_strategy(strategy, config)
    first = _first_in_range(candles, config, strategy.warmup_candles)
    window = strategy.warmup_candles
    account = _Account(config=config, cash=config.initial_cash)
    signals: list[Signal] = []
    equity_curve: list[EquityPoint] = []
    pending: Signal | None = None

    # Starts on the last warm-up candle: its signal fills at the first open in range.
    for i in range(first - 1, len(candles)):
        candle = candles[i]
        if i >= first:
            if pending is not None:
                account.execute(pending, candle)
                pending = None
            equity_curve.append(
                EquityPoint(ts=candle.ts, equity=account.cash + account.amount * candle.close)
            )
        # Only the last `window` candles: all on_candle uses (D28), and it keeps a long
        # run from copying an ever-growing history on every candle.
        signal = strategy.on_candle(candles[max(0, i + 1 - window) : i + 1], account.position)
        if signal.action == "hold":
            continue
        signals.append(signal)
        if i == len(candles) - 1:
            account.skip(signal, _side(signal), "signal on the last candle: no next candle")
        else:
            pending = signal

    last_close = candles[-1].close
    return BacktestResult(
        signals=signals,
        fills=account.fills,
        skipped=account.skipped,
        equity_curve=equity_curve,
        final_cash=account.cash,
        final_amount=account.amount,
        final_equity=account.cash + account.amount * last_close,
        total_fees=account.total_fees,
    )


def _check_strategy[P: StrategyParams](strategy: Strategy[P], config: BacktestConfig) -> None:
    """The config is what gets stored, so it must describe the strategy that really ran."""
    if strategy.name != config.strategy:
        raise BacktestError(f"config is for {config.strategy!r}, strategy is {strategy.name!r}")
    if strategy.version != config.strategy_version:
        raise BacktestError(
            f"config is for {config.strategy} version {config.strategy_version}, "
            f"strategy is version {strategy.version}"
        )
    params = strategy.params.model_dump(mode="json")
    if params != config.params:
        raise BacktestError(f"config params {config.params} aren't the strategy's {params}")


def check_contiguous(candles: Sequence[Candle], timeframe: Timeframe) -> None:
    """Refuse an empty list, or one with a candle missing anywhere."""
    if not candles:
        raise BacktestError("no candles")
    step = STEP[timeframe]
    gaps = [(a.ts, b.ts) for a, b in pairwise(candles) if b.ts - a.ts != step]
    if gaps:
        # A fill can't jump a gap to an unrelated price later on, and missing candles
        # aren't made up (D25). Fill the gap or pick a range without it.
        listed = ", ".join(f"{a:%Y-%m-%d %H:%M} to {b:%Y-%m-%d %H:%M}" for a, b in gaps[:5])
        raise BacktestError(f"{len(gaps)} gap(s) in the candles (UTC): {listed}")


def _first_in_range(candles: Sequence[Candle], config: BacktestConfig, warmup: int) -> int:
    """The index of the candle at `config.start`, after checking the candles fit the run."""
    check_contiguous(candles, config.timeframe)
    step = STEP[config.timeframe]
    first = (config.start - candles[0].ts) // step
    needed = max(warmup, 1)
    if first < needed:
        raise BacktestError(
            f"needs {needed} warm-up candles before start ({config.start:%Y-%m-%d %H:%M} UTC), "
            f"has {max(first, 0)}"
        )
    expected_last = config.end - step
    if candles[-1].ts != expected_last:
        raise BacktestError(
            f"candles end at {candles[-1].ts:%Y-%m-%d %H:%M} UTC, but the range ends at "
            f"{config.end:%Y-%m-%d %H:%M} UTC"
        )
    return first
