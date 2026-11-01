"""What the alerts service sends to Telegram (P7-1, D59): the event types, and which
log lines are one.

The alerts service (backend/alerts/) reads the lines every service mirrors to the Redis
`logs` stream (shared/logs.py, D49) and asks `to_alert()` whether each one is worth a
message. The engine already logs every decision with its inputs, so the log is the event
record, and the engine needed no change to be watched. The price is that a few event
names are now a contract: `engine/tests/test_alert_contract.py` feeds lines the engine
really logs through `to_alert()`, so renaming one fails a test instead of silencing an
alert. That's why this lives in `shared/`: the engine's tests can import it.

Some events can repeat every candle (a daily loss latch blocks every buy until
midnight; a broken feed errors on every price), so `Quieter` holds back an alert like
one sent less than its window ago and counts what it held back.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, JsonValue

from shared.logs import LogEntry

AlertEvent = Literal[
    "trade",
    "stop",
    "risk_block",
    "risk_resize",
    "breaker",
    "switch",
    "kill_switch",
    "error",
    "daily_summary",
    "engine_down",
]
ALERT_EVENTS: tuple[AlertEvent, ...] = get_args(AlertEvent)

# The engine is down when its heartbeat (every 10 s, shared/heartbeat.py) hasn't changed
# for this long (P7-3, D61): longer than a restart or a rebuild takes, so those stay
# quiet, and short enough that the alert comes within PRD's 2 minutes. The alerts
# service looks every `WATCH_SECONDS`, so it's sent at most about 70 s after the last beat.
ENGINE_DOWN_SECONDS = 60
WATCH_SECONDS = 5

# The alerts service's own lines: alerting on them could loop (a failed send logs, the
# log line would be an alert, which fails to send, ...).
ALERTS_SERVICE = "alerts"


@dataclass(frozen=True)
class EventInfo:
    label: str  # as the Settings page shows it
    description: str  # one line: what sends it
    default: bool  # on or off in a new database (migration 0017 seeds these)


EVENTS: dict[AlertEvent, EventInfo] = {
    "trade": EventInfo(
        "Trades",
        "An order filled: one message per order once it's done, with its average price and "
        "why it was placed.",
        True,
    ),
    "stop": EventInfo(
        "Stops hit",
        "A stop-loss, take-profit or trailing stop fired, and the position is being sold.",
        True,
    ),
    "risk_block": EventInfo(
        "Blocked buys",
        "A risk limit or circuit breaker blocked a buy. At most one an hour per bot and rule.",
        True,
    ),
    "risk_resize": EventInfo(
        "Shrunk buys",
        "A buy was shrunk to fit the per-coin limit. Frequent for all-in bots, so off at first.",
        False,
    ),
    "breaker": EventInfo(
        "Bot paused",
        "The error breaker paused a bot after repeated failed orders.",
        True,
    ),
    "switch": EventInfo(
        "Strategy switches",
        "A bot's strategy changed: by hand, by a rollback or by the regime.",
        True,
    ),
    "kill_switch": EventInfo(
        "Kill switch",
        "The kill switch ran: the bots it stopped, the orders it cancelled, what it's selling.",
        True,
    ),
    "error": EventInfo(
        "Errors",
        "A service logged an error. The same error again within 10 minutes is held back.",
        True,
    ),
    "daily_summary": EventInfo(
        "Daily summary",
        "At 08:00 Manila: equity, yesterday's closed trades, blocks and switches, open "
        "positions and bots.",
        True,
    ),
    "engine_down": EventInfo(
        "Engine down",
        "No engine heartbeat for a minute (or Redis unreachable), and again when it's back.",
        True,
    ),
}

# How long an alert like one just sent is held back. Events not here are always sent.
QUIET: dict[AlertEvent, timedelta] = {
    "risk_block": timedelta(hours=1),
    "error": timedelta(minutes=10),
}

_ERROR_LEVELS = frozenset({"error", "critical"})
_DECISIONS: dict[JsonValue, AlertEvent] = {"blocked": "risk_block", "resized": "risk_resize"}


class Alert(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: AlertEvent
    line: LogEntry  # the log line it came from
    # Alerts with the same event and key are one alert repeating (see `Quieter`).
    quiet_key: str | None = None


def to_alert(entry: LogEntry) -> Alert | None:
    """The alert `entry` is, or None if it isn't one."""
    if entry.service == ALERTS_SERVICE:
        return None
    event = _event(entry)
    if event is None:
        return None
    return Alert(event=event, line=entry, quiet_key=_quiet_key(event, entry))


def _event(entry: LogEntry) -> AlertEvent | None:
    fields = entry.fields
    match entry.event:
        case "broker.order.filled":
            # Once per order, when its last slice fills: a big order can fill in 20 (D36).
            return "trade" if fields.get("status") == "closed" else None
        case "broker.order.canceled":
            # An order lives one candle (D36): what filled before it was cancelled traded.
            return "trade" if _positive(fields.get("filled")) else None
        case "bot.exit":
            # The first time the stop fires; `again` is each exit placed after it.
            return "stop" if fields.get("again") is False else None
        case "risk.decision":
            return _DECISIONS.get(fields.get("decision"))
        case "risk.breaker.tripped":
            return "breaker"
        case "bot.switch":
            return "switch"
        case "kill_switch.done":
            return "kill_switch"
    return "error" if entry.level in _ERROR_LEVELS else None


def _positive(value: JsonValue) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return Decimal(value) > 0
    except InvalidOperation:
        return False


def _quiet_key(event: AlertEvent, entry: LogEntry) -> str | None:
    if event == "risk_block":
        return f"{entry.bot_id}:{entry.fields.get('rule')}"
    if event == "error":
        return f"{entry.service}:{entry.event}:{entry.bot_id}"
    return None


@dataclass(frozen=True)
class Passed:
    """An alert to send, and how many like it were held back since the last one sent."""

    held: int = 0
    since: datetime | None = None  # when the first of those came


class Quieter:
    """Holds back an alert like one sent less than its event's window ago (`QUIET`).

    In memory: a restart may send one again sooner. Pass each alert's own time, so a
    backlog read after an outage is judged by when things happened.
    """

    def __init__(self, windows: dict[AlertEvent, timedelta] | None = None) -> None:
        self._windows = QUIET if windows is None else windows
        self._sent: dict[tuple[AlertEvent, str], datetime] = {}
        self._held: dict[tuple[AlertEvent, str], tuple[int, datetime]] = {}

    def check(self, alert: Alert, at: datetime) -> Passed | None:
        """Whether to send `alert` (with what was held back), or None to hold it back."""
        window = self._windows.get(alert.event)
        if window is None or alert.quiet_key is None:
            return Passed()
        key = (alert.event, alert.quiet_key)
        last = self._sent.get(key)
        if last is not None and at - last < window:
            count, since = self._held.get(key, (0, at))
            self._held[key] = (count + 1, since)
            return None
        self._sent[key] = at
        count, first = self._held.pop(key, (0, None))
        return Passed(count, first)
