"""SQLAlchemy table definitions, shared so the engine writes and the API reads the same schema.

These describe the tables; they don't create them. Alembic migrations (migrations/) do
that, and a test checks the two agree.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Must match engine.exchange.models.Timeframe (a test checks this).
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
# Must match engine.brokers.base.Mode and OrderStatus (a test checks these).
TRADING_MODES = ("paper", "live")
ORDER_STATUSES = ("open", "closed", "canceled", "rejected")
BOT_STATUSES = ("running", "paused", "stopped")
# Must match engine.risk.limits.Decision (a test checks this).
RISK_DECISIONS = ("allowed", "blocked", "resized")
# What a strategy switch does with an open position. Must match
# shared.commands.SwitchPolicy (a test checks this).
SWITCH_POLICIES = ("keep", "close_first", "hand_over")
# The default sizing (engine.risk.sizing.AllIn): spend all the free cash, as before P4.
ALL_IN_SIZING = '{"mode": "all_in"}'
# The exchanges an account's keys can be for (CCXT ids): MEXC, and Binance's testnet (D23).
EXCHANGES = ("mexc", "binance")


class Base(DeclarativeBase):
    # Predictable constraint names, so later migrations can refer to them by name.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class CandleRow(Base):
    """One closed OHLCV candle. A TimescaleDB hypertable, partitioned by `ts`."""

    __tablename__ = "candles"
    __table_args__ = (CheckConstraint(f"timeframe IN {TIMEFRAMES!r}", name="timeframe"),)

    # The primary key is also the index every candle query uses: one series, ordered by time.
    exchange: Mapped[str] = mapped_column(Text, primary_key=True)  # CCXT id, e.g. "mexc"
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)  # CCXT format, e.g. "BTC/USDT"
    timeframe: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)  # open time
    # NUMERIC without a fixed precision: exact, and fits any coin's price or volume.
    open: Mapped[Decimal] = mapped_column(Numeric)
    high: Mapped[Decimal] = mapped_column(Numeric)
    low: Mapped[Decimal] = mapped_column(Numeric)
    close: Mapped[Decimal] = mapped_column(Numeric)
    volume: Mapped[Decimal] = mapped_column(Numeric)


class BacktestRunRow(Base):
    """One backtest: everything it was run with, and what came out.

    The inputs plus the candles (checked by `candles_sha256`) reproduce the result exactly.
    """

    __tablename__ = "backtest_runs"
    __table_args__ = (CheckConstraint(f"timeframe IN {TIMEFRAMES!r}", name="timeframe"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Inputs
    strategy: Mapped[str] = mapped_column(Text)
    strategy_version: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    exchange: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    timeframe: Mapped[str] = mapped_column(Text)
    range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # first candle traded
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # exclusive
    warmup_candles: Mapped[int] = mapped_column(Integer)  # loaded from before range_start
    initial_cash: Mapped[Decimal] = mapped_column(Numeric)
    fee_rate: Mapped[Decimal] = mapped_column(Numeric)
    slippage_rate: Mapped[Decimal] = mapped_column(Numeric)
    market_rules: Mapped[dict[str, Any]] = mapped_column(JSONB)  # step sizes and minimums
    # How each buy was sized (engine/risk/sizing.py); runs before P4-1 were all-in.
    sizing: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=ALL_IN_SIZING)
    # Its stops (engine/risk/stops.py); {} is none, as for every run before P4-2.
    stops: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    candle_count: Mapped[int] = mapped_column(Integer)  # warm-up included
    candles_sha256: Mapped[str] = mapped_column(Text)  # proves a re-run uses the same candles
    backtester_version: Mapped[int] = mapped_column(Integer)
    # Results. Money in the JSON is strings, so it stays exact.
    final_equity: Mapped[Decimal] = mapped_column(Numeric)
    total_fees: Mapped[Decimal] = mapped_column(Numeric)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)  # signals, fills, equity curve


# --- Bots (P3) --------------------------------------------------------------------------


class BotRow(Base):
    """One strategy config running on one symbol and timeframe, in paper or live mode.

    The bot runner runs the bots whose status is "running" or "paused" when the engine
    starts. A paused bot sells and keeps its stops, but its buys are blocked (D42). The
    kill switch sets every one of them "stopped" (D43).
    """

    __tablename__ = "bots"
    __table_args__ = (
        CheckConstraint(f"timeframe IN {TIMEFRAMES!r}", name="timeframe"),
        CheckConstraint(f"mode IN {TRADING_MODES!r}", name="mode"),
        CheckConstraint(f"status IN {BOT_STATUSES!r}", name="status"),
        CheckConstraint(f"switch_policy IN {SWITCH_POLICIES!r}", name="switch_policy"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    name: Mapped[str] = mapped_column(Text, unique=True)
    # The CCXT id, e.g. "mexc". P5 replaces it with an exchange account (and its keys).
    exchange: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    timeframe: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text, server_default="paper")  # D17
    status: Mapped[str] = mapped_column(Text, server_default="stopped")
    # The config it runs now. use_alter: the two tables point at each other, so this
    # foreign key is added after both exist.
    active_strategy_config_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("strategy_configs.id", use_alter=True)
    )
    # What a strategy switch does with an open position (shared.commands.SwitchPolicy, D51).
    switch_policy: Mapped[str] = mapped_column(Text, server_default="keep")
    # How much each buy spends (engine/risk/sizing.py, D39), validated when loaded.
    sizing: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=ALL_IN_SIZING)
    # When a position is sold whatever the strategy says (engine/risk/stops.py, D40); {} is none.
    stops: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    # Why and when it was paused (the error breaker, D42); NULL unless paused.
    paused_reason: Mapped[str | None] = mapped_column(Text)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why and when the kill switch stopped it (D43); NULL unless it did.
    stopped_reason: Mapped[str | None] = mapped_column(Text)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyConfigRow(Base):
    """A strategy with specific params, for one bot. Append-only: a change adds a row."""

    __tablename__ = "strategy_configs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("bots.id"), index=True)
    strategy: Mapped[str] = mapped_column(Text)  # a name in engine.strategies.registry
    version: Mapped[int] = mapped_column(Integer)  # the strategy's version when this was made
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Who made it: "cli", or a command's source ("dashboard"); "regime" from P6-4.
    created_by: Mapped[str] = mapped_column(Text)


# --- Trading (P3): written by the brokers ---------------------------------------------


class OrderRow(Base):
    """One order, paper or live.

    `client_order_id` is ours and unique, so a retry can't duplicate an order (D18).
    """

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(f"mode IN {TRADING_MODES!r}", name="mode"),
        CheckConstraint("side IN ('buy', 'sell')", name="side"),
        CheckConstraint("type IN ('limit')", name="type"),
        CheckConstraint(f"status IN {ORDER_STATUSES!r}", name="status"),
        CheckConstraint("price > 0 AND amount > 0", name="positive"),
        CheckConstraint("filled >= 0 AND filled <= amount", name="filled"),
        # The open orders of a symbol: what every price update checks in paper mode.
        Index("ix_orders_open", "exchange", "symbol", postgresql_where=text("status = 'open'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    bot_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("bots.id"))
    mode: Mapped[str] = mapped_column(Text)
    exchange: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    client_order_id: Mapped[str] = mapped_column(Text, unique=True)
    exchange_order_id: Mapped[str | None] = mapped_column(Text)  # None for paper orders
    side: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric)  # the limit
    amount: Mapped[Decimal] = mapped_column(Numeric)  # in the base currency
    filled: Mapped[Decimal] = mapped_column(Numeric)  # the sum of its fills
    status: Mapped[str] = mapped_column(Text)
    reject_reason: Mapped[str | None] = mapped_column(Text)  # why, when status is 'rejected'
    reason: Mapped[dict[str, Any]] = mapped_column(JSONB)  # the signal and risk check behind it


class FillRow(Base):
    """One fill of an order. A partly filled order has several."""

    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    price: Mapped[Decimal] = mapped_column(Numeric)
    amount: Mapped[Decimal] = mapped_column(Numeric)  # in the base currency
    fee: Mapped[Decimal] = mapped_column(Numeric)
    fee_currency: Mapped[str] = mapped_column(Text)


class PositionRow(Base):
    """A bot's holding in one symbol, from its first buy until it's sold off (`closed_at`)."""

    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint(f"mode IN {TRADING_MODES!r}", name="mode"),
        CheckConstraint("side IN ('long')", name="side"),  # spot only
        CheckConstraint("amount >= 0", name="amount"),
        # At most one open position per bot and symbol.
        Index(
            "uq_positions_open",
            "bot_id",
            "symbol",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("bots.id"))
    mode: Mapped[str] = mapped_column(Text)
    exchange: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric)  # 0 once closed
    avg_entry: Mapped[Decimal] = mapped_column(Numeric)
    fees_paid: Mapped[Decimal] = mapped_column(Numeric)  # every fill's fee, in the quote currency
    # Its exit levels (engine/risk/stops.py, D40), set by the bot runner while it's open;
    # all NULL for a bot without stops. `stop` is the higher of the stop-loss and trailing.
    stop: Mapped[Decimal | None] = mapped_column(Numeric)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric)  # the fixed stop-loss level
    peak: Mapped[Decimal | None] = mapped_column(Numeric)  # highest bid since entry, to trail
    entry_atr: Mapped[Decimal | None] = mapped_column(Numeric)  # for ATR distances
    # The avg_entry the levels were placed from: when another fill moves it, they're
    # placed again. NULL until they're placed.
    levels_entry: Mapped[Decimal | None] = mapped_column(Numeric)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric)  # net of fees (engine.brokers.ledger)


class PaperBalanceRow(Base):
    """A paper bot's virtual balance in one currency."""

    __tablename__ = "paper_balances"
    __table_args__ = (CheckConstraint("free >= 0 AND used >= 0", name="not_negative"),)

    bot_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("bots.id"), primary_key=True)
    currency: Mapped[str] = mapped_column(Text, primary_key=True)
    free: Mapped[Decimal] = mapped_column(Numeric)
    used: Mapped[Decimal] = mapped_column(Numeric)  # held by open orders


# --- Risk (P4-3): the limits, and what they blocked -----------------------------------


class RiskSettingsRow(Base):
    """The account's limits in one mode (engine/risk/limits.py, D41). NULL turns one off.

    Read on every buy, so a change applies to the next one without a restart.
    """

    __tablename__ = "risk_settings"
    __table_args__ = (
        CheckConstraint(f"mode IN {TRADING_MODES!r}", name="mode"),
        CheckConstraint(
            "max_open_positions >= 1"
            " AND max_coin_exposure_pct > 0 AND max_coin_exposure_pct <= 100"
            " AND max_daily_loss_pct > 0 AND max_daily_loss_pct <= 100"
            " AND max_drawdown_pct > 0 AND max_drawdown_pct <= 100",
            name="limits",
        ),
        CheckConstraint(
            "stale_after_candles >= 1"
            " AND max_failed_orders >= 1 AND failed_orders_minutes >= 1"
            " AND (max_failed_orders IS NULL) = (failed_orders_minutes IS NULL)"
            " AND spike_pct > 0 AND spike_minutes >= 1 AND spike_minutes <= 60"
            " AND (spike_pct IS NULL) = (spike_minutes IS NULL)",
            name="breakers",
        ),
    )

    mode: Mapped[str] = mapped_column(Text, primary_key=True)
    max_open_positions: Mapped[int | None] = mapped_column(Integer)
    max_coin_exposure_pct: Mapped[Decimal | None] = mapped_column(Numeric)  # of equity
    max_daily_loss_pct: Mapped[Decimal | None] = mapped_column(Numeric)  # of midnight's equity
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric)  # below the peak
    # The circuit breakers (engine/risk/breakers.py, D42).
    stale_after_candles: Mapped[int | None] = mapped_column(Integer)  # of the buying bot's
    max_failed_orders: Mapped[int | None] = mapped_column(Integer)  # then the bot is paused
    failed_orders_minutes: Mapped[int | None] = mapped_column(Integer)  # within this long
    spike_pct: Mapped[Decimal | None] = mapped_column(Numeric)  # a swing this big stops buys
    spike_minutes: Mapped[int | None] = mapped_column(Integer)  # within this long (60 at most)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RiskEventRow(Base):
    """An order a limit blocked or shrank, with what it was decided on."""

    __tablename__ = "risk_events"
    __table_args__ = (
        CheckConstraint(f"mode IN {TRADING_MODES!r}", name="mode"),
        CheckConstraint(f"decision IN {RISK_DECISIONS!r}", name="decision"),
        # Whether the daily loss limit already blocked a buy today (engine/risk/account.py).
        Index("ix_risk_events_mode_rule_ts", "mode", "rule", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mode: Mapped[str] = mapped_column(Text)
    bot_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("bots.id"))
    client_order_id: Mapped[str] = mapped_column(Text)  # the order's, placed or not
    rule: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)  # the order, settings and account


class UserRow(Base):
    """Who can sign in to the dashboard: one user, with a password and TOTP (§9, D44).

    Made by the CLI (backend/users.py), never by the API.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("failed_logins >= 0 AND session_version >= 1", name="counters"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)  # argon2id, with its own salt
    totp_secret_enc: Mapped[str] = mapped_column(Text)  # Fernet-encrypted (D12), never plain
    # The 30 s step of the last code accepted, so the same code can't be used twice.
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)
    # In every session's JWT; bumping it (logout) ends every session.
    session_version: Mapped[int] = mapped_column(Integer, server_default="1")
    failed_logins: Mapped[int] = mapped_column(Integer, server_default="0")  # in a row
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExchangeAccountRow(Base):
    """An exchange's API key and secret, stored from the Settings page (P5-7, D50).

    Both are Fernet-encrypted (D12) and never sent back to the browser; `key_hint` is
    what the page shows instead. Nothing trades with them until P9.
    """

    __tablename__ = "exchange_accounts"
    __table_args__ = (
        CheckConstraint(f"exchange IN {EXCHANGES!r}", name="exchange"),
        # MEXC has no sandbox (D22); Binance's is its spot testnet (D23).
        CheckConstraint("NOT (exchange = 'mexc' AND sandbox)", name="sandbox"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    exchange: Mapped[str] = mapped_column(Text)  # the CCXT id
    label: Mapped[str] = mapped_column(Text, unique=True)
    api_key_enc: Mapped[str] = mapped_column(Text)  # Fernet-encrypted, never plain
    api_secret_enc: Mapped[str] = mapped_column(Text)  # Fernet-encrypted, never plain
    key_hint: Mapped[str] = mapped_column(Text)  # the API key's last 4 characters
    sandbox: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
