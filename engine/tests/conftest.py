"""Offline replay of the exchange for the adapter tests.

`ReplaySession` stands in for the aiohttp session CCXT would open. CCXT still builds,
signs and parses every request for real; only the socket is replaced, by recorded
JSON from fixtures/ (MEXC by default). A request with no route fails the test, so a
test can't quietly reach the network.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from urllib.parse import parse_qsl, urlsplit

import pytest
from pydantic import SecretStr

from engine.exchange.adapter import ExchangeAdapter

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class Reply:
    fixture: str | None = None  # a file under fixtures/, e.g. "mexc/account.json"
    body: str | None = None  # or an inline body
    status: int = 200
    error: BaseException | None = None  # raised instead of replying: the network failed

    def text(self) -> str:
        if self.fixture is not None:
            return (FIXTURES_DIR / self.fixture).read_text(encoding="utf-8")
        return self.body or ""


@dataclass(frozen=True)
class SentRequest:
    method: str
    host: str
    path: str
    query: dict[str, str]


# MEXC, keyed by (method, path).
ROUTES: dict[tuple[str, str], Reply] = {
    ("GET", "/api/v3/exchangeInfo"): Reply("mexc/exchange_info.json"),
    ("GET", "/api/v1/contract/detail"): Reply("mexc/contract_detail.json"),
    ("GET", "/api/v3/klines"): Reply("mexc/klines_btcusdt_60m.json"),
    ("GET", "/api/v3/ticker/24hr"): Reply("mexc/ticker_24hr_btcusdt.json"),
    ("GET", "/api/v3/account"): Reply("mexc/account.json"),
    ("POST", "/api/v3/order"): Reply("mexc/order_new.json"),
    ("DELETE", "/api/v3/order"): Reply("mexc/order_cancel.json"),
}


class _Response:
    def __init__(self, reply: Reply) -> None:
        self.status = reply.status
        self.reason = "OK" if reply.status == 200 else "Error"
        self.headers = {"Content-Type": "application/json"}
        self._text = reply.text()

    async def text(self, errors: str = "strict") -> str:
        return self._text


class _PendingResponse:
    # CCXT does `async with session.get(...) as response:`.
    def __init__(self, reply: Reply) -> None:
        self._reply = reply

    async def __aenter__(self) -> _Response:
        if self._reply.error is not None:
            raise self._reply.error
        return _Response(self._reply)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


@dataclass
class ReplaySession:
    routes: dict[tuple[str, str], Reply] = field(default_factory=lambda: dict(ROUTES))
    sent: list[SentRequest] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)  # CCXT merges these into requests

    def get(self, url: object, **_: object) -> _PendingResponse:
        return self._request("GET", url)

    def post(self, url: object, **_: object) -> _PendingResponse:
        return self._request("POST", url)

    def delete(self, url: object, **_: object) -> _PendingResponse:
        return self._request("DELETE", url)

    async def close(self) -> None:
        return None

    def sent_to(self, method: str, path: str) -> list[SentRequest]:
        return [r for r in self.sent if (r.method, r.path) == (method, path)]

    def _request(self, method: str, url: object) -> _PendingResponse:
        parts = urlsplit(str(url))
        query = dict(parse_qsl(parts.query))
        self.sent.append(SentRequest(method, parts.hostname or "", parts.path, query))
        reply = self.routes.get((method, parts.path))
        if reply is None:
            raise AssertionError(f"unexpected request in an offline test: {method} {url}")
        return _PendingResponse(reply)


def without_waits(adapter: ExchangeAdapter) -> ExchangeAdapter:
    # The replay answers instantly, so skip CCXT's pacing: its rate limiter would wait
    # seconds per test (MEXC weights exchangeInfo at 25 x 50 ms), and close() waits
    # 250 ms for real sockets to shut. test_rate_limiting_is_on checks the real default.
    adapter._exchange.enableRateLimit = False
    adapter._exchange.timeout_on_exit = 0
    return adapter


@pytest.fixture
def replay() -> ReplaySession:
    return ReplaySession()


@pytest.fixture
async def adapter(replay: ReplaySession) -> AsyncIterator[ExchangeAdapter]:
    """Public data only: no key, trading off (the defaults)."""
    async with without_waits(ExchangeAdapter(session=replay)) as exchange:
        yield exchange


@pytest.fixture
async def trading_adapter(replay: ReplaySession) -> AsyncIterator[ExchangeAdapter]:
    """A fake key and trading switched on, as LiveBroker will build it in P9."""
    exchange = ExchangeAdapter(
        api_key=SecretStr("test-api-key"),
        api_secret=SecretStr("test-api-secret"),
        trading_enabled=True,
        session=replay,
    )
    async with without_waits(exchange):
        yield exchange
