"""SQLAlchemy table definitions, shared so the engine writes and the API reads the same schema.

These describe the tables; they don't create them. Alembic migrations (migrations/) do
that, and a test checks the two agree.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
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
# What a strategy switch (P6) does with an open position.
SWITCH_POLICIES = ("keep", "close_first", "hand_over")
# The default sizing (engine.risk.sizing.AllIn): spend all the free cash, as before P4.
ALL_IN_SIZING = '{"mode": "all_in"}'


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

    The bot runner runs the bots whose status is "running" when the engine starts.
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
    switch_policy: Mapped[str] = mapped_column(Text, server_default="keep")  # used from P6
    # How much each buy spends (engine/risk/sizing.py, D39), validated when loaded.
    sizing: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=ALL_IN_SIZING)
    # When a position is sold whatever the strategy says (engine/risk/stops.py, D40); {} is none.
    stops: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class StrategyConfigRow(Base):
    """A strategy with specific params, for one bot. Append-only: a change adds a row."""

    __tablename__ = "strategy_configs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("bots.id"), index=True)
    strategy: Mapped[str] = mapped_column(Text)  # a name in engine.strategies.registry
    version: Mapped[int] = mapped_column(Integer)  # the strategy's version when this was made
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str] = mapped_column(Text)  # e.g. "cli"; "regime" from P6


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
