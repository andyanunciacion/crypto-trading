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
    Identity,
    Integer,
    MetaData,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Must match engine.exchange.models.Timeframe (a test checks this).
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")


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
    candle_count: Mapped[int] = mapped_column(Integer)  # warm-up included
    candles_sha256: Mapped[str] = mapped_column(Text)  # proves a re-run uses the same candles
    backtester_version: Mapped[int] = mapped_column(Integer)
    # Results. Money in the JSON is strings, so it stays exact.
    final_equity: Mapped[Decimal] = mapped_column(Numeric)
    total_fees: Mapped[Decimal] = mapped_column(Numeric)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)  # signals, fills, equity curve
