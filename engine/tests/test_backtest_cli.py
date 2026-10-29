"""The backtest commands' shared arguments: --sizing, --stops and run's --auto-switch are
checked as they're read."""

import argparse
import json
import re
from decimal import Decimal

import pytest

from engine.backtest.cli import add_run_arguments, auto_switch_arg
from engine.risk.sizing import ALL_IN, PercentOfEquity
from engine.risk.stops import NO_STOPS, AtrMultiple, Percent, RMultiple, Stops
from shared.regime import MappedConfig


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


def auto_switch(**ranging: object) -> str:
    return json.dumps(
        {
            "mapping": {
                "trending_up": {"strategy": "ema_crossover", "params": {"fast": 9, "slow": 21}},
                "ranging": {"strategy": "rsi_mean_reversion", **ranging},
                "trending_down": {"strategy": "stay_flat"},
                "high_volatility": {"strategy": "stay_flat"},
            },
            "min_hold": 5,
            "policy": "close_first",
        }
    )


def test_auto_switch_is_stored_at_each_strategys_version_with_its_defaults() -> None:
    settings = auto_switch_arg(auto_switch())

    assert (settings.min_hold, settings.policy) == (5, "close_first")
    assert settings.mapping["ranging"] == MappedConfig(
        strategy="rsi_mean_reversion",
        version=1,
        params={"period": 14, "oversold": 30.0, "overbought": 70.0},
    )
    assert settings.mapping["trending_down"].version == 1


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (auto_switch(params={"period": 1}), "bad auto-switch: ranging: period:"),
        (auto_switch(strategy="nope"), "bad auto-switch: ranging: "),
        ('{"mapping": {}}', "bad auto-switch: "),
        ("[]", "must be a JSON object"),
    ],
    ids=["bad params", "unknown strategy", "unmapped", "not an object"],
)
def test_a_bad_auto_switch_is_refused_naming_the_regime(text: str, problem: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=re.escape(problem)):
        auto_switch_arg(text)
