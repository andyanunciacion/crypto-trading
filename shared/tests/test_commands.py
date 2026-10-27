"""The command list's messages (shared/commands.py): one parser for every command, and
the Bots page's commands expiring when their sender stops waiting (D47)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from shared.commands import COMMAND, BotCommand, CreateBotCommand, KillCommand

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def test_each_command_parses_as_its_own_type() -> None:
    kill = KillCommand(source="cli", sent_at=NOW)
    create = CreateBotCommand(
        name="btc-ema",
        strategy="ema_crossover",
        symbol="BTC/USDT",
        timeframe="1h",
        cash=Decimal("1000"),
        source="dashboard",
        sent_at=NOW,
        expires_at=NOW + timedelta(seconds=10),
    )
    stop = BotCommand(
        action="stop",
        bot_id=3,
        close_position=True,
        source="dashboard",
        sent_at=NOW,
        expires_at=NOW + timedelta(seconds=10),
    )

    for command in (kill, create, stop):
        assert COMMAND.validate_json(command.model_dump_json()) == command
    assert create.sizing == {"mode": "all_in"} and create.stops == {}


def test_a_bot_command_expires_when_its_sender_stops_waiting() -> None:
    command = BotCommand(
        action="start",
        bot_id=1,
        source="dashboard",
        sent_at=NOW,
        expires_at=NOW + timedelta(seconds=10),
    )

    assert not command.expired(NOW + timedelta(seconds=9))
    assert command.expired(NOW + timedelta(seconds=10))


def test_a_bot_command_without_an_expiry_or_with_cash_of_zero_is_refused() -> None:
    with pytest.raises(ValidationError):
        COMMAND.validate_python(
            {"type": "bot", "action": "start", "bot_id": 1, "source": "x", "sent_at": NOW}
        )
    with pytest.raises(ValidationError):
        CreateBotCommand(
            name="x",
            strategy="ema_crossover",
            symbol="BTC/USDT",
            timeframe="1h",
            cash=Decimal("0"),
            source="dashboard",
            sent_at=NOW,
            expires_at=NOW,
        )
