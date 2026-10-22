"""The adapter's own exceptions, so no code outside it ever needs CCXT's.

Retry rule (P1-4): only `ExchangeNetworkError` may be retried. Everything else means
the exchange understood the request and said no, so retrying can't help.
"""


class ExchangeError(Exception):
    """Base class. Also raised when a response doesn't look like what we expect."""


class ExchangeNetworkError(ExchangeError):
    """Timeout, connection failure, rate limit or maintenance: safe to retry.

    From `place_order` it means the outcome is unknown: the order may have reached
    the exchange. Retry with the same client order ID, never a new one (D18).
    """


class AuthError(ExchangeError):
    """Key rejected, expired, missing a permission, or used from a non-whitelisted IP."""


class OrderRejected(ExchangeError):
    """The exchange (or our pre-send checks) refused the order: never retry it."""


class OrderNotFound(OrderRejected):
    """The order to cancel doesn't exist, or is no longer open."""


class TradingDisabled(ExchangeError):
    """Order calls on an adapter that wasn't built with `trading_enabled=True`."""


class SandboxUnavailable(ExchangeError):
    """`sandbox=True` for an exchange that has no sandbox (MEXC has none)."""
