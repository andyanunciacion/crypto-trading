"""The text of each Telegram message (pure).

Plain text, short enough to read on a lock screen: the first line says what happened
and to which bot, the next ones the numbers. Times are Manila's (UX7). Decimals are
printed from their strings, never through a float (D9), and P&L carries a sign (UX6).
"""

from collections.abc import Mapping
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from pydantic import JsonValue

from backend.alerts.summary import DaySummary
from backend.alerts.trades import TradeFacts
from shared.account import MANILA
from shared.alerts import QUIET, Alert, Passed

CENT = Decimal("0.01")

EXIT_KINDS = {
    "stop_loss": "stop-loss",
    "trailing_stop": "trailing stop",
    "take_profit": "take-profit",
    "kill_switch": "kill switch",
    "close": "stopped with its position closed",
    "switch": "strategy switch (close first)",
}
STOP_LEVELS = {"stop_loss": "stop", "trailing_stop": "stop", "take_profit": "take_profit"}


def manila(ts: datetime) -> str:
    return ts.astimezone(MANILA).strftime("%d %b %H:%M")


def number(value: Decimal | JsonValue) -> str:
    """A Decimal (or a Decimal's string) with thousands separators and no trailing zeros."""
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        return str(value)
    if not d.is_finite():
        return str(d)
    return f"{d.normalize():,f}"


def signed(value: Decimal) -> str:
    return f"+{number(value)}" if value > 0 else number(value)


def money(value: Decimal, currency: str) -> str:
    """An amount of money, to the cent: P&L and equity carry far more digits than a phone
    needs (a fill's price times its amount)."""
    return f"{number(value.quantize(CENT, ROUND_HALF_EVEN))} {currency}"


def signed_money(value: Decimal, currency: str) -> str:
    return f"{signed(value.quantize(CENT, ROUND_HALF_EVEN))} {currency}"


def _quote(symbol: str) -> str:
    return symbol.partition("/")[2] or "USDT"


def _bot(alert: Alert, names: Mapping[int, str]) -> str:
    bot_id = alert.line.bot_id
    if bot_id is None:
        return ""
    return names.get(bot_id, f"bot {bot_id}")


def _held(alert: Alert, passed: Passed) -> list[str]:
    """What the quieter held back before this one, and that it will again."""
    lines = []
    if passed.held and passed.since is not None:
        lines.append(f"(+{passed.held} more like this since {manila(passed.since)})")
    window = QUIET.get(alert.event)
    if window is not None and alert.quiet_key is not None:
        minutes = int(window.total_seconds() // 60)
        span = "hour" if minutes == 60 else f"{minutes} minutes"
        lines.append(f"More like this in the next {span} are held back.")
    return lines


def render(
    alert: Alert,
    *,
    names: Mapping[int, str],
    passed: Passed,
    trade: TradeFacts | None = None,
) -> str:
    """The message for `alert`. `names` maps bot ids to names (missing: "bot <id>");
    `trade` is a trade alert's order as stored (None: what the log line says)."""
    match alert.event:
        case "trade":
            body = _trade(trade) if trade is not None else _trade_from_line(alert, names)
        case "stop":
            body = _stop(alert, names)
        case "risk_block" | "risk_resize":
            body = _risk(alert, names)
        case "breaker":
            body = _breaker(alert, names)
        case "switch":
            body = _switch(alert, names)
        case "kill_switch":
            body = _kill(alert, names)
        case _:
            body = _error(alert, names)
    ts = alert.line.ts
    return "\n".join([*body, *_held(alert, passed), *([manila(ts)] if ts else [])])


def _trade(t: TradeFacts) -> list[str]:
    base = t.symbol.partition("/")[0]
    quote = _quote(t.symbol)
    head = f"{t.side.upper()} {t.bot_name} · {t.symbol} ({t.mode})"
    if t.status == "canceled":
        head += ", part filled before it was cancelled"
    lines = [
        head,
        f"{number(t.filled)} {base} at {number(t.avg_price)} {quote} on average",
        f"Worth {money(t.cost, quote)}, fee {number(t.fee)} {t.fee_currency}",
    ]
    if t.exit_kind is not None:
        lines.append(f"Why: {EXIT_KINDS.get(t.exit_kind, t.exit_kind)}")
    elif t.strategy is not None:
        why = f"{t.strategy} signal"
        lines.append(f"Why: {why}: {t.signal_reason}" if t.signal_reason else f"Why: {why}")
    if t.realized_pnl is not None:
        lines.append(f"Trade closed: {signed_money(t.realized_pnl, quote)} realized, after fees")
    return lines


def _trade_from_line(alert: Alert, names: Mapping[int, str]) -> list[str]:
    """When the order's rows can't be read (the database is down): the line's own fields."""
    f = alert.line.fields
    side = str(f.get("side", "order")).upper()
    lines = [f"{side} {_bot(alert, names)} · {f.get('symbol', '?')}"]
    lines.append(f"Filled {number(f.get('filled', '?'))}")
    if "price" in f:
        lines[-1] += f", last at {number(f['price'])}"
    lines.append(f"Order {f.get('client_order_id', '?')} (details unavailable)")
    return lines


def _stop(alert: Alert, names: Mapping[int, str]) -> list[str]:
    f = alert.line.fields
    kind = str(f.get("kind", "stop"))
    label = EXIT_KINDS.get(kind, kind)
    lines = [f"{label[0].upper()}{label[1:]} hit: {_bot(alert, names)} · {f.get('symbol', '?')}"]
    levels = f.get("levels")
    level = levels.get(STOP_LEVELS.get(kind, "stop")) if isinstance(levels, dict) else None
    reached = f"Bid {number(f.get('bid', '?'))}"
    if level is not None:
        reached += f" reached {number(level)}"
    lines.append(f"{reached}; selling {number(f.get('amount', '?'))} at {number(f.get('limit'))}")
    return lines


def _risk(alert: Alert, names: Mapping[int, str]) -> list[str]:
    f = alert.line.fields
    what = "Buy blocked" if alert.event == "risk_block" else "Buy shrunk"
    lines = [f"{what}: {_bot(alert, names)} · {f.get('symbol', '?')}"]
    lines.append(f"{f.get('rule', '?')}: {f.get('detail', '')}".rstrip(": "))
    if alert.event == "risk_resize" and "requested_amount" in f:
        lines.append(f"Asked {number(f['requested_amount'])}, placed {number(f.get('amount'))}")
    return lines


def _breaker(alert: Alert, names: Mapping[int, str]) -> list[str]:
    f = alert.line.fields
    return [
        f"Bot paused by the error breaker: {_bot(alert, names)}",
        str(f.get("detail", "")),
        "It sells and keeps its stops, but buys nothing until you Start it on the Bots page.",
    ]


def _config(value: JsonValue) -> str:
    if not isinstance(value, dict):
        return "?"
    return f"#{value.get('id')} {value.get('strategy')}"


def _switch(alert: Alert, names: Mapping[int, str]) -> list[str]:
    f = alert.line.fields
    trigger = {"manual": "by hand", "rollback": "rollback", "regime": "by the regime"}.get(
        str(f.get("trigger")), str(f.get("trigger"))
    )
    lines = [
        f"Strategy switched ({trigger}): {_bot(alert, names)}",
        f"{_config(f.get('old'))} → {_config(f.get('new'))}, policy {f.get('policy')}",
    ]
    if f.get("reason"):
        lines.append(f"Why: {f['reason']}")
    return lines


def _kill(alert: Alert, names: Mapping[int, str]) -> list[str]:
    f = alert.line.fields
    stopped = f.get("stopped")
    canceled = f.get("canceled")
    closing = f.get("closing")
    not_closed = f.get("not_closed")
    head = "Kill switch done" if f.get("ok") is not False else "Kill switch FAILED (partly done)"
    lines = [
        head,
        f"Stopped {len(stopped) if isinstance(stopped, list) else 0} bots, "
        f"cancelled {len(canceled) if isinstance(canceled, list) else 0} orders, "
        f"in {f.get('took_ms')} ms",
    ]
    if isinstance(closing, list) and closing:
        lines.append(f"Selling {len(closing)} positions until they're gone")
    if isinstance(not_closed, list):
        for item in not_closed:
            if isinstance(item, dict):
                bot_id = item.get("bot_id")
                name = names.get(bot_id, f"bot {bot_id}") if isinstance(bot_id, int) else "?"
                lines.append(f"Not closed: {name} {item.get('symbol')}: {item.get('reason')}")
    if f.get("error"):
        lines.append(f"Error: {f['error']}")
    return lines


def _error(alert: Alert, names: Mapping[int, str]) -> list[str]:
    line = alert.line
    f = line.fields
    bot = _bot(alert, names)
    lines = [f"Error in {line.service}: {line.event}" + (f" ({bot})" if bot else "")]
    if "error" in f or "detail" in f:
        lines.append(f"{f.get('error', '')}: {f.get('detail', '')}".strip(": "))
    else:
        shown = ", ".join(f"{k}={v}" for k, v in f.items())
        lines.append(shown[:500])
    return lines


def render_summary(s: DaySummary) -> str:
    a = s.overview.account
    cur = a.currency
    lines = [
        f"Daily summary for {s.day:%a %d %b} (Manila)",
        f"Equity {money(a.equity, cur)}; total {signed_money(a.total_pnl, cur)}"
        + (f" ({signed(a.total_pnl_pct)}%)" if a.total_pnl_pct is not None else "")
        + f"; drawdown {number(a.drawdown_pct)}%",
        f"Yesterday: {s.trades_closed} trades closed, {signed_money(s.realized_pnl, cur)} "
        f"realized after fees; {s.orders_filled} orders filled; {s.switches} strategy switches",
    ]
    if s.blocked:
        rules = ", ".join(f"{rule} {count}" for rule, count in s.blocked.items())
        lines.append(f"Buys blocked: {rules}")
    positions = s.overview.positions
    lines.append(f"Open positions: {len(positions) or 'none'}")
    for p in positions:
        lines.append(
            f"  {p.bot_name} · {p.symbol}: {number(p.amount)} ({signed_money(p.pnl, cur)})"
        )
    by_status: dict[str, list[str]] = {}
    for bot in s.overview.bots:
        by_status.setdefault(bot.status, []).append(bot.name)
    if by_status:
        lines.append(
            "Bots: " + "; ".join(f"{status} {', '.join(n)}" for status, n in by_status.items())
        )
    for bot in s.overview.bots:
        why = bot.paused_reason if bot.status == "paused" else None
        if why:
            lines.append(f"  {bot.name} paused: {why}")
    return "\n".join(lines)
