"""The live-price payload in Redis: exact money as strings, UTC times, stable names."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from shared.prices import PriceUpdate, price_channel, price_key

T0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def update(**overrides: object) -> PriceUpdate:
    fields: dict[str, object] = {
        "exchange": "mexc",
        "symbol": "BTC/USDT",
        "bid": Decimal("81819.48"),
        "ask": Decimal("81819.49"),
        "ts": T0,
        "received_at": T0,
        "source": "ws",
    }
    return PriceUpdate.model_validate(fields | overrides)


def test_money_is_sent_as_exact_strings() -> None:
    payload = json.loads(update(bid=Decimal("12345678.123456789012")).model_dump_json())

    # A JSON number would be read back as a float by most clients (JavaScript included).
    assert payload["bid"] == "12345678.123456789012"
    assert payload["ask"] == "81819.49"
    assert payload["last"] is None
    assert payload["ts"] == "2026-10-22T12:00:00Z"


def test_round_trips_through_json() -> None:
    original = update(last=Decimal("81820.01"), source="rest")

    assert PriceUpdate.model_validate_json(original.model_dump_json()) == original


@pytest.mark.parametrize(
    "bad",
    [
        {"bid": Decimal("0")},
        {"ask": Decimal("NaN")},
        {"ts": datetime(2026, 10, 22, 12)},  # noqa: DTZ001 (naive on purpose)
        {"source": "carrier-pigeon"},
    ],
    ids=["zero-price", "nan", "naive-time", "unknown-source"],
)
def test_invalid_updates_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        update(**bad)


def test_names() -> None:
    assert price_key("mexc", "BTC/USDT") == "price:mexc:BTC/USDT"
    assert price_channel("mexc", "BTC/USDT") == "prices:mexc:BTC/USDT"
