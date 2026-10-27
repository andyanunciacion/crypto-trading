"""What a backtest takes and what it gives back. Money is Decimal throughout (D9).

The fills, equity curve, trades and report live in shared/backtest.py, so the dashboard
can read a stored run and report on it without the engine (D48).
"""

from decimal import Decimal
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from engine.exchange.models import MarketRules, Timeframe
from engine.marketdata.timeframes import floor
from engine.risk.sizing import ALL_IN, Sizing
from engine.risk.stops import NO_STOPS, Stops
from engine.strategies.base import Signal
from shared.backtest import BacktestReport, EquityPoint, Fill, SkippedOrder, Trade

__all__ = [
    "DEFAULT_FEE_RATE",
    "DEFAULT_INITIAL_CASH",
    "BacktestConfig",
    "BacktestReport",
    "BacktestResult",
    "EquityPoint",
    "Fill",
    "SkippedOrder",
    "Trade",
    "default_slippage",
]

DEFAULT_INITIAL_CASH = Decimal(1000)  # in the quote currency (USDT)
# 0.1% a fill: conservative, since a fill at the next open acts like a taker order.
# Set your real MEXC fee tier per run.
DEFAULT_FEE_RATE = Decimal("0.001")

# Slippage defaults (ARCHITECTURE §8): the big pairs have deep books; anything else is
# assumed thin until shown otherwise.
MAJOR_PAIRS = frozenset({"BTC/USDT", "ETH/USDT"})
MAJOR_SLIPPAGE = Decimal("0.0005")  # 0.05%
OTHER_SLIPPAGE = Decimal("0.003")  # 0.30%


def default_slippage(symbol: str) -> Decimal:
    return MAJOR_SLIPPAGE if symbol in MAJOR_PAIRS else OTHER_SLIPPAGE


class _Frozen(BaseModel):
    # Same rules as the exchange models: immutable, no unknown fields, no NaN/Infinity.
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class BacktestConfig(_Frozen):
    """Everything a run depends on besides the candles, so it can be stored and re-run."""

    strategy: str
    strategy_version: int
    params: dict[str, JsonValue]  # the strategy's params, as JSON
    exchange: str
    symbol: str
    timeframe: Timeframe
    # Trading runs on the candles with open times in [start, end). The strategy's
    # warm-up candles come from before `start`.
    start: AwareDatetime
    end: AwareDatetime
    initial_cash: Decimal = Field(default=DEFAULT_INITIAL_CASH, gt=0)
    fee_rate: Decimal = Field(default=DEFAULT_FEE_RATE, ge=0, lt=1)  # of each fill's value
    slippage_rate: Decimal = Field(ge=0, lt=1)  # see default_slippage()
    market_rules: MarketRules
    sizing: Sizing = ALL_IN  # how much each BUY spends (risk/sizing.py, D39)
    stops: Stops = NO_STOPS  # when a position is sold regardless (risk/stops.py, D40)

    @model_validator(mode="after")
    def _range_is_whole_candles(self) -> Self:
        if self.start >= self.end:
            raise ValueError(f"start ({self.start}) must be before end ({self.end})")
        for name, ts in (("start", self.start), ("end", self.end)):
            if floor(ts, self.timeframe) != ts:
                raise ValueError(f"{name} ({ts}) must be on a {self.timeframe} candle boundary")
        return self


class BacktestResult(_Frozen):
    """As stored in `backtest_runs.result`; shared.backtest.StoredResult reads it back
    with the signals left as JSON."""

    signals: list[Signal]  # every signal that wasn't HOLD, in order
    fills: list[Fill]
    skipped: list[SkippedOrder]
    equity_curve: list[EquityPoint]  # one point per candle in the range
    final_cash: Decimal
    final_amount: Decimal  # coin still held at the end; 0 when flat
    final_equity: Decimal  # cash + coin at the last close
    total_fees: Decimal
