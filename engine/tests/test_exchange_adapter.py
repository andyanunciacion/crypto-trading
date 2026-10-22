"""ExchangeAdapter against recorded MEXC responses (see conftest.py; no network)."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import FIXTURES_DIR, ReplaySession, Reply, without_waits
from pydantic import ValidationError
from structlog.testing import capture_logs

from engine.exchange.adapter import ExchangeAdapter
from engine.exchange.errors import (
    AuthError,
    ExchangeError,
    ExchangeNetworkError,
    OrderRejected,
    SandboxUnavailable,
    TradingDisabled,
)
from engine.exchange.models import OrderRequest, Ticker

BTC = "BTC/USDT"


def limit_buy(amount: str = "0.0001", price: str = "80000") -> OrderRequest:
    return OrderRequest(
        symbol=BTC,
        side="buy",
        amount=Decimal(amount),
        price=Decimal(price),
        client_order_id="bot1-000001",
    )


# --- Market data ---------------------------------------------------------------------


async def test_candles_are_exact_decimals_in_utc(
    adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    candles = await adapter.fetch_candles(BTC, "1h")

    assert len(candles) == 3
    first = candles[0]
    assert first.ts == datetime(2026, 10, 22, 11, 0, tzinfo=UTC)
    # Same digits as the raw JSON: nothing went through a float.
    assert first.open == Decimal("82040.01")
    assert first.high == Decimal("82042.28")
    assert first.low == Decimal("81760")
    assert first.close == Decimal("81760.01")
    assert first.volume == Decimal("222.40078459")
    assert [c.ts for c in candles] == [first.ts + timedelta(hours=h) for h in range(3)]
    (request,) = replay.sent_to("GET", "/api/v3/klines")
    assert request.query["symbol"] == "BTCUSDT"
    assert request.query["interval"] == "60m"  # MEXC's name for 1h


async def test_numbers_never_pass_through_float(
    adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    # 20 significant digits: a float keeps about 16, so this only survives as a string.
    replay.routes["GET", "/api/v3/klines"] = Reply(
        body='[[1792666800000,"1","1","1","1","12345678.123456789012",1792670400000,"1"]]'
    )

    (candle,) = await adapter.fetch_candles(BTC, "1h")

    assert candle.volume == Decimal("12345678.123456789012")


async def test_candles_pass_since_and_limit(
    adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    await adapter.fetch_candles(BTC, "1h", since=datetime(2026, 10, 22, 11, tzinfo=UTC), limit=3)

    (request,) = replay.sent_to("GET", "/api/v3/klines")
    assert request.query["startTime"] == "1792666800000"
    assert request.query["limit"] == "3"


async def test_candles_reject_naive_since(adapter: ExchangeAdapter) -> None:
    with pytest.raises(ValueError, match="timezone"):
        await adapter.fetch_candles(BTC, "1h", since=datetime(2026, 10, 22, 11))  # noqa: DTZ001


async def test_nan_from_the_exchange_is_an_error_not_a_price(
    adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    replay.routes["GET", "/api/v3/klines"] = Reply(
        body='[[1792666800000,"NaN","1","1","1","1",1792670400000,"1"]]'
    )

    with pytest.raises(ExchangeError, match="unexpected"):
        await adapter.fetch_candles(BTC, "1h")


async def test_ticker(adapter: ExchangeAdapter) -> None:
    ticker = await adapter.fetch_ticker(BTC)

    assert ticker.symbol == BTC
    assert ticker.bid == Decimal("81666.32")
    assert ticker.ask == Decimal("81666.33")
    assert ticker.last == Decimal("81670.41")
    assert ticker.ts.tzinfo is UTC


def test_a_ticker_needs_at_least_one_price() -> None:
    with pytest.raises(ValidationError, match="no price"):
        Ticker(symbol=BTC, ts=datetime(2026, 10, 22, tzinfo=UTC))


# --- Streaming (WebSocket) -------------------------------------------------------------


@dataclass
class FakeStreamClient:
    """Stands in for CCXT's WebSocket client: hands back recorded, already-parsed updates."""

    updates: list[dict[str, object]]
    closed: bool = False

    async def watch_ticker(self, symbol: str) -> dict[str, object]:
        return self.updates.pop(0)

    async def close(self) -> None:
        self.closed = True


def recorded_stream_ticker() -> dict[str, object]:
    raw: dict[str, object] = json.loads(
        (FIXTURES_DIR / "mexc/ws_ticker_btcusdt.json").read_text(encoding="utf-8")
    )
    return raw


async def test_streamed_ticker_has_bid_and_ask_but_no_last(adapter: ExchangeAdapter) -> None:
    # MEXC's spot stream is its book ticker: best bid and ask only, no last trade.
    adapter._stream_clients[BTC] = FakeStreamClient([recorded_stream_ticker()])

    ticker = await adapter.watch_ticker(BTC)

    assert ticker.symbol == BTC
    assert ticker.bid == Decimal("81819.48")
    assert ticker.ask == Decimal("81819.49")
    assert ticker.last is None
    assert ticker.ts == datetime(2026, 10, 22, 14, 40, 55, 344000, tzinfo=UTC)


async def test_streamed_ticker_without_prices_is_an_error(adapter: ExchangeAdapter) -> None:
    empty = recorded_stream_ticker() | {"bid": None, "ask": None}
    adapter._stream_clients[BTC] = FakeStreamClient([empty])

    with pytest.raises(ExchangeError, match="unexpected"):
        await adapter.watch_ticker(BTC)


async def test_stream_client_reuses_the_loaded_markets(
    adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    # Each reconnect builds a new client; it must not download every market again.
    client = await adapter._stream_client(BTC)
    client.timeout_on_exit = 0  # no real socket to wait for
    again = await adapter._stream_client(BTC)

    assert again is client
    assert client.markets is adapter._exchange.markets
    assert client.number is str  # numbers stay strings, as on the REST client
    assert len(replay.sent_to("GET", "/api/v3/exchangeInfo")) == 1  # the REST load only


async def test_close_stream_drops_the_client(adapter: ExchangeAdapter) -> None:
    fake = FakeStreamClient([])
    adapter._stream_clients[BTC] = fake

    await adapter.close_stream(BTC)
    await adapter.close_stream(BTC)  # closing twice is fine

    assert fake.closed
    assert BTC not in adapter._stream_clients


async def test_close_also_closes_the_streams(replay: ReplaySession) -> None:
    fake = FakeStreamClient([])
    async with without_waits(ExchangeAdapter(session=replay)) as exchange:
        exchange._stream_clients[BTC] = fake

    assert fake.closed


async def test_market_rules(adapter: ExchangeAdapter) -> None:
    rules = await adapter.market_rules(BTC)

    assert rules.amount_step == Decimal("0.00000001")
    assert rules.price_step == Decimal("0.01")
    assert rules.min_amount == Decimal("0.000001")
    assert rules.min_cost == Decimal("1")


async def test_unknown_symbol(adapter: ExchangeAdapter) -> None:
    with pytest.raises(ExchangeError):
        await adapter.market_rules("NOPE/USDT")


# --- Balances ------------------------------------------------------------------------


async def test_balances_skip_empty_currencies(trading_adapter: ExchangeAdapter) -> None:
    balances = await trading_adapter.fetch_balances()

    assert set(balances) == {"BTC", "USDT"}  # ETH is all zero
    btc = balances["BTC"]
    assert btc.free == Decimal("0.00123456")
    assert btc.used == Decimal("0.0001")
    assert btc.total == Decimal("0.00133456")
    assert balances["USDT"].total == Decimal("250.5")


async def test_balances_need_a_key(adapter: ExchangeAdapter, replay: ReplaySession) -> None:
    with pytest.raises(AuthError):
        await adapter.fetch_balances()
    assert replay.sent_to("GET", "/api/v3/account") == []


# --- Orders --------------------------------------------------------------------------


async def test_place_limit_order(trading_adapter: ExchangeAdapter, replay: ReplaySession) -> None:
    order = await trading_adapter.place_order(limit_buy())

    assert order.exchange_order_id == "C02__594221748317392896088"
    assert order.client_order_id == "bot1-000001"
    assert order.symbol == BTC
    assert order.side == "buy"
    assert order.type == "limit"
    assert order.price == Decimal("80000")
    assert order.amount == Decimal("0.0001")
    assert order.ts == datetime(2026, 10, 22, 13, 6, 44, 564000, tzinfo=UTC)
    (request,) = replay.sent_to("POST", "/api/v3/order")
    assert request.query["symbol"] == "BTCUSDT"
    assert request.query["side"] == "BUY"
    assert request.query["type"] == "LIMIT"
    assert request.query["quantity"] == "0.0001"
    assert request.query["price"] == "80000"
    assert request.query["newClientOrderId"] == "bot1-000001"


async def test_cancel_order_by_client_order_id(
    trading_adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    order = await trading_adapter.cancel_order(BTC, "bot1-000001")

    assert order.status == "canceled"
    assert order.client_order_id == "bot1-000001"
    assert order.filled == Decimal("0")
    (request,) = replay.sent_to("DELETE", "/api/v3/order")
    assert request.query["origClientOrderId"] == "bot1-000001"
    assert request.query["symbol"] == "BTCUSDT"


async def test_trading_is_off_by_default(adapter: ExchangeAdapter, replay: ReplaySession) -> None:
    with pytest.raises(TradingDisabled):
        await adapter.place_order(limit_buy())
    with pytest.raises(TradingDisabled):
        await adapter.cancel_order(BTC, "bot1-000001")
    assert replay.sent == []


@pytest.mark.parametrize(
    ("amount", "price", "problem"),
    [
        ("0.000000015", "80000", "amount step"),  # BTC amounts go in steps of 0.00000001
        ("0.0001", "80000.001", "price step"),  # prices go in steps of 0.01
        ("0.0000005", "80000", "minimum amount"),  # min 0.000001 BTC
        ("0.00001", "80000", "minimum cost"),  # 0.8 USDT, min 1 USDT
    ],
)
async def test_orders_that_dont_fit_the_market_are_refused_before_sending(
    trading_adapter: ExchangeAdapter,
    replay: ReplaySession,
    amount: str,
    price: str,
    problem: str,
) -> None:
    with pytest.raises(OrderRejected, match=problem):
        await trading_adapter.place_order(limit_buy(amount, price))
    assert replay.sent_to("POST", "/api/v3/order") == []


@pytest.mark.parametrize("client_order_id", ["", "has space", "x" * 33, "semi;colon"])
def test_client_order_id_format(client_order_id: str) -> None:
    with pytest.raises(ValidationError):
        OrderRequest(
            symbol=BTC,
            side="buy",
            amount=Decimal("0.0001"),
            price=Decimal("80000"),
            client_order_id=client_order_id,
        )


# --- Errors --------------------------------------------------------------------------


async def test_insufficient_balance_is_a_rejection(
    trading_adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    replay.routes["POST", "/api/v3/order"] = Reply(
        "mexc/error_insufficient_balance.json", status=400
    )

    with pytest.raises(OrderRejected):
        await trading_adapter.place_order(limit_buy())


async def test_bad_signature_is_an_auth_error(
    trading_adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    replay.routes["GET", "/api/v3/account"] = Reply("mexc/error_bad_signature.json", status=400)

    with pytest.raises(AuthError):
        await trading_adapter.fetch_balances()


@pytest.mark.parametrize(
    "reply",
    [Reply(error=TimeoutError()), Reply(body="Service Unavailable", status=503)],
    ids=["timeout", "http-503"],
)
async def test_network_failures_are_retryable(
    adapter: ExchangeAdapter, replay: ReplaySession, reply: Reply
) -> None:
    replay.routes["GET", "/api/v3/ticker/24hr"] = reply

    with pytest.raises(ExchangeNetworkError):
        await adapter.fetch_ticker(BTC)
    # Retried with backoff, then given up: 5 tries in all.
    assert len(replay.sent_to("GET", "/api/v3/ticker/24hr")) == 5


async def test_rejections_are_not_network_errors(
    trading_adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    # P1-4 retries ExchangeNetworkError only, so a rejection must never be one.
    replay.routes["POST", "/api/v3/order"] = Reply(
        "mexc/error_insufficient_balance.json", status=400
    )

    with pytest.raises(ExchangeError) as caught:
        await trading_adapter.place_order(limit_buy())
    assert not isinstance(caught.value, ExchangeNetworkError)


# --- Retries (P1-4): reads retry on network errors; orders never resend ---------------


@pytest.mark.parametrize(
    "failure",
    [
        Reply(error=TimeoutError()),
        Reply(body="Service Unavailable", status=503),
        Reply(body="Too Many Requests", status=429),  # rate limited
    ],
    ids=["timeout", "http-503", "http-429"],
)
async def test_reads_retry_network_errors(
    adapter: ExchangeAdapter, replay: ReplaySession, failure: Reply
) -> None:
    replay.routes["GET", "/api/v3/klines"] = [failure, Reply("mexc/klines_btcusdt_60m.json")]

    with capture_logs() as logs:
        candles = await adapter.fetch_candles(BTC, "1h")

    assert len(candles) == 3
    assert len(replay.sent_to("GET", "/api/v3/klines")) == 2
    (retry,) = [e for e in logs if e["event"] == "exchange.retrying"]
    assert retry["operation"] == "fetch_candles"
    assert retry["symbol"] == BTC


async def test_market_loading_retries(adapter: ExchangeAdapter, replay: ReplaySession) -> None:
    replay.routes["GET", "/api/v3/exchangeInfo"] = [
        Reply(error=TimeoutError()),
        Reply("mexc/exchange_info.json"),
    ]

    rules = await adapter.market_rules(BTC)

    assert rules.price_step == Decimal("0.01")
    assert len(replay.sent_to("GET", "/api/v3/exchangeInfo")) == 2


async def test_balances_retry(trading_adapter: ExchangeAdapter, replay: ReplaySession) -> None:
    replay.routes["GET", "/api/v3/account"] = [
        Reply(error=TimeoutError()),
        Reply("mexc/account.json"),
    ]

    balances = await trading_adapter.fetch_balances()

    assert set(balances) == {"BTC", "USDT"}
    assert len(replay.sent_to("GET", "/api/v3/account")) == 2


async def test_auth_errors_are_not_retried(
    trading_adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    replay.routes["GET", "/api/v3/account"] = Reply("mexc/error_bad_signature.json", status=400)

    with pytest.raises(AuthError):
        await trading_adapter.fetch_balances()
    assert len(replay.sent_to("GET", "/api/v3/account")) == 1


async def test_rejected_orders_are_not_retried(
    trading_adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    replay.routes["POST", "/api/v3/order"] = Reply(
        "mexc/error_insufficient_balance.json", status=400
    )

    with capture_logs() as logs, pytest.raises(OrderRejected):
        await trading_adapter.place_order(limit_buy())

    assert len(replay.sent_to("POST", "/api/v3/order")) == 1
    assert not [e for e in logs if e["event"].startswith("exchange.retry")]


@pytest.mark.parametrize(
    "failure",
    [Reply(error=TimeoutError()), Reply(body="Service Unavailable", status=503)],
    ids=["timeout", "http-503"],
)
async def test_orders_with_unknown_outcome_are_not_resent(
    trading_adapter: ExchangeAdapter, replay: ReplaySession, failure: Reply
) -> None:
    # The order may have reached the exchange. Resending could place it twice;
    # LiveBroker (P9) looks it up by client order ID instead (D18, D27).
    replay.routes["POST", "/api/v3/order"] = [failure, Reply("mexc/order_new.json")]

    with pytest.raises(ExchangeNetworkError):
        await trading_adapter.place_order(limit_buy())

    assert len(replay.sent_to("POST", "/api/v3/order")) == 1


async def test_cancels_are_not_retried(
    trading_adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    replay.routes["DELETE", "/api/v3/order"] = [
        Reply(error=TimeoutError()),
        Reply("mexc/order_cancel.json"),
    ]

    with pytest.raises(ExchangeNetworkError):
        await trading_adapter.cancel_order(BTC, "bot1-000001")

    assert len(replay.sent_to("DELETE", "/api/v3/order")) == 1


async def test_order_checks_retry_before_anything_is_sent(
    trading_adapter: ExchangeAdapter, replay: ReplaySession
) -> None:
    # Loading the market rules is a read, and no order has gone out yet: safe to retry.
    replay.routes["GET", "/api/v3/exchangeInfo"] = [
        Reply(error=TimeoutError()),
        Reply("mexc/exchange_info.json"),
    ]

    order = await trading_adapter.place_order(limit_buy())

    assert order.client_order_id == "bot1-000001"
    assert len(replay.sent_to("POST", "/api/v3/order")) == 1


# --- Safety --------------------------------------------------------------------------


def test_sandbox_fails_closed() -> None:
    # MEXC has no sandbox; asking for one must never quietly give us the live API.
    with pytest.raises(SandboxUnavailable):
        ExchangeAdapter(sandbox=True)


async def test_rate_limiting_is_on(replay: ReplaySession) -> None:
    # The fixtures switch it off for speed; the real adapter must never.
    async with ExchangeAdapter(session=replay) as exchange:
        assert exchange._exchange.enableRateLimit is True


def test_adapter_has_no_way_to_move_funds() -> None:
    forbidden = ("withdraw", "transfer", "deposit")
    public = [name for name in dir(ExchangeAdapter) if not name.startswith("_")]
    assert not [name for name in public if any(word in name.lower() for word in forbidden)]


# --- Binance spot testnet (D23: a P9 test step for LiveBroker, fake money) -----------


async def test_binance_testnet_loads_spot_markets_only() -> None:
    # Only the spot route exists: a futures request (or any other) fails the test.
    # CCXT refuses Binance's futures testnet outright, so loading every market type
    # would break sandbox mode, and we trade spot only anyway.
    replay = ReplaySession(
        routes={("GET", "/api/v3/exchangeInfo"): Reply("binance_testnet/exchange_info.json")}
    )
    async with without_waits(ExchangeAdapter("binance", sandbox=True, session=replay)) as exchange:
        rules = await exchange.market_rules(BTC)

    # Sandbox mode really went to the testnet, not the live API.
    assert [(r.host, r.path) for r in replay.sent] == [
        ("testnet.binance.vision", "/api/v3/exchangeInfo")
    ]
    # Binance's own rules, not MEXC's: coarser amount step, 5 USDT minimum.
    assert rules.amount_step == Decimal("0.00001")
    assert rules.price_step == Decimal("0.01")
    assert rules.min_amount == Decimal("0.00001")
    assert rules.min_cost == Decimal("5")
