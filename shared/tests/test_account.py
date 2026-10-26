"""The account's totals (equity, deposits, peak, day start), worked out from its parts.

The same account as engine/tests/test_risk_account.py, at noon in Manila on 2026-10-26:
2600 USDT of cash, 5 BTC bought at 100 (0.5 fee so far) with the bid at 90, and two
closed trades: +150 just before midnight in Manila, -50 just after.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from shared.account import MANILA, ClosedTrade, Held, Totals, account_totals, manila_midnight

MIDNIGHT = datetime(2026, 10, 25, 16, 0, tzinfo=UTC)  # in Manila
MINUTE = timedelta(minutes=1)


def d(value: str) -> Decimal:
    return Decimal(value)


BTC = Held(amount=d("5"), avg_entry=d("100"), realized_pnl=d("-0.5"), bid=d("90"))
BEFORE = ClosedTrade(realized_pnl=d("150"), closed_at=MIDNIGHT - MINUTE)
AFTER = ClosedTrade(realized_pnl=d("-50"), closed_at=MIDNIGHT + MINUTE)


def test_the_totals_of_an_account_with_open_and_closed_trades() -> None:
    assert account_totals(d("2600"), [BTC], [BEFORE, AFTER], MIDNIGHT) == Totals(
        equity=d("3050"),  # 2600 cash + 5 BTC at the 90 bid
        # Cash plus positions at cost (3100) is the deposits plus every realized P&L
        # (-0.5 + 150 - 50 = 99.5).
        deposits=d("3000.5"),
        peak=d("3150.5"),  # +150 by the first closed trade, then -50
        day_start=d("3150.5"),  # the +150 closed before midnight in Manila, the -50 after
    )


def test_closed_trades_count_in_the_order_they_closed() -> None:
    # The peak is a running total: given the other way round, it's the same.
    assert account_totals(d("2600"), [BTC], [AFTER, BEFORE], MIDNIGHT).peak == d("3150.5")


def test_a_trade_closed_exactly_at_midnight_is_today_s() -> None:
    at_midnight = ClosedTrade(realized_pnl=d("150"), closed_at=MIDNIGHT)
    assert account_totals(d("2600"), [], [at_midnight], MIDNIGHT).day_start == d("2450")


def test_an_open_trade_s_loss_shows_in_equity_not_in_the_peak() -> None:
    totals = account_totals(d("500"), [BTC], [], MIDNIGHT)

    assert totals.equity == d("950")
    assert totals.deposits == d("1000.5")  # 500 + 500 at cost + the 0.5 fee
    assert totals.peak == totals.deposits == totals.day_start


def test_an_empty_account_is_all_zero() -> None:
    assert account_totals(d("0"), [], [], MIDNIGHT) == Totals(
        equity=d("0"), deposits=d("0"), peak=d("0"), day_start=d("0")
    )


@pytest.mark.parametrize(
    ("now", "midnight"),
    [
        # 15:59 UTC is 23:59 in Manila: still the 26th there.
        (datetime(2026, 10, 26, 15, 59, tzinfo=UTC), datetime(2026, 10, 25, 16, 0, tzinfo=UTC)),
        # 16:00 UTC is midnight in Manila: a new day.
        (datetime(2026, 10, 26, 16, 0, tzinfo=UTC), datetime(2026, 10, 26, 16, 0, tzinfo=UTC)),
        # 02:00 UTC is 10:00 in Manila.
        (datetime(2026, 10, 26, 2, 0, tzinfo=UTC), datetime(2026, 10, 25, 16, 0, tzinfo=UTC)),
    ],
)
def test_the_day_starts_at_midnight_in_manila(now: datetime, midnight: datetime) -> None:
    assert manila_midnight(now) == midnight
    assert manila_midnight(now).tzinfo == MANILA
