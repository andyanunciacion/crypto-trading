"""The backtest commands' shared arguments: --sizing and --stops are checked as they're read."""

import argparse
from decimal import Decimal

import pytest

from engine.backtest.cli import add_run_arguments
from engine.risk.sizing import ALL_IN, PercentOfEquity
from engine.risk.stops import NO_STOPS, AtrMultiple, Percent, RMultiple, Stops


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_run_arguments(parser)
    return parser.parse_args(["BTC/USDT", "1h", *argv])


def test_no_sizing_or_stops_means_all_in_without_stops() -> None:
    args = parse()
    assert (args.sizing, args.stops) == (ALL_IN, NO_STOPS)


def test_sizing_and_stops_are_read_from_json() -> None:
    args = parse(
        "--sizing",
        '{"mode": "percent_of_equity", "percent": 25}',
        "--stops",
        '{"stop_loss": {"kind": "atr", "multiple": 2}, "take_profit": {"kind": "r", "multiple": 2},'
        ' "trailing": {"kind": "percent", "percent": 3}}',
    )

    assert args.sizing == PercentOfEquity(percent=Decimal(25))
    assert args.stops == Stops(
        stop_loss=AtrMultiple(multiple=Decimal(2)),
        take_profit=RMultiple(multiple=Decimal(2)),
        trailing=Percent(percent=Decimal(3)),
    )


@pytest.mark.parametrize(
    "argv",
    [
        ("--stops", '{"take_profit": {"kind": "r", "multiple": 2}}'),  # R with no stop-loss
        ("--stops", '{"stop_loss": {"kind": "percent", "percent": 150}}'),
        ("--stops", "[]"),
        ("--sizing", '{"mode": "fixed"}'),
    ],
)
def test_bad_json_settings_stop_the_command(
    argv: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        parse(*argv)
    error = capsys.readouterr().err
    assert f"argument {argv[0]}:" in error
    assert "bad " in error or "must be a JSON object" in error
