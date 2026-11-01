"""Error reporting to Sentry (shared/errors.py, P7-4, D62): what's sent, what's held
back, and that no secret leaves in an event. A fake transport keeps each event instead
of sending it."""

import io
from typing import Any

import pytest
import sentry_sdk
import structlog
from pydantic import SecretStr

from shared.config import Settings
from shared.errors import (
    REDACTED,
    ErrorForwarder,
    Scrubber,
    SentryTestError,
    init_errors,
    secrets_of,
    send_test,
)
from shared.logs import configure_logging
from shared.testing import SentryEvents

DSN = "https://0123456789abcdef0123456789abcdef@o1.ingest.sentry.io/1"
PASSWORD = "db-password-for-tests"
BOT_TOKEN = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawz"


def settings(dsn: str | None = DSN) -> Settings:
    return Settings(
        _env_file=None,
        postgres_user="crypto",
        postgres_password=SecretStr(PASSWORD),
        postgres_db="crypto_trading",
        sentry_dsn=None if dsn is None else SecretStr(dsn),
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def transport(sentry_events: SentryEvents) -> SentryEvents:
    return sentry_events


def start(
    transport: SentryEvents, service: str = "engine", clock: Clock | None = None
) -> ErrorForwarder:
    """Sentry with the fake transport, and structlog through its forwarder."""
    errors = init_errors(
        settings(), service, transport=transport, environ={"TELEGRAM_BOT_TOKEN": BOT_TOKEN}
    )
    if clock is not None:
        errors = ErrorForwarder(service, enabled=True, clock=clock)
    configure_logging("info", io.StringIO(), errors=errors)
    return errors


def frames(event: dict[str, Any]) -> list[dict[str, Any]]:
    [exception] = event["exception"]["values"]
    found: list[dict[str, Any]] = exception["stacktrace"]["frames"]
    return found


def fail_deep(detail: str) -> None:
    raise ValueError(detail)


def test_an_error_logged_while_handling_an_exception_carries_its_stack_trace(
    transport: SentryEvents,
) -> None:
    start(transport)
    log = structlog.get_logger()
    secret_local = PASSWORD  # a local variable holding a secret, in the failing frame

    try:
        fail_deep("the database said no")
    except ValueError as exc:
        log.error("bot.error", bot_id=3, name="btc", error=type(exc).__name__, detail=str(exc))

    [event] = transport.events
    [exception] = event["exception"]["values"]
    assert (exception["type"], exception["value"]) == ("ValueError", "the database said no")
    assert [f["function"] for f in frames(event)][-2:] == [
        "test_an_error_logged_while_handling_an_exception_carries_its_stack_trace",
        "fail_deep",
    ]
    assert all("vars" not in f for f in frames(event))  # no local variables, ever
    assert event["tags"] == {
        "service": "engine",
        "mode": "paper",
        "event": "bot.error",
        "bot_id": "3",
    }
    assert event["contexts"]["log"] == {
        "bot_id": 3,
        "name": "btc",
        "error": "ValueError",
        "detail": "the database said no",
    }
    assert event["server_name"] == "engine" and event["environment"] == "local"
    assert secret_local not in str(event)


def test_an_error_line_with_no_exception_is_a_message_grouped_by_its_event(
    transport: SentryEvents,
) -> None:
    start(transport, "api")

    structlog.get_logger().error("api.kill.no_reply", waited_s=10, id="k1")

    [event] = transport.events
    assert event["message"] == "api.kill.no_reply"
    assert event["level"] == "error"
    assert event["fingerprint"] == ["api", "api.kill.no_reply"]
    assert event["contexts"]["log"] == {"waited_s": 10, "id": "k1"}
    assert "exception" not in event


def test_info_and_warning_lines_are_breadcrumbs_not_events(transport: SentryEvents) -> None:
    start(transport)
    log = structlog.get_logger()

    log.info("engine.heartbeat", beat=1)  # every 10 s: not worth a breadcrumb
    log.info("bot.order", bot_id=3, side="buy")
    log.warning("candles.live.late", symbol="BTC/USDT")
    log.error("bot.error", bot_id=3)

    [event] = transport.events
    crumbs = [(c["message"], c["level"]) for c in event["breadcrumbs"]["values"]]
    assert crumbs[-2:] == [("bot.order", "info"), ("candles.live.late", "warning")]
    assert "engine.heartbeat" not in [c for c, _ in crumbs]


def test_the_same_event_for_the_same_bot_goes_once_per_10_minutes_then_says_how_many(
    transport: SentryEvents,
) -> None:
    clock = Clock()
    start(transport, clock=clock)
    log = structlog.get_logger()

    log.error("bot.error", bot_id=3)
    log.error("bot.error", bot_id=3)
    log.error("bot.error", bot_id=4)  # another bot: its own
    log.error("bot.levels_failed", bot_id=3)  # another event: its own
    clock.now += 599
    log.error("bot.error", bot_id=3)
    clock.now += 1
    log.error("bot.error", bot_id=3)

    sent = [
        (e["tags"]["event"], e["tags"]["bot_id"], e["extra"].get("held_back"))
        for e in transport.events
    ]
    assert sent == [
        ("bot.error", "3", None),
        ("bot.error", "4", None),
        ("bot.levels_failed", "3", None),
        ("bot.error", "3", 2),
    ]


def test_a_critical_line_is_fatal(transport: SentryEvents) -> None:
    start(transport)

    structlog.get_logger().critical("engine.crashed")

    assert transport.events[0]["level"] == "fatal"


def test_no_secret_leaves_in_an_event_or_a_breadcrumb(transport: SentryEvents) -> None:
    start(transport, "alerts")
    log = structlog.get_logger()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    sentry_sdk.add_breadcrumb(category="httplib", data={"url": url})  # as httpx's would
    log.info("alerts.sending", url=url)
    try:
        raise ConnectionError(f"POST {url} failed; password {PASSWORD}")
    except ConnectionError:
        log.error("alerts.failed", detail=f"token {BOT_TOKEN}", nested={"deep": [PASSWORD]})

    [event] = transport.events
    text = str(event)
    assert BOT_TOKEN not in text and PASSWORD not in text
    assert "bot" + REDACTED + "/sendMessage" in text
    assert event["contexts"]["log"]["nested"] == {"deep": [REDACTED]}


def test_the_scrubber_replaces_long_secrets_and_any_telegram_token() -> None:
    other_token = "987654321:ZZHdqTcvCH1vGWJxfSeofSAs0K5PALDsXYZ"  # not in the environment
    scrubber = Scrubber([PASSWORD, "short", PASSWORD + "-and-more"])

    assert scrubber.text(f"x {PASSWORD}-and-more {PASSWORD} short") == (
        f"x {REDACTED} {REDACTED} short"  # the longer one whole; "short" is too short
    )
    assert scrubber.text(f"/bot{other_token}/getUpdates") == f"/bot{REDACTED}/getUpdates"
    assert scrubber.walk({"k": (PASSWORD, [1, None])}) == {"k": (REDACTED, [1, None])}


def test_the_secrets_are_the_settings_secret_values_and_the_known_variables() -> None:
    found = secrets_of(
        settings(), {"JWT_SECRET": "j" * 40, "TELEGRAM_BOT_TOKEN": BOT_TOKEN, "OTHER": "x" * 40}
    )

    assert set(found) == {PASSWORD, DSN, "j" * 40, BOT_TOKEN}


def test_without_a_dsn_nothing_starts_and_nothing_is_sent(transport: SentryEvents) -> None:
    errors = init_errors(settings(dsn=None), "engine")
    configure_logging("info", io.StringIO(), errors=errors)

    structlog.get_logger().error("bot.error", bot_id=3)

    assert not errors.enabled
    assert not sentry_sdk.get_client().is_active()
    assert transport.events == []


def test_a_failing_capture_never_breaks_logging(
    transport: SentryEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = io.StringIO()
    errors = init_errors(settings(), "engine", transport=transport, environ={})
    configure_logging("info", stdout, errors=errors)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Sentry is broken")

    monkeypatch.setattr(sentry_sdk, "capture_message", broken)
    structlog.get_logger().error("bot.error", bot_id=3)

    assert '"event": "bot.error"' in stdout.getvalue()  # still logged


def test_the_test_command_sends_a_deliberate_exception_with_its_stack_trace(
    transport: SentryEvents,
) -> None:
    start(transport, "backtester")

    event_id = send_test("backtester")

    [event] = transport.events
    assert event["event_id"] == event_id
    [exception] = event["exception"]["values"]
    assert exception["type"] == SentryTestError.__name__
    assert [f["function"] for f in frames(event)][-2:] == ["send_test", "_deliberate_failure"]
    assert event["tags"]["service"] == "backtester"
