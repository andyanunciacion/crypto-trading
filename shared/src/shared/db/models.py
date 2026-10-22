"""SQLAlchemy table definitions, shared so the engine writes and the API reads the same schema.

These describe the tables; they don't create them. Alembic migrations (migrations/) do
that, and a test checks the two agree.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, MetaData, Numeric, Text
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
