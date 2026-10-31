"""Telegram commands, the pure parts (P7-2): reading a command and an update, and the
text of each reply (backend/alerts/commands.py, replies.py)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from telegram import Update

from backend.alerts import replies
from backend.alerts.commands import Parsed, ShownKills, incoming, parse
from backend.dashboard.overview import AccountView, BotView, Overview, PositionView, PriceView
from shared.commands import BotReply, Closing, KillReply, NotClosed
from shared.heartbeat import EngineHeartbeat

T0 = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)  # 10:00 in Manila
CHAT = 42
BTC = "BTC/USDT"


# --- Reading a command ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("/kill", Parsed("kill", ())),
        ("/kill@crypto_trading_guy_bot", Parsed("kill", ())),
        ("/PAUSE btc-ema", Parsed("pause", ("btc-ema",))),
        ("  /pause   3 ", Parsed("pause", ("3",))),
        ("/start", Parsed("start", ())),
        ("/whatever x y", Parsed("whatever", ("x", "y"))),
        ("kill", None),
        ("", None),
        ("/", None),
        ("/@bot", None),
    ],
)
def test_a_command_is_read_from_its_text(text: str, parsed: Parsed | None) -> None:
    assert parse(text) == parsed


def sender(user: int) -> dict[str, Any]:
    return {"id": user, "is_bot": False, "first_name": "A", "username": f"user{user}"}


def chat(chat_id: int) -> dict[str, Any]:
    return {"id": chat_id, "type": "private"}


def test_a_message_is_read_with_who_sent_it_and_when() -> None:
    update = Update.de_json(
        {
            "update_id": 7,
            "message": {
                "message_id": 70,
                "date": int(T0.timestamp()),
                "chat": chat(CHAT),
                "from": sender(CHAT),
                "text": "/status",
            },
        },
        None,
    )

    came = incoming(update)

    assert (came.update_id, came.chat_id, came.user_id, came.username) == (7, CHAT, CHAT, "user42")
    assert (came.text, came.button, came.date) == ("/status", None, T0)


def test_a_button_press_is_read_with_its_message() -> None:
    update = Update.de_json(
        {
            "update_id": 8,
            "callback_query": {
                "id": "cq-1",
                "from": sender(CHAT),
                "chat_instance": "ci",
                "data": replies.KILL_STOP,
                "message": {
                    "message_id": 71,
                    "date": int(T0.timestamp()),
                    "chat": chat(CHAT),
                    "text": replies.KILL_PROMPT,
                },
            },
        },
        None,
    )

    came = incoming(update)

    assert (came.chat_id, came.button, came.callback_id, came.message_id) == (
        CHAT,
        replies.KILL_STOP,
        "cq-1",
        71,
    )
    assert came.text is None


def test_shown_kills_keep_only_the_latest() -> None:
    shown = ShownKills(size=2)
    first, second, third = uuid4(), uuid4(), uuid4()
    for command_id in (first, second, third):
        shown.add(command_id)
    shown.discard(second)
    shown.discard(uuid4())  # never added: nothing happens

    assert str(first) not in shown and str(second) not in shown
    assert str(third) in shown


# --- The replies ------------------------------------------------------------------------


def account(**fields: Any) -> AccountView:
    base: dict[str, Any] = {
        "mode": "paper",
        "currency": "USDT",
        "equity": Decimal("3050.004"),
        "deposits": Decimal("3000.5"),
        "today_pnl": Decimal("-100.5"),
        "today_pnl_pct": Decimal("-3.19"),
        "total_pnl": Decimal("49.5"),
        "total_pnl_pct": Decimal("1.65"),
        "peak": Decimal("3150.5"),
        "drawdown_pct": Decimal("3.19"),
    }
    return AccountView(**{**base, **fields})


def bot(bot_id: int, name: str, status: str, **fields: Any) -> BotView:
    base: dict[str, Any] = {
        "exchange": "mexc",
        "symbol": BTC,
        "timeframe": "1h",
        "mode": "paper",
        "strategy": "ema_crossover",
        "paused_reason": None,
        "paused_at": None,
        "stopped_reason": None,
        "stopped_at": None,
    }
    return BotView(id=bot_id, name=name, status=status, **{**base, **fields})


POSITION = PositionView(
    bot_id=1,
    bot_name="alpha",
    exchange="mexc",
    symbol=BTC,
    amount=Decimal("5.000"),
    avg_entry=Decimal("100"),
    bid=Decimal("90"),
    price_ts=T0,
    value=Decimal("450"),
    pnl=Decimal("-50.5"),
    pnl_pct=Decimal("-10.10"),
    stop=None,
    take_profit=None,
    opened_at=T0 - timedelta(days=1),
)


def beat(ago: timedelta) -> EngineHeartbeat:
    return EngineHeartbeat(
        ts=T0 - ago,
        started_at=T0 - timedelta(hours=3),
        mode="paper",
        beat=100,
        exchange="mexc",
        symbols=[BTC],
    )


def overview(
    *,
    engine: EngineHeartbeat | None = None,
    positions: list[PositionView] | None = None,
    bots: list[BotView] | None = None,
    prices: list[PriceView] | None = None,
    **account_fields: Any,
) -> Overview:
    return Overview(
        server_time=T0,
        mode="paper",
        engine=engine,
        account=account(**account_fields),
        positions=positions or [],
        bots=bots or [],
        prices=prices or [],
    )


def test_status_says_the_engine_is_up_and_lists_bots_positions_and_the_oldest_price() -> None:
    text = replies.status(
        overview(
            engine=beat(timedelta(seconds=4)),
            positions=[POSITION],
            bots=[
                bot(1, "alpha", "running"),
                bot(2, "beta", "paused", paused_reason="paused from telegram"),
                bot(3, "gamma", "running"),
                bot(4, "delta", "stopped", stopped_reason="kill switch from telegram"),
            ],
            prices=[
                PriceView(exchange="mexc", symbol=BTC, bid=None, ask=None, ts=T0),
                PriceView(
                    exchange="mexc",
                    symbol="ETH/USDT",
                    bid=None,
                    ask=None,
                    ts=T0 - timedelta(seconds=45),
                ),
                PriceView(exchange="mexc", symbol="SOL/USDT", bid=None, ask=None, ts=None),
            ],
        )
    )

    assert text.splitlines() == [
        "Engine up: heartbeat 4 s ago (paper, mexc), running since 31 Oct 07:00",
        "Bots: running alpha, gamma; paused beta; stopped delta",
        "  beta paused: paused from telegram",
        "Open positions: 1",
        "  alpha · BTC/USDT: 5, -50.5 USDT (-10.1%)",
        "Oldest price: ETH/USDT, 45 s old (stale)",
        "No price yet: SOL/USDT",
    ]


@pytest.mark.parametrize(
    ("engine", "line"),
    [
        (None, "Engine: no heartbeat ever (has it run?)"),
        (beat(timedelta(seconds=30)), "Engine up: heartbeat 30 s ago (paper, mexc)"),
        (beat(timedelta(seconds=31)), "Engine DOWN? No heartbeat for 31 s (last 31 Oct 09:59)"),
        (beat(timedelta(minutes=5)), "Engine DOWN? No heartbeat for 5 min (last 31 Oct 09:55)"),
        (beat(timedelta(hours=3)), "Engine DOWN? No heartbeat for 3 h (last 31 Oct 07:00)"),
    ],
)
def test_status_says_how_old_the_engine_s_heartbeat_is(
    engine: EngineHeartbeat | None, line: str
) -> None:
    first = replies.status(overview(engine=engine)).splitlines()[0]

    assert first.startswith(line)


def test_status_with_nothing_yet() -> None:
    lines = replies.status(overview(engine=beat(timedelta(seconds=1)))).splitlines()

    assert lines[1:] == ["Bots: none yet", "Open positions: none"]


def test_status_without_the_database_still_says_how_the_engine_is() -> None:
    text = replies.status_without_db(beat(timedelta(minutes=5)), T0)

    assert text.splitlines() == [
        "Engine DOWN? No heartbeat for 5 min (last 31 Oct 09:55)",
        "Couldn't read the database in time. Bots and positions unknown.",
    ]


def test_pnl_signs_every_gain_and_loss_and_rounds_to_the_cent() -> None:
    text = replies.pnl(overview(positions=[POSITION]))

    assert text.splitlines() == [
        "Equity 3,050 USDT",
        "Today -100.5 USDT (-3.19%)",
        "Total +49.5 USDT (+1.65%)",
        "Drawdown 3.19% (peak 3,150.5 USDT)",
        "Open positions:",
        "  alpha · BTC/USDT: 5, -50.5 USDT (-10.1%)",
    ]


def test_pnl_with_no_deposits_has_no_percent() -> None:
    text = replies.pnl(
        overview(
            equity=Decimal(0),
            deposits=Decimal(0),
            today_pnl=Decimal(0),
            today_pnl_pct=None,
            total_pnl=Decimal(0),
            total_pnl_pct=None,
            peak=Decimal(0),
            drawdown_pct=Decimal(0),
        )
    )

    assert text.splitlines()[1:3] == ["Today 0 USDT", "Total 0 USDT"]


def test_the_kill_reply_says_what_the_kill_switch_did_as_its_alert_does() -> None:
    reply = KillReply(
        id=uuid4(),
        ok=True,
        stopped=[1, 2],
        canceled=["b1-x-b"],
        closing=[Closing(bot_id=1, symbol=BTC, amount=Decimal("5"))],
        not_closed=[NotClosed(bot_id=2, symbol="ETH/USDT", reason="below the minimums")],
        took_ms=197,
    )

    assert replies.kill_result(reply, {1: "alpha", 2: "beta"}).splitlines() == [
        "Kill switch done",
        "Stopped 2 bots, cancelled 1 orders, in 197 ms",
        "Selling 1 positions until they're gone",
        "Not closed: beta ETH/USDT: below the minimums",
    ]


def test_a_failed_kill_says_so_and_why() -> None:
    reply = KillReply(id=uuid4(), ok=False, error="database down", took_ms=5)

    lines = replies.kill_result(reply, {}).splitlines()

    assert lines[0] == "Kill switch FAILED (partly done)"
    assert lines[-1] == "Error: database down"


def test_pause_replies() -> None:
    ok = BotReply(id=uuid4(), ok=True, bot_id=1, status="paused", took_ms=3)
    refused = BotReply(id=uuid4(), ok=False, error="Bot 1 is paused, not running.", took_ms=3)

    assert replies.pause_result("alpha", ok, 10) == (
        "alpha: paused. No buys; it still sells and keeps its stops."
    )
    assert replies.pause_result("alpha", refused, 10) == (
        "alpha: not paused. Bot 1 is paused, not running."
    )
    assert "no reply from the engine in 10 s" in replies.pause_result("alpha", None, 10)
    assert replies.unknown_bot("nope", [(1, "alpha"), (3, "gamma")]) == (
        "There's no bot called “nope”. Running: alpha, gamma."
    )
    assert "Use the bot's id" in replies.unknown_bot("nope", None)


def test_the_pause_buttons_are_the_running_bots_then_all_then_cancel() -> None:
    buttons = replies.pause_buttons([(1, "alpha"), (3, "gamma")])

    assert [list(row) for row in buttons] == [
        [("alpha", "pause:1")],
        [("gamma", "pause:3")],
        [("All running bots", replies.PAUSE_ALL)],
        [("Cancel", replies.CANCEL)],
    ]
    # Telegram takes at most 64 bytes of data per button.
    assert all(len(data.encode()) <= 64 for row in buttons for _, data in row)


def test_a_command_that_waited_too_long_says_when_it_was_sent() -> None:
    text = replies.too_old("/kill", T0 - timedelta(minutes=15), T0)

    assert text.startswith("Not run: “/kill” was sent 15 min ago (31 Oct 09:45)")
    assert text.endswith("Send it again if you still want it.")


def test_help_lists_the_commands_and_never_offers_to_start_bots() -> None:
    assert [line.split(":")[0] for line in replies.HELP.splitlines()[1:5]] == [
        "/status",
        "/pnl",
        "/pause",
        "/kill",
    ]
    assert "/start" not in replies.HELP
