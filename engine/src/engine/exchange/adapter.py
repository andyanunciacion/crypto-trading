"""The only gateway to the exchange: everything that talks to MEXC goes through here.

Wraps CCXT's async client; this package is the only place that imports ccxt. On the
way out, CCXT's dicts become the validated models in models.py (Decimal money, UTC
times) and CCXT's exceptions become the ones in errors.py.

The adapter doesn't decide whether to trade; the RiskManager does that. It refuses
orders only when trading is switched off, or when the exchange would refuse them anyway.
"""

import contextlib
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

import ccxt.async_support as ccxt
import structlog
from pydantic import BaseModel, SecretStr, ValidationError

from engine.exchange import errors
from engine.exchange.models import (
    Balance,
    Candle,
    MarketRules,
    Order,
    OrderRequest,
    Ticker,
    Timeframe,
)

log = structlog.get_logger()

# CCXT logs every request's headers, API key included, at DEBUG. Keep its logger above
# that, whatever level the rest of the app logs at.
logging.getLogger("ccxt").setLevel(logging.INFO)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Keys in CCXT's balance dict that aren't currencies.
_BALANCE_META = frozenset({"info", "free", "used", "total", "timestamp", "datetime"})


class ExchangeAdapter:
    """Async access to one exchange account. Use `async with`, so the HTTP session closes.

    `trading_enabled=False` (the default) makes `place_order`/`cancel_order` refuse.
    Only LiveBroker (P9) turns it on, and only when `MODE=live` (D17, D22).
    `session` lets tests replace the network with recorded replies.
    """

    def __init__(
        self,
        exchange_id: str = "mexc",
        *,
        api_key: SecretStr | None = None,
        api_secret: SecretStr | None = None,
        sandbox: bool = False,
        trading_enabled: bool = False,
        session: object | None = None,
    ) -> None:
        if exchange_id not in ccxt.exchanges:
            raise ValueError(f"unknown exchange id: {exchange_id!r}")
        config: dict[str, object] = {
            "enableRateLimit": True,
            # load_markets() would also read the currency list from a wallet endpoint.
            # A trade-only key shouldn't have wallet permissions, and we don't need the
            # list: order precision comes from the markets.
            "has": {"fetchCurrencies": False},
            # Spot only. Some exchanges also load futures/options markets by default,
            # and CCXT refuses Binance's futures testnet in sandbox mode (D23).
            # MEXC ignores this option.
            "options": {"fetchMarkets": {"types": ["spot"]}},
        }
        if api_key is not None:
            config["apiKey"] = api_key.get_secret_value()
        if api_secret is not None:
            config["secret"] = api_secret.get_secret_value()
        if session is not None:
            # CCXT uses this instead of opening its own connection pool.
            config["session"] = session
        self._exchange: Any = getattr(ccxt, exchange_id)(config)
        # Numbers come back as strings, so they reach Decimal without passing through float.
        self._exchange.number = str
        self._trading_enabled = trading_enabled

        if self._exchange.precisionMode != ccxt.TICK_SIZE:
            # market_rules() reads precision as step sizes; other modes mean digit counts.
            raise ValueError(f"{exchange_id}: only tick-size precision is supported")
        if sandbox:
            # Fail closed: never fall back to the live API without saying so. Checked
            # here because CCXT's own check crashes when the URL is listed but empty (mexc).
            if not self._exchange.urls.get("test"):
                raise errors.SandboxUnavailable(
                    f"{exchange_id} has no sandbox; test with paper mode instead"
                )
            self._exchange.set_sandbox_mode(True)

    async def close(self) -> None:
        await self._exchange.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # --- Market data -----------------------------------------------------------------

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        """Candles as the exchange sends them, oldest first.

        The newest one may still be open (not closed yet); callers decide what to do with it.
        """
        since_ms = None if since is None else _to_ms(since)
        with _translated_errors():
            rows = await self._exchange.fetch_ohlcv(symbol, timeframe, since_ms, limit)
        return [
            _parse(
                Candle,
                {
                    "ts": _from_ms(row[0]),
                    "open": row[1],
                    "high": row[2],
                    "low": row[3],
                    "close": row[4],
                    "volume": row[5],
                },
            )
            for row in rows
        ]

    async def fetch_ticker(self, symbol: str) -> Ticker:
        with _translated_errors():
            raw = await self._exchange.fetch_ticker(symbol)
        return _parse(
            Ticker,
            {
                "symbol": raw.get("symbol"),
                "ts": _from_ms(raw.get("timestamp")),
                "bid": raw.get("bid"),
                "ask": raw.get("ask"),
                "last": raw.get("last"),
            },
        )

    async def market_rules(self, symbol: str) -> MarketRules:
        """Step sizes and minimums for `symbol`. Markets load once, on first use."""
        with _translated_errors():
            await self._exchange.load_markets()
            market = self._exchange.market(symbol)
        return _parse(
            MarketRules,
            {
                "symbol": market["symbol"],
                "amount_step": market["precision"]["amount"],
                "price_step": market["precision"]["price"],
                "min_amount": market["limits"]["amount"]["min"],
                "min_cost": market["limits"]["cost"]["min"],
            },
        )

    # --- Account ---------------------------------------------------------------------

    async def fetch_balances(self) -> dict[str, Balance]:
        """Non-zero balances by currency code. Needs an API key."""
        with _translated_errors():
            raw = await self._exchange.fetch_balance()
        balances: dict[str, Balance] = {}
        for currency, entry in raw.items():
            if currency in _BALANCE_META or not isinstance(entry, dict):
                continue
            balance = _parse(
                Balance,
                {
                    "currency": currency,
                    "free": entry.get("free"),
                    "used": entry.get("used"),
                    "total": entry.get("total"),
                },
            )
            if balance.total > 0:
                balances[currency] = balance
        return balances

    # --- Orders: only LiveBroker (P9) calls these, after the RiskManager -------------

    async def place_order(self, request: OrderRequest) -> Order:
        """Place a limit order, exactly as requested: it's never rounded or resized.

        Raises `OrderRejected` before sending if it doesn't fit the market's step sizes
        and minimums, and `ExchangeNetworkError` if the outcome is unknown.
        """
        self._require_trading("place_order")
        fields = request.model_dump(mode="json")
        _refuse_if_off_market(request, await self.market_rules(request.symbol))
        log.info("exchange.order.placing", **fields)
        try:
            with _translated_errors():
                raw = await self._exchange.create_order(
                    request.symbol,
                    request.type,
                    request.side,
                    _plain(request.amount),
                    _plain(request.price),
                    {"clientOrderId": request.client_order_id},
                )
        except errors.ExchangeError as exc:
            log.warning(
                "exchange.order.failed", error=type(exc).__name__, detail=str(exc), **fields
            )
            raise
        order = _parse_order(raw, request.client_order_id)
        log.info("exchange.order.placed", **order.model_dump(mode="json"))
        return order

    async def cancel_order(self, symbol: str, client_order_id: str) -> Order:
        self._require_trading("cancel_order")
        fields = {"symbol": symbol, "client_order_id": client_order_id}
        log.info("exchange.order.cancelling", **fields)
        try:
            with _translated_errors():
                raw = await self._exchange.cancel_order(
                    None, symbol, {"clientOrderId": client_order_id}
                )
        except errors.ExchangeError as exc:
            log.warning(
                "exchange.order.cancel_failed", error=type(exc).__name__, detail=str(exc), **fields
            )
            raise
        order = _parse_order(raw, client_order_id)
        log.info("exchange.order.cancelled", **order.model_dump(mode="json"))
        return order

    def _require_trading(self, action: str) -> None:
        if not self._trading_enabled:
            log.warning("exchange.trading_disabled", action=action)
            raise errors.TradingDisabled(f"{action} refused: trading is not enabled")


@contextlib.contextmanager
def _translated_errors() -> Iterator[None]:
    """Re-raise CCXT's exceptions as ours. Subclasses come before their parents."""
    try:
        yield
    except ccxt.NetworkError as exc:  # timeouts, rate limits, maintenance, clock skew
        raise errors.ExchangeNetworkError(str(exc)) from exc
    except ccxt.AuthenticationError as exc:  # incl. missing permission, IP not whitelisted
        raise errors.AuthError(str(exc)) from exc
    except ccxt.OrderNotFound as exc:
        raise errors.OrderNotFound(str(exc)) from exc
    except (ccxt.InvalidOrder, ccxt.InsufficientFunds) as exc:
        raise errors.OrderRejected(str(exc)) from exc
    except ccxt.BaseError as exc:
        raise errors.ExchangeError(str(exc)) from exc


def _refuse_if_off_market(request: OrderRequest, rules: MarketRules) -> None:
    problems = []
    if request.amount % rules.amount_step != 0:
        problems.append(f"amount {request.amount} is off the amount step {rules.amount_step}")
    if request.price % rules.price_step != 0:
        problems.append(f"price {request.price} is off the price step {rules.price_step}")
    if rules.min_amount is not None and request.amount < rules.min_amount:
        problems.append(f"amount {request.amount} is below the minimum amount {rules.min_amount}")
    cost = request.amount * request.price
    if rules.min_cost is not None and cost < rules.min_cost:
        problems.append(f"cost {cost} is below the minimum cost {rules.min_cost}")
    if problems:
        log.warning("exchange.order.refused", problems=problems, **request.model_dump(mode="json"))
        raise errors.OrderRejected("; ".join(problems))


def _parse_order(raw: Any, client_order_id: str) -> Order:
    timestamp = raw.get("timestamp")
    return _parse(
        Order,
        {
            "exchange_order_id": raw.get("id"),
            # MEXC doesn't echo it back in every reply. It's our own ID, so we know it.
            "client_order_id": client_order_id,
            "symbol": raw.get("symbol"),
            "side": raw.get("side"),
            "type": raw.get("type"),
            "price": raw.get("price"),
            "amount": raw.get("amount"),
            "filled": raw.get("filled"),
            "status": raw.get("status"),
            "ts": None if timestamp is None else _from_ms(timestamp),
        },
    )


def _parse[M: BaseModel](model: type[M], data: dict[str, object]) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise errors.ExchangeError(
            f"unexpected {model.__name__} data from the exchange: {exc}"
        ) from exc


def _to_ms(ts: datetime) -> int:
    if ts.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware (UTC)")
    return (ts - _EPOCH) // timedelta(milliseconds=1)


def _from_ms(ms: object) -> datetime:
    # Integer maths, so no float rounding of the milliseconds.
    if not isinstance(ms, int) or isinstance(ms, bool):
        raise errors.ExchangeError(f"unexpected timestamp from the exchange: {ms!r}")
    return _EPOCH + timedelta(milliseconds=ms)


def _plain(value: Decimal) -> str:
    # str(Decimal("0.0000001")) is "1E-7"; the exchange wants "0.0000001".
    return format(value, "f")
