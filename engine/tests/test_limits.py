"""The limits (risk/limits.py): what a buy may do, given the account and the settings.

Pure: an account snapshot and settings in, a decision and the order to place out. The
account here is 1000 USDT of equity, flat, with nothing lost today; each test changes
what it's about. Market rules as on MEXC's BTC/USDT: steps of 0.0001 BTC, 1 USDT minimum.
"""

from dataclasses import replace
from decimal import Decimal
from typing import get_args

import pytest

from engine.exchange.models import MarketRules, OrderRequest
from engine.risk.limits import (
    NO_LIMITS,
    SELLS_ALLOWED,
    WITHIN_LIMITS,
    Account,
    Checked,
    Decision,
    RiskDecision,
    RiskSettings,
    check_buy,
)
from shared.db.models import RISK_DECISIONS

BTC = "BTC/USDT"
RULES = MarketRules(
    symbol=BTC,
    amount_step=Decimal("0.0001"),
    price_step=Decimal("0.01"),
    min_amount=Decimal("0.0001"),
    min_cost=Decimal("1"),
)
# §6's starting values.
DEFAULTS = RiskSettings(
    max_open_positions=3,
    max_coin_exposure_pct=Decimal(25),
    max_daily_loss_pct=Decimal(3),
    max_drawdown_pct=Decimal(10),
)
FLAT = Account(
    equity=Decimal(1000),
    peak=Decimal(1000),
    day_start=Decimal(1000),
    open_positions=0,
    coin_exposure=Decimal(0),
    daily_loss_hit=False,
)


def d(value: str) -> Decimal:
    return Decimal(value)


def buy(amount: str = "2", price: str = "100") -> OrderRequest:
    """A buy of `amount` BTC at `price`: 200 USDT by default, 20% of the 1000 equity."""
    return OrderRequest.model_validate(
        {
            "symbol": BTC,
            "side": "buy",
            "amount": amount,
            "price": price,
            "client_order_id": "b1-202610261200-b",
        }
    )


def blocked(rule: str, detail: str) -> Checked:
    return Checked(RiskDecision(decision="blocked", rule=rule, detail=detail), None)


def test_a_buy_within_every_limit_is_allowed_unchanged() -> None:
    assert check_buy(DEFAULTS, FLAT, buy(), RULES) == Checked(WITHIN_LIMITS, buy())


def test_with_no_limits_anything_goes() -> None:
    broke = Account(
        equity=d("100"),
        peak=d("1000"),
        day_start=d("1000"),
        open_positions=50,
        coin_exposure=d("100"),
        daily_loss_hit=True,
    )
    assert check_buy(NO_LIMITS, broke, buy(), RULES) == Checked(WITHIN_LIMITS, buy())


def test_a_sell_is_not_checked() -> None:
    sell = buy().model_copy(update={"side": "sell"})
    with pytest.raises(ValueError, match="never blocked"):
        check_buy(DEFAULTS, FLAT, sell, RULES)
    assert SELLS_ALLOWED.decision == "allowed"


# --- Max drawdown ----------------------------------------------------------------------


def test_drawdown_at_the_limit_blocks() -> None:
    # 1000 down to 900 is a 10% drawdown: exactly the limit.
    account = replace(FLAT, equity=d("900"), day_start=d("900"))

    assert check_buy(DEFAULTS, account, buy(), RULES) == blocked(
        "max_drawdown", "equity 900 is 10% below its peak 1000: the limit is 10%"
    )


def test_drawdown_just_under_the_limit_allows() -> None:
    account = replace(FLAT, equity=d("900.01"), day_start=d("900.01"))
    assert check_buy(DEFAULTS, account, buy(), RULES).decision == WITHIN_LIMITS


def test_drawdown_counts_from_the_peak_not_the_start() -> None:
    # Up to 1200 then back to 1050: still above the start, but 12.5% below the peak.
    account = replace(FLAT, equity=d("1050"), peak=d("1200"), day_start=d("1050"))

    assert check_buy(DEFAULTS, account, buy(), RULES) == blocked(
        "max_drawdown", "equity 1050 is 12.5% below its peak 1200: the limit is 10%"
    )


# --- Max daily loss --------------------------------------------------------------------


def test_daily_loss_at_the_limit_blocks() -> None:
    # 1000 at midnight, 970 now: 3% down today.
    account = replace(FLAT, equity=d("970"))

    assert check_buy(DEFAULTS, account, buy(), RULES) == blocked(
        "daily_loss",
        "down 30 today, 3% of 1000 at midnight Asia/Manila: the limit is 3%;"
        " no buys until the next midnight",
    )


def test_daily_loss_just_under_the_limit_allows() -> None:
    account = replace(FLAT, equity=d("970.01"))
    assert check_buy(DEFAULTS, account, buy(), RULES).decision == WITHIN_LIMITS


def test_a_daily_loss_hit_earlier_today_still_blocks_after_a_bounce() -> None:
    # Back at 1000, but the limit already blocked a buy today: paused until midnight.
    account = replace(FLAT, daily_loss_hit=True)

    assert check_buy(DEFAULTS, account, buy(), RULES) == blocked(
        "daily_loss",
        "the daily loss limit was reached earlier today: no buys until midnight Asia/Manila",
    )


def test_a_daily_gain_never_blocks() -> None:
    account = replace(FLAT, equity=d("1100"), peak=d("1100"))
    assert check_buy(DEFAULTS, account, buy(), RULES).decision == WITHIN_LIMITS


# --- Max open positions ----------------------------------------------------------------


def test_open_positions_at_the_limit_block() -> None:
    account = replace(FLAT, open_positions=3)

    assert check_buy(DEFAULTS, account, buy(), RULES) == blocked(
        "max_open_positions", "3 positions open or opening: the limit is 3"
    )


def test_one_slot_left_allows() -> None:
    account = replace(FLAT, open_positions=2)
    assert check_buy(DEFAULTS, account, buy(), RULES).decision == WITHIN_LIMITS


def test_adding_to_a_position_the_bot_holds_opens_none() -> None:
    # A grid or DCA buy into a coin the bot already holds (P6-6): still 3 positions after.
    account = replace(FLAT, open_positions=3, adds=True)
    assert check_buy(DEFAULTS, account, buy(), RULES).decision == WITHIN_LIMITS


def test_an_add_is_still_capped_by_the_coin_exposure() -> None:
    # 200 held, 25% of 1000 = 250: only 50 more fits, 0.5 BTC at 100.
    account = replace(FLAT, open_positions=3, coin_exposure=d("200"), adds=True)
    checked = check_buy(DEFAULTS, account, buy(), RULES)

    assert checked.decision.decision == "resized"
    assert checked.request == buy("0.5")


# --- Max exposure per coin -------------------------------------------------------------


def test_a_buy_up_to_the_coin_cap_is_allowed() -> None:
    # 2.5 BTC at 100 = 250: exactly 25% of 1000.
    assert check_buy(DEFAULTS, FLAT, buy("2.5"), RULES) == Checked(WITHIN_LIMITS, buy("2.5"))


def test_a_buy_over_the_coin_cap_is_shrunk_to_fit() -> None:
    # All-in at 100: 9.99 BTC. 25% of 1000 = 250 fits 2.5 BTC.
    checked = check_buy(DEFAULTS, FLAT, buy("9.99"), RULES)

    assert checked == Checked(
        RiskDecision(
            decision="resized",
            rule="max_coin_exposure",
            detail="BTC would be 999 of 1000 equity, over the 25% limit (250),"
            " with 0 already held or on order; resized from 9.99 to 2.5",
            requested_amount=d("9.99"),
        ),
        buy("2.5"),
    )


def test_the_shrunk_amount_rounds_down_to_the_step() -> None:
    # 250 of room at 110.06 = 2.27148... BTC -> 2.2714.
    checked = check_buy(DEFAULTS, FLAT, buy("9", "110.06"), RULES)
    assert checked.request == buy("2.2714", "110.06")


def test_what_is_already_held_or_on_order_counts_against_the_cap() -> None:
    # 200 of BTC already: room for 50, so 0.5 BTC.
    account = replace(FLAT, coin_exposure=d("200"))
    checked = check_buy(DEFAULTS, account, buy("2"), RULES)

    assert checked.decision.decision == "resized"
    assert checked.request == buy("0.5")


def test_a_shrunk_buy_too_small_to_trade_is_blocked() -> None:
    # 249.5 already: room for 0.5 USDT, under the 1 USDT minimum.
    account = replace(FLAT, coin_exposure=d("249.5"))

    assert check_buy(DEFAULTS, account, buy("2"), RULES) == blocked(
        "max_coin_exposure",
        "BTC would be 449.5 of 1000 equity, over the 25% limit (250),"
        " with 249.5 already held or on order; what fits is too small:"
        " value 0.5000 below min_cost 1",
    )


def test_a_coin_already_over_the_cap_blocks() -> None:
    # Prices rose since the buy: 300 held, over the 250 cap. No room at all.
    account = replace(FLAT, coin_exposure=d("300"))
    checked = check_buy(DEFAULTS, account, buy("2"), RULES)

    assert checked.request is None
    assert checked.decision.rule == "max_coin_exposure"
    assert checked.decision.detail.endswith("size rounds to 0 at amount step 0.0001")


# --- Which rule decides ----------------------------------------------------------------


def test_the_first_breached_rule_decides() -> None:
    # Every limit breached: the drawdown is checked first, then the daily loss, then
    # open positions, and the exposure (the only one that can shrink a buy) last.
    worst = Account(
        equity=d("800"),
        peak=d("1000"),
        day_start=d("1000"),
        open_positions=3,
        coin_exposure=d("800"),
        daily_loss_hit=True,
    )
    rules_in_order = []
    settings = DEFAULTS
    for off in ("max_drawdown_pct", "max_daily_loss_pct", "max_open_positions", None):
        rules_in_order.append(check_buy(settings, worst, buy(), RULES).decision.rule)
        if off is not None:
            settings = settings.model_copy(update={off: None})

    assert rules_in_order == [
        "max_drawdown",
        "daily_loss",
        "max_open_positions",
        "max_coin_exposure",
    ]


# --- Settings and time -----------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"max_open_positions": 0},
        {"max_coin_exposure_pct": "0"},
        {"max_daily_loss_pct": "100.1"},
        {"max_drawdown_pct": "-5"},
        {"max_drawdown_pct": "NaN"},
        {"surprise": 1},
    ],
)
def test_bad_settings_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RiskSettings.model_validate(bad)


def test_decisions_match_the_database_check() -> None:
    assert get_args(Decision) == RISK_DECISIONS
