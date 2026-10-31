"""The text of each Telegram message (backend/alerts/format.py, D59)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from backend.alerts.format import number, render, render_summary
from backend.alerts.summary import DaySummary
from backend.alerts.trades import TradeFacts
from backend.dashboard.overview import AccountView, BotView, Overview, PositionView
from shared.alerts import Alert, Passed, to_alert
from shared.logs import mirrored_line, read_entry

T0 = datetime(2026, 10, 31, 2, 42, tzinfo=UTC)  # 10:42 in Manila
NAMES = {3: "btc-dca"}


def alert(event: str, level: str = "info", service: str = "engine", **fields: Any) -> Alert:
    raw = mirrored_line(
        {"event": event, "level": level, "timestamp": T0.isoformat(), **fields}, service
    )
    entry = read_entry("1-0", raw.encode())
    assert entry is not None
    made = to_alert(entry)
    assert made is not None
    return made


def facts(**changes: Any) -> TradeFacts:
    values: dict[str, Any] = {
        "bot_name": "btc-dca",
        "mode": "paper",
        "symbol": "BTC/USDT",
        "side": "buy",
        "status": "closed",
        "filled": Decimal("0.00031"),
        "avg_price": Decimal("64512.10"),
        "cost": Decimal("19.998751"),
        "fee": Decimal("0.019998751"),
        "fee_currency": "USDT",
        "strategy": "dca",
        "signal_reason": "1 candles since the last buy",
        "exit_kind": None,
        "realized_pnl": None,
        "filled_at": T0,
    }
    return TradeFacts(**{**values, **changes})


def test_numbers_have_separators_no_trailing_zeros_and_never_go_through_a_float() -> None:
    assert number(Decimal("64512.10")) == "64,512.1"
    assert number(Decimal("0.00000001")) == "0.00000001"
    assert number(Decimal("100")) == "100"
    assert number("0E-8") == "0"
    assert number("not a number") == "not a number"


def test_a_buy_says_what_filled_at_what_average_and_why() -> None:
    trade = alert("broker.order.filled", bot_id=3, status="closed", client_order_id="b3-x-b")

    text = render(trade, names=NAMES, passed=Passed(), trade=facts())

    assert text == (
        "BUY btc-dca · BTC/USDT (paper)\n"
        "0.00031 BTC at 64,512.1 USDT on average\n"
        "Worth 20 USDT, fee 0.019998751 USDT\n"
        "Why: dca signal: 1 candles since the last buy\n"
        "31 Oct 10:42"
    )


def test_a_stop_s_sell_that_closed_the_trade_says_its_realized_p_and_l() -> None:
    trade = alert("broker.order.filled", bot_id=3, status="closed", client_order_id="b3-x-x1")
    sold = facts(side="sell", exit_kind="stop_loss", realized_pnl=Decimal("-1.234"))

    text = render(trade, names=NAMES, passed=Passed(), trade=sold)

    assert "SELL btc-dca · BTC/USDT (paper)" in text
    assert "Why: stop-loss" in text
    assert "Trade closed: -1.23 USDT realized, after fees" in text  # to the cent


def test_an_order_cancelled_after_part_filled_says_so() -> None:
    trade = alert("broker.order.canceled", bot_id=3, filled="0.0001", client_order_id="b3-x-b")

    text = render(trade, names=NAMES, passed=Passed(), trade=facts(status="canceled"))

    assert text.startswith("BUY btc-dca · BTC/USDT (paper), part filled before it was cancelled")


def test_with_the_database_down_a_trade_is_described_from_its_log_line() -> None:
    trade = alert(
        "broker.order.filled",
        bot_id=3,
        status="closed",
        client_order_id="b3-x-b",
        symbol="BTC/USDT",
        side="buy",
        price="64512.1",
        filled="0.00031",
    )

    text = render(trade, names={}, passed=Passed(), trade=None)

    assert text.splitlines()[:3] == [
        "BUY bot 3 · BTC/USDT",
        "Filled 0.00031, last at 64,512.1",
        "Order b3-x-b (details unavailable)",
    ]


def test_a_stop_says_which_level_the_bid_reached_and_what_it_sells() -> None:
    hit = alert(
        "bot.exit",
        bot_id=3,
        symbol="BTC/USDT",
        kind="trailing_stop",
        again=False,
        bid="63900",
        amount="0.0155",
        limit="63868.05",
        levels={"stop": "64000", "take_profit": None, "stop_loss": "62000", "peak": "66000"},
    )

    assert render(hit, names=NAMES, passed=Passed()).splitlines()[:2] == [
        "Trailing stop hit: btc-dca · BTC/USDT",
        "Bid 63,900 reached 64,000; selling 0.0155 at 63,868.05",
    ]


def test_a_block_says_its_rule_and_that_repeats_are_held_back_and_counted() -> None:
    blocked = alert(
        "risk.decision",
        bot_id=3,
        symbol="BTC/USDT",
        side="buy",
        decision="blocked",
        rule="daily_loss",
        detail="equity is 3.2% below midnight's (limit 3%)",
    )
    since = T0 - timedelta(hours=1)

    first = render(blocked, names=NAMES, passed=Passed())
    later = render(blocked, names=NAMES, passed=Passed(held=12, since=since))

    assert first == (
        "Buy blocked: btc-dca · BTC/USDT\n"
        "daily_loss: equity is 3.2% below midnight's (limit 3%)\n"
        "More like this in the next hour are held back.\n"
        "31 Oct 10:42"
    )
    assert "(+12 more like this since 31 Oct 09:42)" in later


def test_a_shrunk_buy_says_what_was_asked_and_placed() -> None:
    shrunk = alert(
        "risk.decision",
        bot_id=3,
        symbol="BTC/USDT",
        decision="resized",
        rule="max_coin_exposure",
        detail="shrunk",
        requested_amount="0.6",
        amount="0.25",
    )

    assert "Asked 0.6, placed 0.25" in render(shrunk, names=NAMES, passed=Passed())


def test_a_switch_names_both_configs_the_trigger_and_why() -> None:
    switched = alert(
        "bot.switch",
        bot_id=3,
        old={"id": 4, "strategy": "ema_crossover"},
        new={"id": 7, "strategy": "rsi_mean_reversion"},
        trigger="regime",
        policy="keep",
        reason="ranging for 3 candles",
    )

    assert render(switched, names=NAMES, passed=Passed()).splitlines()[:3] == [
        "Strategy switched (by the regime): btc-dca",
        "#4 ema_crossover → #7 rsi_mean_reversion, policy keep",
        "Why: ranging for 3 candles",
    ]


def test_the_kill_switch_says_what_it_did_and_what_it_couldn_t_close() -> None:
    done = alert(
        "kill_switch.done",
        "warning",
        ok=True,
        stopped=[3, 4],
        canceled=["b3-x-b"],
        closing=[{"bot_id": 3, "symbol": "BTC/USDT", "amount": "0.1"}],
        not_closed=[{"bot_id": 4, "symbol": "ETH/USDT", "reason": "below the minimums"}],
        took_ms=197,
    )

    assert render(done, names=NAMES, passed=Passed()).splitlines()[:4] == [
        "Kill switch done",
        "Stopped 2 bots, cancelled 1 orders, in 197 ms",
        "Selling 1 positions until they're gone",
        "Not closed: bot 4 ETH/USDT: below the minimums",
    ]


def test_an_error_names_its_service_event_and_bot() -> None:
    failed = alert("bot.error", "error", bot_id=3, error="TimeoutError", detail="db down")

    assert render(failed, names=NAMES, passed=Passed()).splitlines()[:2] == [
        "Error in engine: bot.error (btc-dca)",
        "TimeoutError: db down",
    ]


def test_the_daily_summary() -> None:
    overview = Overview(
        server_time=T0,
        mode="paper",
        engine=None,
        account=AccountView(
            mode="paper",
            currency="USDT",
            equity=Decimal("1012.3456"),
            deposits=Decimal("1000"),
            today_pnl=Decimal("2"),
            today_pnl_pct=Decimal("0.20"),
            total_pnl=Decimal("12.3456"),
            total_pnl_pct=Decimal("1.23"),
            peak=Decimal("1020"),
            drawdown_pct=Decimal("0.75"),
        ),
        positions=[
            PositionView(
                bot_id=3,
                bot_name="btc-dca",
                exchange="mexc",
                symbol="BTC/USDT",
                amount=Decimal("0.0031"),
                avg_entry=Decimal("64000"),
                bid=Decimal("64500"),
                price_ts=T0,
                value=Decimal("199.95"),
                pnl=Decimal("1.55"),
                pnl_pct=Decimal("0.78"),
                stop=None,
                take_profit=None,
                opened_at=T0,
            )
        ],
        bots=[
            BotView(
                id=3,
                name="btc-dca",
                exchange="mexc",
                symbol="BTC/USDT",
                timeframe="1m",
                mode="paper",
                status="running",
                strategy="dca",
                paused_reason=None,
                paused_at=None,
                stopped_reason=None,
                stopped_at=None,
            ),
            BotView(
                id=4,
                name="eth-ema",
                exchange="mexc",
                symbol="ETH/USDT",
                timeframe="1h",
                mode="paper",
                status="paused",
                strategy="ema_crossover",
                paused_reason="3 failed orders in 10 min",
                paused_at=T0,
                stopped_reason=None,
                stopped_at=None,
            ),
        ],
        prices=[],
    )
    summary = DaySummary(
        day=date(2026, 10, 30),
        overview=overview,
        trades_closed=4,
        realized_pnl=Decimal("3.5"),
        orders_filled=9,
        blocked={"bot_paused": 5, "stale_data": 1},
        switches=1,
    )

    assert render_summary(summary) == (
        "Daily summary for Fri 30 Oct (Manila)\n"
        "Equity 1,012.35 USDT; total +12.35 USDT (+1.23%); drawdown 0.75%\n"
        "Yesterday: 4 trades closed, +3.5 USDT realized after fees; 9 orders filled; "
        "1 strategy switches\n"
        "Buys blocked: bot_paused 5, stale_data 1\n"
        "Open positions: 1\n"
        "  btc-dca · BTC/USDT: 0.0031 (+1.55 USDT)\n"
        "Bots: running btc-dca; paused eth-ema\n"
        "  eth-ema paused: 3 failed orders in 10 min"
    )
