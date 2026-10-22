"""Retry an exchange call on network errors, with backoff (P1-4).

Only `ExchangeNetworkError` is retried: a timeout, a dropped connection, a rate limit
or maintenance may well pass. Anything else means the exchange understood the request
and said no (or sent something we can't read), and asking again can't change that.

The adapter uses this for reads only. Orders are never resent here: after a network
error the order may already be on the exchange (D27).
"""

import asyncio
from collections.abc import Awaitable, Callable

import structlog

from engine.backoff import Backoff
from engine.exchange.errors import ExchangeNetworkError

log = structlog.get_logger()


async def with_retries[T](
    call: Callable[[], Awaitable[T]],
    *,
    operation: str,
    attempts: int,
    backoff: Backoff,
    **log_fields: object,
) -> T:
    """Await `call()` up to `attempts` times; re-raise the last network error after that.

    Pass a fresh `backoff` per call, so concurrent calls don't share a failure count.
    Each retry is logged with `operation` and `log_fields`.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, not {attempts}")
    attempt = 1
    while True:
        try:
            result = await call()
        except ExchangeNetworkError as exc:
            if attempt >= attempts:
                log.error(
                    "exchange.retry.gave_up",
                    operation=operation,
                    attempts=attempt,
                    error=type(exc).__name__,
                    detail=str(exc),
                    **log_fields,
                )
                raise
            delay = backoff.next_delay()
            log.warning(
                "exchange.retrying",
                operation=operation,
                attempt=attempt,
                of=attempts,
                delay_s=round(delay, 2),
                error=type(exc).__name__,
                detail=str(exc),
                **log_fields,
            )
            await asyncio.sleep(delay)
            attempt += 1
            continue
        if attempt > 1:
            log.info(
                "exchange.retry.recovered", operation=operation, attempts=attempt, **log_fields
            )
        return result
