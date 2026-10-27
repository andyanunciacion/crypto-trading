"""Reading a stored backtest result and reporting on it, without the engine (D48).

The formulas themselves are checked against hand calculations in
engine/tests/test_backtest_report.py, through the engine's report().
"""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from shared.backtest import StoredResult, metrics

D = Decimal

# One round trip from 1000 USDT, fee 0.1%, as backtest_runs.result stores it (money as
# strings): buy 9 @ 100 (cost 900.90 with the fee), sold by a stop-loss 9 @ 120
# (1078.92 after the fee): +178.02.
STORED: dict[str, Any] = {
    "signals": [{"ts": "2026-01-01T00:00:00Z", "action": "buy", "reason": "fast crossed above"}],
    "fills": [
        {
            "ts": "2026-01-01T01:00:00Z",
            "side": "buy",
            "price": "100",
            "amount": "9",
            "fee": "0.9",
            "cash_after": "99.1",
            "exit": None,
        },
        {
            "ts": "2026-01-01T02:00:00Z",
            "side": "sell",
            "price": "120",
            "amount": "9",
            "fee": "1.08",
            "cash_after": "1178.02",
            "exit": "stop_loss",
        },
    ],
    "skipped": [{"signal_ts": "2026-01-01T02:00:00Z", "side": "buy", "reason": "below min_cost"}],
    "equity_curve": [
        {"ts": "2026-01-01T00:00:00Z", "equity": "1000"},
        {"ts": "2026-01-01T01:00:00Z", "equity": "1089.1"},
        {"ts": "2026-01-01T02:00:00Z", "equity": "1178.02"},
    ],
    "final_cash": "1178.02",
    "final_amount": "0",
    "final_equity": "1178.02",
    "total_fees": "1.98",
}


def test_a_stored_result_reads_back_and_reports() -> None:
    r = metrics(D(1000), "1h", StoredResult.model_validate(STORED))

    assert r.total_return == D("0.17802")
    assert r.max_drawdown == 0
    assert r.trade_count == 1
    assert r.trades[0].pnl == D("178.02")
    assert r.trades[0].exit == "stop_loss"
    assert r.win_rate == 1
    assert r.profit_factor is None  # no losing trade
    assert r.open_trade is False
    assert r.total_fees == D("1.98")


def test_a_fill_from_before_stops_has_no_exit() -> None:
    fills = [{k: v for k, v in f.items() if k != "exit"} for f in STORED["fills"]]

    stored = StoredResult.model_validate(STORED | {"fills": fills})

    assert [f.exit for f in stored.fills] == [None, None]


def test_an_unknown_exit_kind_is_refused() -> None:
    fills = [STORED["fills"][0], STORED["fills"][1] | {"exit": "panic"}]

    with pytest.raises(ValidationError):
        StoredResult.model_validate(STORED | {"fills": fills})
