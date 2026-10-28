"""Stay flat (P6-4): sells what it holds, never buys."""

from datetime import UTC, datetime
from decimal import Decimal

from engine.exchange.models import Candle
from engine.strategies.base import Position
from engine.strategies.registry import build

CANDLE = Candle(
    ts=datetime(2026, 1, 1, tzinfo=UTC),
    open=Decimal(100),
    high=Decimal(101),
    low=Decimal(99),
    close=Decimal(100),
    volume=Decimal(1),
)


def test_sells_while_holding_and_holds_while_flat() -> None:
    flat = build("stay_flat", 1, {})

    assert flat.warmup_candles == 1
    held = flat.on_candle([CANDLE], Position(amount=Decimal(1), avg_entry=Decimal(90)))
    assert held.action == "sell"
    assert flat.on_candle([CANDLE], None).action == "hold"
