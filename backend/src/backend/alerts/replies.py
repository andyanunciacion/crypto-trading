"""What the bot answers to Telegram commands (P7-2), and the buttons it shows (pure).

The same style as the alerts (format.py): plain text, Manila times, Decimals from their
strings, P&L signed. The numbers come from the Overview (D45), so they're the ones the
dashboard shows and the risk limits check.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from backend.alerts.format import age, kill_lines, manila, money, number, signed, signed_money
from backend.alerts.telegram import Buttons
from backend.dashboard.overview import Overview, PositionView
from shared.commands import BotReply, KillReply
from shared.heartbeat import ENGINE_STALE_SECONDS, EngineHeartbeat
from shared.prices import PRICE_STALE_SECONDS

# The buttons' data: what a press sends back (commands.py reads it).
KILL_STOP = "kill:stop"
KILL_CLOSE = "kill:close"
PAUSE_ALL = "pause:all"
PAUSE_PREFIX = "pause:"  # + the bot's id
CANCEL = "cancel"

# The menu Telegram shows in the chat (set at startup), and /help.
MENU: list[tuple[str, str]] = [
    ("status", "Engine, bots and open positions"),
    ("pnl", "Equity and profit/loss"),
    ("pause", "Pause a bot: no buys, it still sells and keeps its stops"),
    ("kill", "Kill switch: stop all bots, and choose whether to close positions"),
]

HELP = "\n".join(
    [
        "Commands (only from this chat):",
        *(f"/{name}: {what}" for name, what in MENU),
        "/pause <name or id> pauses that bot at once.",
        "Starting bots again is on the dashboard's Bots page.",
    ]
)

KILL_PROMPT = (
    "Kill switch: stop every bot and cancel every open order?\n"
    "“Stop all + close positions” also sells every position until it's gone."
)
KILL_BUTTONS: Buttons = [
    [("Stop all bots", KILL_STOP)],
    [("Stop all + close positions", KILL_CLOSE)],
    [("Cancel", CANCEL)],
]

PAUSE_PROMPT = "Pause which bot? It stops buying; it still sells and keeps its stops."
NO_BOT_RUNNING = "No bot is running, so there's nothing to pause."
CANCELLED = "Cancelled: nothing was done."
EXPIRED = "These buttons have expired, so nothing was done. Send the command again."
DB_DOWN = "Couldn't read the database in time."


def unknown(name: str) -> str:
    return f"I don't know /{name}.\n\n{HELP}"


def too_old(text: str, sent_at: datetime, now: datetime) -> str:
    return (
        f"Not run: “{text[:64]}” was sent {age((now - sent_at).total_seconds())} ago "
        f"({manila(sent_at)}), while this service wasn't reading. "
        "Send it again if you still want it."
    )


# --- /status and /pnl -------------------------------------------------------------------


def engine_line(beat: EngineHeartbeat | None, now: datetime) -> str:
    if beat is None:
        return "Engine: no heartbeat ever (has it run?)"
    since = (now - beat.ts).total_seconds()
    if since > ENGINE_STALE_SECONDS:
        return f"Engine DOWN? No heartbeat for {age(since)} (last {manila(beat.ts)})"
    return (
        f"Engine up: heartbeat {age(since)} ago ({beat.mode}, {beat.exchange}), "
        f"running since {manila(beat.started_at)}"
    )


def _position(p: PositionView, currency: str) -> str:
    return f"  {p.bot_name} · {p.symbol}: {number(p.amount)}, {signed_money(p.pnl, currency)}" + (
        _pct(p.pnl_pct)
    )


def status(o: Overview) -> str:
    now = o.server_time
    lines = [engine_line(o.engine, now)]
    by_status: dict[str, list[str]] = {}
    for bot in o.bots:
        by_status.setdefault(bot.status, []).append(bot.name)
    if by_status:
        lines.append(
            "Bots: " + "; ".join(f"{s} {', '.join(names)}" for s, names in by_status.items())
        )
    else:
        lines.append("Bots: none yet")
    for bot in o.bots:
        if bot.status == "paused" and bot.paused_reason:
            lines.append(f"  {bot.name} paused: {bot.paused_reason}")
    cur = o.account.currency
    lines.append(f"Open positions: {len(o.positions) or 'none'}")
    lines.extend(_position(p, cur) for p in o.positions)
    lines.extend(_prices(o, now))
    return "\n".join(lines)


def _prices(o: Overview, now: datetime) -> list[str]:
    """The oldest price, as the dashboard's header shows it: a stuck stream can't hide
    behind a busy one (UX5)."""
    timed = [p for p in o.prices if p.ts is not None]
    missing = [p.symbol for p in o.prices if p.ts is None]
    lines = []
    if timed:
        oldest = min(timed, key=lambda p: p.ts or now)
        since = (now - (oldest.ts or now)).total_seconds()
        stale = " (stale)" if since > PRICE_STALE_SECONDS else ""
        lines.append(f"Oldest price: {oldest.symbol}, {age(since)} old{stale}")
    if missing:
        lines.append(f"No price yet: {', '.join(missing)}")
    return lines


def status_without_db(beat: EngineHeartbeat | None, now: datetime) -> str:
    return "\n".join([engine_line(beat, now), f"{DB_DOWN} Bots and positions unknown."])


def _pct(value: Decimal | None) -> str:
    return f" ({signed(value)}%)" if value is not None else ""


def pnl(o: Overview) -> str:
    a = o.account
    cur = a.currency
    lines = [
        f"Equity {money(a.equity, cur)}",
        f"Today {signed_money(a.today_pnl, cur)}{_pct(a.today_pnl_pct)}",
        f"Total {signed_money(a.total_pnl, cur)}{_pct(a.total_pnl_pct)}",
        f"Drawdown {number(a.drawdown_pct)}% (peak {money(a.peak, cur)})",
    ]
    if o.positions:
        lines.append("Open positions:")
        lines.extend(_position(p, cur) for p in o.positions)
    return "\n".join(lines)


# --- /kill ------------------------------------------------------------------------------


def kill_chosen(close_positions: bool) -> str:
    what = "Stop all + close positions" if close_positions else "Stop all bots"
    return f"Kill switch: {what}. Sent to the engine…"


def kill_result(reply: KillReply, names: Mapping[int, str]) -> str:
    return "\n".join(kill_lines(reply.model_dump(mode="json"), names))


def kill_no_reply(wait: float) -> str:
    return (
        f"No reply from the engine in {wait:g} s. The kill stays queued: it runs when the "
        "engine starts, before any bot loads."
    )


# --- /pause -----------------------------------------------------------------------------


def pause_buttons(running: Sequence[tuple[int, str]]) -> Buttons:
    return [
        *([(name, f"{PAUSE_PREFIX}{bot_id}")] for bot_id, name in running),
        [("All running bots", PAUSE_ALL)],
        [("Cancel", CANCEL)],
    ]


def pause_chosen(what: str) -> str:
    return f"Pause {what}. Sent to the engine…"


def unknown_bot(arg: str, running: Sequence[tuple[int, str]] | None) -> str:
    if running is None:
        return f"{DB_DOWN} Use the bot's id: /pause <id>."
    names = ", ".join(name for _, name in running) or "none"
    return f"There's no bot called “{arg[:64]}”. Running: {names}."


def pause_result(name: str, reply: BotReply | None, wait: float) -> str:
    if reply is None:
        return (
            f"{name}: no reply from the engine in {wait:g} s, so the pause was dropped. "
            "Is the engine running?"
        )
    if not reply.ok:
        return f"{name}: not paused. {reply.error or 'The engine refused.'}"
    return f"{name}: paused. No buys; it still sells and keeps its stops."


def paused(results: Sequence[str]) -> str:
    """Each bot's `pause_result()`, and how to undo it."""
    return "\n".join([*results, "Start a paused bot again on the dashboard's Bots page."])
