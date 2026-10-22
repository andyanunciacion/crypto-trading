"""with_retries: network errors are retried with backoff, everything else is not."""

import pytest
from structlog.testing import capture_logs

from engine.backoff import Backoff
from engine.exchange.errors import (
    AuthError,
    ExchangeError,
    ExchangeNetworkError,
    OrderRejected,
)
from engine.exchange.retry import with_retries


class Script:
    """A call that raises the scripted errors in turn, then returns "ok"."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def no_wait() -> Backoff:
    return Backoff(base=0)


async def test_network_errors_are_retried_until_success() -> None:
    call = Script(ExchangeNetworkError("timeout"), ExchangeNetworkError("503"))

    with capture_logs() as logs:
        result = await with_retries(call, operation="fetch_ticker", attempts=5, backoff=no_wait())

    assert result == "ok"
    assert call.calls == 3
    retries = [e for e in logs if e["event"] == "exchange.retrying"]
    assert [e["attempt"] for e in retries] == [1, 2]
    assert retries[0]["operation"] == "fetch_ticker"
    assert retries[0]["error"] == "ExchangeNetworkError"
    assert retries[0]["log_level"] == "warning"
    (recovered,) = [e for e in logs if e["event"] == "exchange.retry.recovered"]
    assert recovered["attempts"] == 3


async def test_gives_up_after_the_last_attempt() -> None:
    errors = [ExchangeNetworkError(f"timeout {n}") for n in range(5)]
    call = Script(*errors)

    with capture_logs() as logs, pytest.raises(ExchangeNetworkError) as caught:
        await with_retries(call, operation="fetch_candles", attempts=3, backoff=no_wait())

    assert call.calls == 3
    assert caught.value is errors[2]  # the last error, unchanged
    assert len([e for e in logs if e["event"] == "exchange.retrying"]) == 2
    (gave_up,) = [e for e in logs if e["event"] == "exchange.retry.gave_up"]
    assert gave_up["attempts"] == 3
    assert gave_up["log_level"] == "error"


@pytest.mark.parametrize(
    "error",
    [OrderRejected("insufficient balance"), AuthError("bad key"), ExchangeError("odd reply")],
    ids=["rejected", "auth", "bad-data"],
)
async def test_other_errors_are_never_retried(error: ExchangeError) -> None:
    call = Script(error)

    with capture_logs() as logs, pytest.raises(type(error)):
        await with_retries(call, operation="fetch_balances", attempts=5, backoff=no_wait())

    assert call.calls == 1
    assert logs == []


async def test_success_first_time_logs_nothing() -> None:
    with capture_logs() as logs:
        assert await with_retries(Script(), operation="x", attempts=5, backoff=no_wait()) == "ok"

    assert logs == []


async def test_waits_follow_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("engine.exchange.retry.asyncio.sleep", fake_sleep)
    call = Script(ExchangeNetworkError("a"), ExchangeNetworkError("b"))

    await with_retries(call, operation="x", attempts=5, backoff=Backoff(base=1, cap=30))

    # Equal jitter: between half and all of 1 s, then of 2 s.
    assert len(waits) == 2
    assert 0.5 <= waits[0] <= 1
    assert 1 <= waits[1] <= 2


async def test_extra_fields_are_logged() -> None:
    call = Script(ExchangeNetworkError("timeout"))

    with capture_logs() as logs:
        await with_retries(
            call, operation="fetch_ticker", attempts=2, backoff=no_wait(), symbol="BTC/USDT"
        )

    (retry,) = [e for e in logs if e["event"] == "exchange.retrying"]
    assert retry["symbol"] == "BTC/USDT"


async def test_attempts_must_be_positive() -> None:
    call = Script()

    with pytest.raises(ValueError, match="attempts"):
        await with_retries(call, operation="x", attempts=0, backoff=no_wait())
    assert call.calls == 0
