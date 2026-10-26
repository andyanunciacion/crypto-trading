"""Account limits: how much the bots may hold, whatever sizing asks for (ARCHITECTURE §6, D41).

Pure, all Decimal. On every buy the RiskManager takes the limits in `risk_settings` and
a snapshot of the account (risk/account.py), and `check_buy` says whether the buy may
go ahead, smaller or not at all. Sizing (sizing.py) says how much a bot wants; these
say how much it may have.

The limits, each off when None, checked in this order:
- `max_drawdown_pct`: no buys once equity is this far below its peak. Drawdown is the
  fall from the high-water mark, the most the account was ever worth.
- `max_daily_loss_pct`: no buys once today's loss reaches this share of the equity at
  midnight in Manila, and none until the next midnight, even if prices come back.
- `max_open_positions`: no buy that would open a position past this many.
- `max_coin_exposure_pct`: no coin worth more than this share of equity. A buy that
  would pass it is shrunk to fit, or blocked if what fits is too small to trade.

At exactly a limit it blocks. A sell is never checked: on spot, long only, a sell can
only lower exposure, and blocking one could trap a position (D40).

They're account-wide: equity, positions and exposure count every bot in the mode, as
they will on one shared exchange account (P9).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from engine.exchange.models import MarketRules, OrderRequest
from engine.risk.sizing import below_minimum

# The same values as `risk_events.decision`.
Decision = Literal["allowed", "blocked", "resized"]

# The daily loss rule's name: its blocks in `risk_events` keep it hit until midnight.
DAILY_LOSS = "daily_loss"
# Manila is UTC+8 all year: the Philippines has had no daylight saving since 1978.
MANILA = timezone(timedelta(hours=8), "Asia/Manila")


class _Frozen(BaseModel):
    # Same rules as the other settings: immutable, no unknown fields, no NaN/Infinity.
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class RiskDecision(_Frozen):
    """What the RiskManager made of one order."""

    decision: Decision
    rule: str  # the rule that decided
    detail: str
    requested_amount: Decimal | None = None  # what was asked for, when resized


class RiskSettings(_Frozen):
    """The limits, one row of `risk_settings` per mode. None turns a limit off."""

    max_open_positions: int | None = Field(default=None, ge=1)
    max_coin_exposure_pct: Decimal | None = Field(default=None, gt=0, le=100)
    max_daily_loss_pct: Decimal | None = Field(default=None, gt=0, le=100)
    max_drawdown_pct: Decimal | None = Field(default=None, gt=0, le=100)


NO_LIMITS = RiskSettings()

WITHIN_LIMITS = RiskDecision(decision="allowed", rule="within_limits", detail="within every limit")
SELLS_ALLOWED = RiskDecision(
    decision="allowed", rule="reduces_exposure", detail="a sell only lowers exposure: never blocked"
)


@dataclass(frozen=True)
class Account:
    """The account as a buy finds it, in the quote currency (risk/account.py works it out)."""

    equity: Decimal  # all the cash, plus every open position at the bid
    peak: Decimal  # the most equity was worth after any closed trade
    day_start: Decimal  # equity at today's midnight in Manila
    open_positions: int  # open positions, plus buys still opening one
    coin_exposure: Decimal  # the coin bought: what's held at the bid, plus open buys
    daily_loss_hit: bool  # the daily loss limit already blocked a buy today

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "equity": _show(self.equity),
            "peak": _show(self.peak),
            "day_start": _show(self.day_start),
            "open_positions": self.open_positions,
            "coin_exposure": _show(self.coin_exposure),
            "daily_loss_hit": self.daily_loss_hit,
        }


@dataclass(frozen=True)
class Checked:
    decision: RiskDecision
    request: OrderRequest | None  # what to place (maybe smaller than asked); None when blocked


@dataclass(frozen=True)
class RiskEvent:
    """A block or resize, for `risk_events`."""

    bot_id: int
    client_order_id: str
    decision: RiskDecision
    inputs: dict[str, JsonValue]  # the order, the settings and the account it was decided on


def check_buy(
    settings: RiskSettings, account: Account, request: OrderRequest, rules: MarketRules
) -> Checked:
    """Whether `request` may be placed as it is, smaller, or not at all."""
    if request.side != "buy":
        raise ValueError("only buys are checked: a sell is never blocked")
    for rule in (_drawdown, _daily_loss, _open_positions):
        blocked = rule(settings, account)
        if blocked is not None:
            return Checked(blocked, None)
    return _coin_exposure(settings, account, request, rules)


def manila_midnight(now: datetime) -> datetime:
    """When `now`'s day began in Manila: when the daily loss starts again."""
    return now.astimezone(MANILA).replace(hour=0, minute=0, second=0, microsecond=0)


def _drawdown(settings: RiskSettings, account: Account) -> RiskDecision | None:
    limit = settings.max_drawdown_pct
    peak, equity = account.peak, account.equity
    # Compared by multiplying, not dividing, so a fall of exactly the limit is exact.
    if limit is None or peak <= 0 or peak - equity < peak * limit / 100:
        return None
    drawdown = (peak - equity) / peak * 100
    return _blocked(
        "max_drawdown",
        f"equity {_show(equity)} is {_pct(drawdown)}% below its peak {_show(peak)}:"
        f" the limit is {_show(limit)}%",
    )


def _daily_loss(settings: RiskSettings, account: Account) -> RiskDecision | None:
    limit = settings.max_daily_loss_pct
    if limit is None:
        return None
    if account.daily_loss_hit:
        return _blocked(
            DAILY_LOSS,
            "the daily loss limit was reached earlier today: no buys until midnight Asia/Manila",
        )
    start = account.day_start
    loss = start - account.equity
    if start <= 0 or loss < start * limit / 100:
        return None
    return _blocked(
        DAILY_LOSS,
        f"down {_show(loss)} today, {_pct(loss / start * 100)}% of {_show(start)} at midnight"
        f" Asia/Manila: the limit is {_show(limit)}%; no buys until the next midnight",
    )


def _open_positions(settings: RiskSettings, account: Account) -> RiskDecision | None:
    limit = settings.max_open_positions
    if limit is None or account.open_positions < limit:
        return None
    return _blocked(
        "max_open_positions",
        f"{account.open_positions} positions open or opening: the limit is {limit}",
    )


def _coin_exposure(
    settings: RiskSettings, account: Account, request: OrderRequest, rules: MarketRules
) -> Checked:
    limit = settings.max_coin_exposure_pct
    if limit is None:
        return Checked(WITHIN_LIMITS, request)
    # Valued at the limit, the most the buy can cost.
    after = account.coin_exposure + request.amount * request.price
    cap = account.equity * limit / 100
    if after <= cap:
        return Checked(WITHIN_LIMITS, request)
    coin = request.symbol.split("/")[0]
    over = (
        f"{coin} would be {_show(after)} of {_show(account.equity)} equity, over the"
        f" {_show(limit)}% limit ({_show(cap)}), with {_show(account.coin_exposure)}"
        " already held or on order"
    )
    room = max(cap - account.coin_exposure, Decimal(0))
    amount = room / request.price // rules.amount_step * rules.amount_step
    too_small = below_minimum(amount, request.price, rules)
    if too_small is not None:
        return Checked(
            _blocked("max_coin_exposure", f"{over}; what fits is too small: {too_small}"), None
        )
    resized = RiskDecision(
        decision="resized",
        rule="max_coin_exposure",
        detail=f"{over}; resized from {_show(request.amount)} to {_show(amount)}",
        requested_amount=request.amount,
    )
    return Checked(resized, request.model_copy(update={"amount": amount}))


def _blocked(rule: str, detail: str) -> RiskDecision:
    return RiskDecision(decision="blocked", rule=rule, detail=detail)


def _pct(value: Decimal) -> str:
    return _show(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _show(value: Decimal) -> str:
    # Plain digits: no "0E-13" or "1000.0" from a value read back from the database.
    return f"{value.normalize():f}"
