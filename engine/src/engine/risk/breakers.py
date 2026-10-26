"""Circuit breakers: automatic pauses when trading can't be trusted (ARCHITECTURE §6, D42).

A circuit breaker trips on its own and stops buys until things are back to normal.
The limits (limits.py) ask whether the account can afford a buy; these ask whether
anything should be bought right now. The RiskManager checks them on every buy, before
the limits, with the settings in `risk_settings` (each off when None):

- the bot's status: a bot that isn't `running` buys nothing. A paused one still sells
  and keeps its stops; a stopped one only until the engine restarts without it.
- `stale_data`: every price the buy relies on, the coin bought and every coin the
  account holds (equity values them at their bids), must be at most
  `stale_after_candles` of the buying bot's candles old. An older one means its feed
  is down, and the account can't be valued.
- `price_spike`: no buys of a coin whose mid price swung `spike_pct` or more within the
  last `spike_minutes` (highest against lowest, as a share of the lowest). It clears
  once the swing leaves the window. The prices come from `PriceSwings`, which the price
  feed fills.
- the error breaker: `max_failed_orders` failed orders (rejected, or the broker raised)
  by one bot within `failed_orders_minutes` pause it. That stays until someone sets it
  running again: failures in a row usually mean a bug or an exchange problem, which a
  person should look at first. `Failures` counts them, in memory.

Like the limits, they never block a sell, a stop's exit or a cancel: those only lower
exposure, and blocking one could trap a losing position (D41).
"""

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import JsonValue

from engine.exchange.models import Timeframe
from engine.marketdata.timeframes import STEP
from engine.risk.limits import MAX_SPIKE_MINUTES, RiskDecision, RiskSettings, show, show_pct
from shared.prices import PriceUpdate

# How much price history `PriceSwings` keeps: the longest spike window allowed.
_KEPT = timedelta(minutes=MAX_SPIKE_MINUTES)


@dataclass(frozen=True)
class Swing:
    """The lowest and highest mid price within a window."""

    low: Decimal
    high: Decimal


@dataclass(frozen=True)
class Market:
    """What the breakers check a buy against (risk/account.py works it out)."""

    status: str  # the buying bot's: running, paused or stopped
    paused_reason: str | None  # why it's paused, if it says
    timeframe: Timeframe  # the buying bot's: the stale cutoff counts its candles
    # The age of each price the buy relies on, the coin bought first; None: never had one.
    price_ages: dict[str, timedelta | None]
    swing: Swing | None  # the coin's, within the spike window; None with no prices in it

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "status": self.status,
            "paused_reason": self.paused_reason,
            "timeframe": self.timeframe,
            "price_ages_s": {
                symbol: None if age is None else round(age.total_seconds(), 3)
                for symbol, age in self.price_ages.items()
            },
            "swing": None
            if self.swing is None
            else {"low": show(self.swing.low), "high": show(self.swing.high)},
        }


def check_breakers(settings: RiskSettings, market: Market, symbol: str) -> RiskDecision | None:
    """The block for a buy of `symbol`, if a breaker has tripped; None to go on to the limits."""
    for rule in (_status, _stale, _spike):
        decision = rule(settings, market, symbol)
        if decision is not None:
            return decision
    return None


def _status(settings: RiskSettings, market: Market, symbol: str) -> RiskDecision | None:
    if market.status == "running":
        return None
    detail = f"the bot is {market.status}"
    if market.paused_reason is not None:
        detail += f": {market.paused_reason}"
    return _blocked(f"bot_{market.status}", detail)


def _stale(settings: RiskSettings, market: Market, symbol: str) -> RiskDecision | None:
    candles = settings.stale_after_candles
    if candles is None:
        return None
    cutoff = candles * STEP[market.timeframe]
    for priced, age in market.price_ages.items():
        if age is None:
            return _blocked("stale_data", f"no price for {priced}")
        if age > cutoff:
            return _blocked(
                "stale_data",
                f"the price for {priced} is {age.total_seconds():.0f} s old, over {candles}"
                f" candles of {market.timeframe} ({cutoff.total_seconds():.0f} s):"
                " its feed may be down",
            )
    return None


def _spike(settings: RiskSettings, market: Market, symbol: str) -> RiskDecision | None:
    limit, swing = settings.spike_pct, market.swing
    # Compared by multiplying, not dividing, so a swing of exactly the limit is exact.
    if limit is None or swing is None or swing.low <= 0:
        return None
    if (swing.high - swing.low) * 100 < limit * swing.low:
        return None
    swung = (swing.high - swing.low) / swing.low * 100
    return _blocked(
        "price_spike",
        f"{symbol} swung {show_pct(swung)}% within {settings.spike_minutes} min"
        f" (low {show(swing.low)}, high {show(swing.high)}): the limit is {show(limit)}%",
    )


def _blocked(rule: str, detail: str) -> RiskDecision:
    return RiskDecision(decision="blocked", rule=rule, detail=detail)


# --- Price swings ------------------------------------------------------------------------


@dataclass(slots=True)
class _Second:
    at: datetime
    low: Decimal
    high: Decimal


class PriceSwings:
    """Each symbol's mid prices over the last hour, as the lowest and highest per second.

    In memory: a restarted engine starts with none, which reads as no spike.
    """

    def __init__(self) -> None:
        self._seconds: dict[tuple[str, str], deque[_Second]] = {}  # oldest first

    def observe(self, update: PriceUpdate) -> None:
        """Note a new price. One with neither a bid nor an ask is skipped."""
        sides = [p for p in (update.bid, update.ask) if p is not None]
        if not sides:
            return
        mid = sum(sides, Decimal(0)) / len(sides)
        at = update.ts.replace(microsecond=0)
        seconds = self._seconds.setdefault((update.exchange, update.symbol), deque())
        if seconds and seconds[-1].at >= at:
            # The same second, or a late one (REST polling overlapping the WebSocket):
            # into the newest, so its extreme still counts.
            last = seconds[-1]
            last.low, last.high = min(last.low, mid), max(last.high, mid)
        else:
            seconds.append(_Second(at, mid, mid))
        while seconds[0].at < at - _KEPT:
            seconds.popleft()

    def swing(self, exchange: str, symbol: str, since: datetime) -> Swing | None:
        """The lowest and highest mid from `since` on; None if there's none."""
        start = since.replace(microsecond=0)
        window = [s for s in self._seconds.get((exchange, symbol), ()) if s.at >= start]
        if not window:
            return None
        return Swing(low=min(s.low for s in window), high=max(s.high for s in window))


# --- Repeated failures -------------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    """An order that failed: the broker rejected it, or raised."""

    ts: datetime
    client_order_id: str
    why: str


@dataclass
class Failures:
    """Each bot's recent failed orders. In memory: a restart starts the count again (a
    pause it tripped is in the database, and stays)."""

    _by_bot: dict[int, deque[Failure]] = field(default_factory=dict)

    def add(self, bot_id: int, failure: Failure, settings: RiskSettings) -> list[Failure] | None:
        """Count `failure`. If it trips the breaker, the failures that did; else None."""
        most, minutes = settings.max_failed_orders, settings.failed_orders_minutes
        if most is None or minutes is None:
            return None
        recent = self._by_bot.setdefault(bot_id, deque())
        recent.append(failure)
        while recent[0].ts < failure.ts - timedelta(minutes=minutes):
            recent.popleft()
        if len(recent) < most:
            return None
        tripped = list(recent)
        recent.clear()  # once it's resumed, it takes as many again
        return tripped


def paused_reason(failures: Sequence[Failure], settings: RiskSettings) -> str:
    """Why the error breaker paused a bot: for `bots.paused_reason`, and every buy it blocks."""
    orders = ", ".join(f"{f.client_order_id} ({f.why})" for f in failures)
    return f"{len(failures)} failed orders within {settings.failed_orders_minutes} min: {orders}"
