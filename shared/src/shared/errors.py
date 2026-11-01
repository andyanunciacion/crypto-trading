"""Error reporting to Sentry for the long-running services (P7-4, D62).

The engine, the backtester, the API and the alerts service each call `init_errors()` at
startup and hand the `ErrorForwarder` it returns to `configure_logging()`. With
SENTRY_DSN blank (a fresh clone, the e2e stack) nothing is sent anywhere.

Sentry receives:
- crashes: an exception that escapes `asyncio.run` (Sentry's excepthook), an unhandled
  exception in an API route (its FastAPI integration), and stdlib `logging` errors from
  libraries (e.g. asyncio's "Task exception was never retrieved");
- every ERROR or CRITICAL structlog line, through `ErrorForwarder`. Most of our errors are
  caught and logged (`bot.error` never stops the other bots), so this is where they come
  from. A line logged inside an `except` block carries the exception and its stack trace,
  with no change where it's logged. The same event for the same bot goes at most once
  per 10 minutes (the free tier counts every event); the next one says how many were held.
  INFO and WARNING lines become breadcrumbs: the trail shown before an error.

Never sent: local variables in stack frames (they could hold a password, a TOTP code or a
key), request bodies (`/auth/login` carries the password and the code), cookies and
headers (`send_default_pii` off). Every event and breadcrumb is scrubbed of the secret
values in the environment and anything shaped like a Telegram bot token, which sits in
every Telegram request's URL (Sentry records URLs by itself). Performance tracing is off.

`python -m shared.errors test` sends a deliberate exception, to check the setup.
"""

import argparse
import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

import sentry_sdk
import structlog
from pydantic import BaseModel, SecretStr
from sentry_sdk.integrations import DidNotEnable, Integration
from sentry_sdk.transport import Transport
from sentry_sdk.types import Breadcrumb, BreadcrumbHint, Event, Hint
from structlog.typing import EventDict, WrappedLogger

from shared.config import Settings, load_settings
from shared.logs import SKIPPED_EVENTS, configure_logging, json_default

QUIET_SECONDS = 600.0  # the same event for the same bot: at most once per 10 minutes
REDACTED = "<redacted>"
# Secrets every service's environment may hold (Compose gives each one all of .env),
# scrubbed even from a service whose settings don't read them.
SECRET_ENV_VARS = (
    "POSTGRES_PASSWORD",
    "JWT_SECRET",
    "ENCRYPTION_KEY",
    "TELEGRAM_BOT_TOKEN",
    "SENTRY_DSN",
)
# Shorter values aren't replaced: a short password like "crypto" would blank out every
# "crypto_trading" in a stack trace, and real secrets here are long (D44, D50).
MIN_SECRET_LENGTH = 8
# A Telegram bot token: the bot's id, a colon and 35 characters. In a URL it follows
# "/bot", so no word boundary marks its start.
TELEGRAM_TOKEN = re.compile(r"(?<![0-9])[0-9]{6,}:[A-Za-z0-9_-]{30,}")

ERROR_LEVELS = frozenset({"error", "critical"})
BREADCRUMB_LEVELS = frozenset({"info", "warning"})
_LINE_PARTS = frozenset({"event", "level", "timestamp"})

log = structlog.get_logger()

T = TypeVar("T")


class Scrubber:
    """Replaces secret values, and anything shaped like a Telegram bot token, wherever a
    string appears in an event: its message, exception values, stack frames' source
    lines, breadcrumbs, contexts and extras."""

    def __init__(self, secrets: Iterable[str]) -> None:
        # Longest first, so a secret that contains another is replaced whole.
        self._secrets = sorted(
            {s for s in secrets if len(s) >= MIN_SECRET_LENGTH}, key=len, reverse=True
        )

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        return TELEGRAM_TOKEN.sub(REDACTED, value)

    def walk(self, value: T) -> T:
        """`value` with every string in it scrubbed (dict keys included)."""
        result: Any
        if isinstance(value, str):
            result = self.text(value)
        elif isinstance(value, dict):
            result = {self.walk(k): self.walk(v) for k, v in value.items()}
        elif isinstance(value, list):
            result = [self.walk(v) for v in value]
        elif isinstance(value, tuple):
            result = tuple(self.walk(v) for v in value)
        else:
            return value
        return result  # type: ignore[no-any-return]

    def event(self, event: Event, hint: Hint) -> Event:
        """Sentry's `before_send`."""
        return self.walk(event)

    def breadcrumb(self, crumb: Breadcrumb, hint: BreadcrumbHint) -> Breadcrumb:
        """Sentry's `before_breadcrumb`."""
        return self.walk(crumb)


def secrets_of(settings: BaseModel, environ: dict[str, str] | None = None) -> list[str]:
    """Every secret this process could leak: the settings' `SecretStr` values and the
    well-known secret variables in its environment."""
    env = os.environ if environ is None else environ
    values = [
        value.get_secret_value()
        for name in type(settings).model_fields
        if isinstance(value := getattr(settings, name), SecretStr)
    ]
    values += [env[name] for name in SECRET_ENV_VARS if env.get(name)]
    return values


def plain(fields: dict[str, Any]) -> dict[str, Any]:
    """A log line's fields as plain JSON values, exactly as the Logs page shows them
    (a Decimal stays exact)."""
    result: dict[str, Any] = json.loads(json.dumps(fields, default=json_default))
    return result


class ErrorForwarder:
    """A structlog processor: sends ERROR and CRITICAL lines to Sentry, and keeps INFO and
    WARNING lines as breadcrumbs. Placed before the JSON renderer, it sees each line as a
    dict. It never raises and never waits: the SDK sends from its own thread."""

    def __init__(
        self,
        service: str,
        *,
        enabled: bool,
        environment: str | None = None,
        quiet_seconds: float = QUIET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.service = service
        self.enabled = enabled
        self.environment = environment
        self._quiet_seconds = quiet_seconds
        self._clock = clock
        # (event, bot_id) -> (when last sent, how many held back since)
        self._sent: dict[tuple[str, object], tuple[float, int]] = {}

    def announce(self) -> None:
        """Say once at startup whether errors go to Sentry."""
        if self.enabled:
            log.info("errors.reporting", service=self.service, environment=self.environment)
        else:
            log.info("errors.off", service=self.service, hint="set SENTRY_DSN to report errors")

    def __call__(self, logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        if self.enabled:
            # Reporting an error must never break logging, nor what was being logged.
            with contextlib.suppress(Exception):
                self._forward(event_dict)
        return event_dict

    def _forward(self, event_dict: EventDict) -> None:
        level = event_dict.get("level")
        event = event_dict.get("event")
        if not isinstance(event, str) or level not in ERROR_LEVELS | BREADCRUMB_LEVELS:
            return
        fields = plain({k: v for k, v in event_dict.items() if k not in _LINE_PARTS})
        if level in BREADCRUMB_LEVELS:
            if event not in SKIPPED_EVENTS:
                sentry_sdk.add_breadcrumb(category="log", message=event, level=level, data=fields)
            return
        bot_id = event_dict.get("bot_id")
        held = self._held(event, bot_id)
        if held is None:
            return
        tags = {"event": event, **({"bot_id": str(bot_id)} if bot_id is not None else {})}
        extras = {"held_back": held} if held else None
        # Inside an `except` block, the exception being handled: its stack trace.
        handling = sys.exc_info()[1]
        if handling is not None:
            sentry_sdk.capture_exception(
                handling, tags=tags, contexts={"log": fields}, extras=extras
            )
        else:
            sentry_sdk.capture_message(
                event,
                level="fatal" if level == "critical" else "error",
                tags=tags,
                contexts={"log": fields},
                extras=extras,
                fingerprint=[self.service, event],  # one issue per event, not per text
            )

    def _held(self, event: str, bot_id: object) -> int | None:
        """How many of this (event, bot) were held back before this one, or None to hold
        this one back too."""
        now = self._clock()
        key = (event, bot_id)
        last = self._sent.get(key)
        if last is not None and now - last[0] < self._quiet_seconds:
            self._sent[key] = (last[0], last[1] + 1)
            return None
        self._sent[key] = (now, 0)
        return 0 if last is None else last[1]


def _web_integrations() -> list[Integration]:
    """Report only unhandled exceptions from API routes: a deliberate 5xx (the kill
    switch's 504) has its own error line, which the forwarder sends. Empty in the
    engine's image, which has no FastAPI."""
    try:
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except (ImportError, DidNotEnable):
        return []
    return [
        StarletteIntegration(failed_request_status_codes=set()),
        FastApiIntegration(failed_request_status_codes=set()),
    ]


def init_errors(
    settings: Settings,
    service: str,
    *,
    transport: Transport | None = None,
    environ: dict[str, str] | None = None,
) -> ErrorForwarder:
    """Start Sentry for `service` if SENTRY_DSN is set, and return the forwarder for
    `configure_logging()` (disabled without a DSN). `transport` is for tests."""
    if settings.sentry_dsn is None:
        return ErrorForwarder(service, enabled=False)
    scrubber = Scrubber(secrets_of(settings, environ))
    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.sentry_environment,
        server_name=service,  # not the container's random hostname
        traces_sample_rate=0,  # errors only: no performance tracing
        data_collection={
            "user_info": False,  # no user or IP
            "cookies": {"mode": "off"},  # the session cookie
            "http_headers": {"request": {"mode": "off"}},
            "http_bodies": [],  # /auth/login's password and code, exchange keys
            "url_query_params": {"mode": "off"},
            "database_query_data": False,
            "queues": False,
            "stack_frame_variables": False,  # a frame's locals may hold any secret
        },
        before_send=scrubber.event,
        before_breadcrumb=scrubber.breadcrumb,
        integrations=_web_integrations(),
        transport=transport,
    )
    sentry_sdk.set_tag("service", service)
    sentry_sdk.set_tag("mode", settings.mode)
    return ErrorForwarder(service, enabled=True, environment=settings.sentry_environment)


class SentryTestError(Exception):
    """The deliberate exception `python -m shared.errors test` sends."""


def _deliberate_failure(service: str) -> None:
    raise SentryTestError(f"a deliberate test exception from the {service} service")


def send_test(service: str) -> str | None:
    """Raise and catch a deliberate exception, log it as an error (so it goes through the
    forwarder as a real error line would), wait until it's sent, and return its event id."""
    try:
        _deliberate_failure(service)
    except SentryTestError:
        log.error("errors.test", service=service, hint="sent by python -m shared.errors test")
    event_id = sentry_sdk.last_event_id()
    sentry_sdk.flush(timeout=10)
    return event_id


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m shared.errors", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser("test", help="send a deliberate exception to Sentry")
    test.add_argument("--service", default="cli", help="the service tag (default: cli)")
    args = parser.parse_args(argv)
    settings = load_settings()
    errors = init_errors(settings, args.service)
    configure_logging(settings.log_level, sys.stderr, errors=errors)
    if not errors.enabled:
        print("Not sent: SENTRY_DSN isn't set (see .env.example).", file=sys.stderr)
        sys.exit(1)
    event_id = send_test(args.service)
    if event_id is None:
        print("Not sent: Sentry made no event (see the log above).", file=sys.stderr)
        sys.exit(1)
    print(f"Sent to Sentry as event {event_id} (service {args.service}).")


if __name__ == "__main__":
    main()
