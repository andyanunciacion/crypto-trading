"""The circuit breakers (risk/breakers.py): when buys pause, whatever the limits say.

Pure: the settings and what the market looks like in, a block (or nothing) out. The
bot here trades 1m candles and is running; BTC/USDT's price is a second old and steady
at 100. Each test changes what it's about. The breakers are §6's: 2 candles, 3 failed
orders in 10 minutes, a 3% swing within 5 minutes.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.risk.breakers import (
    Failure,
    Failures,
    Market,
    PriceSwings,
    Swing,
    check_breakers,
    paused_reason,
)
from engine.risk.limits import NO_LIMITS, RiskDecision, RiskSettings
from shared.prices import PriceUpdate

BTC = "BTC/USDT"
ETH = "ETH/USDT"
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
BREAKERS = RiskSettings(
    stale_after_candles=2,
    max_failed_orders=3,
    failed_orders_minutes=10,
    spike_pct=Decimal(3),
    spike_minutes=5,
)
CALM = Market(
    status="running",
    paused_reason=None,
    timeframe="1m",
    price_ages={BTC: SECOND},
    swing=Swing(low=Decimal(100), high=Decimal(100)),
)


def d(value: str) -> Decimal:
    return Decimal(value)


def blocked(rule: str, detail: str) -> RiskDecision:
    return RiskDecision(decision="blocked", rule=rule, detail=detail)


def test_a_calm_market_lets_a_buy_through() -> None:
    assert check_breakers(BREAKERS, CALM, BTC) is None


def test_with_every_breaker_off_only_the_bots_status_counts() -> None:
    wild = replace(
        CALM, price_ages={BTC: None, ETH: timedelta(days=3)}, swing=Swing(d("50"), d("200"))
    )
    assert check_breakers(NO_LIMITS, wild, BTC) is None

    paused = replace(wild, status="paused")
    assert check_breakers(NO_LIMITS, paused, BTC) == blocked("bot_paused", "the bot is paused")


# --- The bot's status ------------------------------------------------------------------


def test_a_paused_bot_says_why() -> None:
    market = replace(CALM, status="paused", paused_reason="3 failed orders within 10 min")

    assert check_breakers(BREAKERS, market, BTC) == blocked(
        "bot_paused", "the bot is paused: 3 failed orders within 10 min"
    )


def test_a_stopped_bot_buys_nothing_even_before_the_engine_restarts() -> None:
    market = replace(CALM, status="stopped")
    assert check_breakers(BREAKERS, market, BTC) == blocked("bot_stopped", "the bot is stopped")


# --- Stale data ------------------------------------------------------------------------


def test_a_price_two_candles_old_is_still_fresh() -> None:
    market = replace(CALM, price_ages={BTC: 2 * MINUTE})
    assert check_breakers(BREAKERS, market, BTC) is None


def test_a_price_older_than_two_candles_blocks() -> None:
    market = replace(CALM, price_ages={BTC: 2 * MINUTE + SECOND})

    assert check_breakers(BREAKERS, market, BTC) == blocked(
        "stale_data",
        "the price for BTC/USDT is 121 s old, over 2 candles of 1m (120 s): its feed may be down",
    )


def test_the_cutoff_counts_the_buying_bots_candles() -> None:
    market = replace(CALM, timeframe="1h", price_ages={BTC: 119 * MINUTE})
    assert check_breakers(BREAKERS, market, BTC) is None


def test_no_price_at_all_blocks() -> None:
    market = replace(CALM, price_ages={BTC: None})
    assert check_breakers(BREAKERS, market, BTC) == blocked("stale_data", "no price for BTC/USDT")


def test_a_held_coins_stale_price_blocks_a_buy_of_another() -> None:
    # Equity counts the ETH held at its bid: a bid from an hour ago can't value it.
    market = replace(CALM, price_ages={BTC: SECOND, ETH: 60 * MINUTE})

    decision = check_breakers(BREAKERS, market, BTC)

    assert decision is not None and decision.rule == "stale_data"
    assert decision.detail.startswith("the price for ETH/USDT is 3600 s old")


# --- Price spikes ----------------------------------------------------------------------


def test_a_swing_of_exactly_the_limit_blocks() -> None:
    market = replace(CALM, swing=Swing(low=d("100"), high=d("103")))

    assert check_breakers(BREAKERS, market, BTC) == blocked(
        "price_spike",
        "BTC/USDT swung 3% within 5 min (low 100, high 103): the limit is 3%",
    )


def test_a_swing_just_under_the_limit_allows() -> None:
    market = replace(CALM, swing=Swing(low=d("100"), high=d("102.99")))
    assert check_breakers(BREAKERS, market, BTC) is None


def test_no_prices_in_the_window_is_no_spike() -> None:
    # Just started: nothing to compare. A missing price is the stale breaker's to catch.
    assert check_breakers(BREAKERS, replace(CALM, swing=None), BTC) is None


def test_the_first_breaker_tripped_decides() -> None:
    # Everything wrong: the bot's status first, then stale data, then the spike.
    worst = Market(
        status="paused",
        paused_reason=None,
        timeframe="1m",
        price_ages={BTC: None},
        swing=Swing(d("50"), d("100")),
    )
    running = replace(worst, status="running")
    fresh = replace(running, price_ages={BTC: SECOND})

    rules = []
    for market in (worst, running, fresh):
        decision = check_breakers(BREAKERS, market, BTC)
        assert decision is not None
        rules.append(decision.rule)
    assert rules == ["bot_paused", "stale_data", "price_spike"]


# --- Price swings ----------------------------------------------------------------------


def update(ts: datetime, bid: str | None, ask: str | None, symbol: str = BTC) -> PriceUpdate:
    return PriceUpdate(
        exchange="mexc",
        symbol=symbol,
        bid=None if bid is None else d(bid),
        ask=None if ask is None else d(ask),
        ts=ts,
        received_at=ts,
        source="ws",
    )


def test_a_swing_is_the_lowest_and_highest_mid_since_then() -> None:
    swings = PriceSwings()
    swings.observe(update(T0, "99", "101"))  # mid 100
    swings.observe(update(T0 + 0.5 * SECOND, "102", "104"))  # 103, the same second
    swings.observe(update(T0 + 2 * MINUTE, "96", "98"))  # 97
    swings.observe(update(T0 + 3 * MINUTE, "99.9", "100.1"))  # 100

    assert swings.swing("mexc", BTC, T0) == Swing(low=d("97"), high=d("103"))
    assert swings.swing("mexc", BTC, T0 + MINUTE) == Swing(low=d("97"), high=d("100"))
    assert swings.swing("mexc", BTC, T0 + 4 * MINUTE) is None
    assert swings.swing("mexc", ETH, T0) is None


def test_one_sided_quotes_count_their_side_and_empty_ones_nothing() -> None:
    swings = PriceSwings()
    swings.observe(update(T0, "99", None))
    swings.observe(update(T0 + SECOND, None, "105"))
    swings.observe(update(T0 + 2 * SECOND, None, None))

    assert swings.swing("mexc", BTC, T0) == Swing(low=d("99"), high=d("105"))


def test_an_older_update_folds_into_the_newest_second() -> None:
    swings = PriceSwings()
    swings.observe(update(T0 + MINUTE, "100", "100"))
    swings.observe(update(T0, "90", "90"))  # late: REST polling while the WebSocket was back

    assert swings.swing("mexc", BTC, T0 + MINUTE) == Swing(low=d("90"), high=d("100"))


def test_history_past_the_longest_window_is_dropped() -> None:
    swings = PriceSwings()
    swings.observe(update(T0, "50", "50"))
    swings.observe(update(T0 + 61 * MINUTE, "100", "100"))

    assert swings.swing("mexc", BTC, T0) == Swing(low=d("100"), high=d("100"))


# --- Repeated failures -----------------------------------------------------------------


def failure(minutes: float, cid: str = "b1-202610261200-b") -> Failure:
    return Failure(T0 + minutes * MINUTE, cid, "rejected: insufficient USDT")


def test_the_third_failure_within_ten_minutes_trips() -> None:
    failures = Failures()

    assert failures.add(1, failure(0, "b1-a"), BREAKERS) is None
    assert failures.add(1, failure(4, "b1-b"), BREAKERS) is None
    tripped = failures.add(1, failure(10, "b1-c"), BREAKERS)  # exactly 10 min after the first

    assert tripped == [failure(0, "b1-a"), failure(4, "b1-b"), failure(10, "b1-c")]
    # Counting starts again: the next one alone doesn't trip it.
    assert failures.add(1, failure(11), BREAKERS) is None


def test_failures_spread_over_more_than_ten_minutes_dont_trip() -> None:
    failures = Failures()
    for minutes in (0, 6, 10.5, 17):
        assert failures.add(1, failure(minutes), BREAKERS) is None


def test_failures_count_per_bot() -> None:
    failures = Failures()
    for bot_id in (1, 2, 1, 2):
        assert failures.add(bot_id, failure(0), BREAKERS) is None
    assert failures.add(2, failure(1), BREAKERS) is not None


def test_with_the_error_breaker_off_nothing_trips() -> None:
    failures = Failures()
    for minutes in range(5):
        assert failures.add(1, failure(minutes), NO_LIMITS) is None


def test_the_pause_says_which_orders_failed_and_how() -> None:
    tripped = [failure(0, "b1-a"), failure(1, "b1-b")]
    assert paused_reason(tripped, BREAKERS) == (
        "2 failed orders within 10 min: b1-a (rejected: insufficient USDT),"
        " b1-b (rejected: insufficient USDT)"
    )


# --- Settings ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"stale_after_candles": 0},
        {"max_failed_orders": 3},  # without its window
        {"failed_orders_minutes": 10},
        {"spike_pct": "3"},
        {"spike_pct": "0", "spike_minutes": 5},
        {"spike_pct": "3", "spike_minutes": 61},  # longer than the history kept
    ],
)
def test_bad_breaker_settings_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RiskSettings.model_validate(bad)
