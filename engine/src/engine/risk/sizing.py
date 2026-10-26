"""Position sizing: how much a bot buys when it enters (ARCHITECTURE §6, D39).

Pure, all Decimal: the backtester and the bot runner both call `entry_amount`, so a
paper bot sizes its orders exactly as its backtests did (D7). The RiskManager's limits
(P4-3) then cap what sizing asks for. Only entries are sized; a SELL sells the whole
position.

The modes, each a `mode` plus its settings (stored per bot as JSON, `bots.sizing`):
- `all_in`: spend all the free cash, leaving room for the fee (the default, and the
  only rule before P4).
- `fixed`: buy a fixed value in the quote currency, e.g. 100 USDT.
- `percent_of_equity`: buy a share of what the bot is worth, cash and coins.
- `atr_risk`: buy as much as would lose `risk_percent` of equity if a stop `multiple` x
  ATR below the entry were hit. ATR (Average True Range) is how far the price typically
  moves in one candle, so the size shrinks when the market gets wilder.

In every mode the signal's `size_hint` scales the size; the amount rounds down to the
market's step and never costs more than the free cash, fee included. An amount below the
market's minimums is refused, never rounded up (D22).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from engine.exchange.models import Candle, MarketRules, Timeframe
from engine.marketdata.timeframes import STEP
from shared.indicators import atr


class _Frozen(BaseModel):
    # Same rules as the other settings: immutable, no unknown fields, no NaN/Infinity.
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class AllIn(_Frozen):
    mode: Literal["all_in"] = "all_in"


class FixedAmount(_Frozen):
    mode: Literal["fixed"] = "fixed"
    amount: Decimal = Field(gt=0, description="value of each buy, in the quote currency")


class PercentOfEquity(_Frozen):
    mode: Literal["percent_of_equity"] = "percent_of_equity"
    percent: Decimal = Field(gt=0, le=100, description="share of the bot's equity per buy")


class AtrRisk(_Frozen):
    mode: Literal["atr_risk"] = "atr_risk"
    risk_percent: Decimal = Field(
        default=Decimal(1), gt=0, le=100, description="equity lost if the stop is hit"
    )
    period: int = Field(default=14, ge=1, description="ATR period, in candles")
    multiple: Decimal = Field(
        default=Decimal(2), gt=0, description="stop distance below the entry, in ATRs"
    )


Sizing = Annotated[AllIn | FixedAmount | PercentOfEquity | AtrRisk, Field(discriminator="mode")]
# Parses and dumps a `Sizing` from JSON or a dict: the `mode` picks the model.
SIZING: TypeAdapter[Sizing] = TypeAdapter(Sizing)
ALL_IN = AllIn()  # the default for bots and backtests


@dataclass(frozen=True)
class Sized:
    amount: Decimal  # in the base currency, on the market's step
    detail: str  # how it came about, for the order's `reason` and the logs


@dataclass(frozen=True)
class NotSized:
    reason: str


def entry_amount(
    sizing: Sizing,
    *,
    cash: Decimal,
    equity: Decimal,
    price: Decimal,
    fee_rate: Decimal,
    rules: MarketRules,
    size_hint: Decimal | None,
    atr: Decimal | None,
) -> Sized | NotSized:
    """How much to buy at `price`, the most a unit can cost (a limit, or a fill price).

    `cash` is the bot's free quote currency, `equity` its cash and coins at today's
    price, `atr` the latest ATR (`atr_for`) when the mode needs one.
    """
    hint = Decimal(1) if size_hint is None else size_hint
    step = rules.amount_step
    affordable = buy_amount(cash, price, fee_rate, Decimal(1), step)
    match sizing:
        case AllIn():
            wanted = buy_amount(cash, price, fee_rate, hint, step)
            detail = f"all_in: {_show(hint)} x {_show(cash)} free cash"
        case FixedAmount(amount=value):
            wanted = _down(value * hint / price, step)
            detail = f"fixed: {_show(value)} at {_show(price)}"
        case PercentOfEquity(percent=percent):
            value = equity * percent / 100 * hint
            wanted = _down(value / price, step)
            detail = (
                f"percent_of_equity: {_show(percent)}% of {_show(equity)} equity"
                f" = {_show(value)} at {_show(price)}"
            )
        case AtrRisk(risk_percent=risk, multiple=multiple):
            if atr is None:
                return NotSized(f"no ATR: needs {lookback(sizing)} candles in a row")
            if atr <= 0:
                return NotSized("ATR is 0: no price movement to set a stop by")
            at_risk = equity * risk / 100 * hint
            wanted = _down(at_risk / (multiple * atr), step)
            detail = (
                f"atr_risk: {_show(risk)}% of {_show(equity)} equity = {_show(at_risk)} at risk"
                f" / ({_show(multiple)} x ATR {_show(atr)})"
            )
    if hint != 1 and not isinstance(sizing, AllIn):
        detail += f", size hint {_show(hint)}"
    detail += f" = {_show(wanted)}"
    amount = wanted
    if wanted > affordable:
        amount = affordable
        detail += f"; capped by cash at {_show(affordable)}"
    too_small = below_minimum(amount, price, rules)
    if too_small is not None:
        # Worded as before P4, so stored backtests that skipped a buy still reproduce.
        return NotSized(too_small)
    return Sized(amount=amount, detail=detail)


def lookback(sizing: Sizing) -> int:
    """How many closed candles the mode needs: 0 unless it uses ATR."""
    return atr_lookback(sizing.period) if isinstance(sizing, AtrRisk) else 0


def atr_for(sizing: Sizing, candles: Sequence[Candle], timeframe: Timeframe) -> Decimal | None:
    """The ATR the mode sizes by (`atr_of`), or None when the mode doesn't use ATR."""
    if not isinstance(sizing, AtrRisk):
        return None
    return atr_of(candles, sizing.period, timeframe)


def atr_lookback(period: int) -> int:
    """The closed candles an ATR over `period` is taken from.

    Six periods, like RSI's warm-up (D28): Wilder's smoothing starts from a seed that
    fades by 1 - 1/period per candle, ~0.2% of it left after six periods.
    """
    return 6 * period + 1


def atr_of(candles: Sequence[Candle], period: int, timeframe: Timeframe) -> Decimal | None:
    """Wilder's ATR on exactly the last `atr_lookback(period)` closed candles.

    The same candles in a backtest and a live bot give the same value. None with too few
    candles or a gap among them.
    """
    needed = atr_lookback(period)
    window = candles[-needed:]
    step = STEP[timeframe]
    if len(window) < needed or any(b.ts - a.ts != step for a, b in pairwise(window)):
        return None
    values = atr(
        [float(c.high) for c in window],
        [float(c.low) for c in window],
        [float(c.close) for c in window],
        period,
    )
    # Indicator math is float (D9); back to Decimal for the money math.
    return Decimal(str(values[-1]))


def buy_amount(
    cash: Decimal, price: Decimal, fee_rate: Decimal, fraction: Decimal, step: Decimal
) -> Decimal:
    """How much to buy with `fraction` of `cash`, leaving room for the fee.

    Rounded down to the amount step, so cost + fee never exceeds the cash.
    """
    return cash * fraction / (price * (1 + fee_rate)) // step * step


def below_minimum(amount: Decimal, price: Decimal, rules: MarketRules) -> str | None:
    """Why an order of `amount` at `price` is too small for the market, or None if it isn't."""
    if amount == 0:
        return f"size rounds to 0 at amount step {rules.amount_step}"
    if rules.min_amount is not None and amount < rules.min_amount:
        return f"amount {amount} below min_amount {rules.min_amount}"
    if rules.min_cost is not None and amount * price < rules.min_cost:
        return f"value {amount * price} below min_cost {rules.min_cost}"
    return None


def _down(value: Decimal, step: Decimal) -> Decimal:
    return value // step * step


def _show(value: Decimal) -> str:
    # Plain digits: no "0E-13" or "1000.0" from a value read back from the database.
    return f"{value.normalize():f}"
