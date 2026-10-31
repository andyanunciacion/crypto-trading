"""The Backtests page's API (backend/backtests/, D48): a run is queued as a job for the
backtester, and stored runs are read from the database with their metrics worked out by
shared.backtest.metrics, as the CLI's report does.

The backtester's side is tested in engine/tests/test_backtest_worker.py.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, text

from backend.testing import T0, Api
from shared.backtest import StoredResult, metrics
from shared.backtest_jobs import (
    BACKTEST_COMMANDS_KEY,
    BacktestJob,
    RunBacktestCommand,
    read_job,
    write_job,
)
from shared.db.models import BacktestRunRow, CandleRow
from shared.regime import AutoSwitchSettings

D = Decimal
HOUR = timedelta(hours=1)

REQUEST = {
    "strategy": "ema_crossover",
    "params": {"fast": 9, "slow": 21},
    "symbol": "BTC/USDT",
    "timeframe": "4h",
    "days": 60,
    "cash": "1000",
    "fee_rate": "0.001",
    "sizing": {"mode": "percent_of_equity", "percent": "50"},
    "stops": {"stop_loss": {"kind": "percent", "percent": "3"}},
}

# Two round trips from 1000 USDT, fee 0.1% (test_backtest_report.py's numbers): +178.02
# sold by a stop-loss, then -102.10 on a SELL signal.
RESULT: dict[str, Any] = {
    "signals": [{"ts": "2026-01-01T00:00:00Z"}, {"ts": "2026-01-01T02:00:00Z"}],
    "fills": [
        {"ts": "2026-01-01T01:00:00Z", "side": "buy", "price": "100", "amount": "9",
         "fee": "0.9", "cash_after": "99.1"},
        {"ts": "2026-01-01T02:00:00Z", "side": "sell", "price": "120", "amount": "9",
         "fee": "1.08", "cash_after": "1178.02", "exit": "stop_loss"},
        {"ts": "2026-01-01T03:00:00Z", "side": "buy", "price": "110", "amount": "10",
         "fee": "1.1", "cash_after": "76.92"},
        {"ts": "2026-01-01T04:00:00Z", "side": "sell", "price": "100", "amount": "10",
         "fee": "1", "cash_after": "1075.92"},
    ],
    "skipped": [{"signal_ts": "2026-01-01T04:00:00Z", "side": "buy", "reason": "below min_cost"}],
    "equity_curve": [
        {"ts": f"2026-01-01T0{h}:00:00Z", "equity": e}
        for h, e in enumerate(["1000", "1089.1", "1178.02", "1126.92", "1075.92", "1075.92"])
    ],
    "final_cash": "1075.92",
    "final_amount": "0",
    "final_equity": "1075.92",
    "total_fees": "4.08",
}  # fmt: skip


async def clean(api: Api) -> None:
    async with api.db.begin() as conn:
        await conn.execute(text("TRUNCATE backtest_runs, candles RESTART IDENTITY"))
    await api.redis.flushdb()


async def store_run(api: Api, result: dict[str, Any] = RESULT, **overrides: Any) -> int:
    values: dict[str, Any] = {
        "strategy": "ema_crossover",
        "strategy_version": 1,
        "params": {"fast": 9, "slow": 21},
        "exchange": "mexc",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "range_start": T0,
        "range_end": T0 + 6 * HOUR,
        "warmup_candles": 64,
        "initial_cash": D(1000),
        "fee_rate": D("0.001"),
        "slippage_rate": D("0.0005"),
        "market_rules": {"symbol": "BTC/USDT"},
        "sizing": {"mode": "all_in"},
        "stops": {"stop_loss": {"kind": "percent", "percent": "3"}},
        "candle_count": 70,
        "candles_sha256": "ab" * 32,
        "backtester_version": 1,
        "final_equity": D(result["final_equity"]),
        "total_fees": D(result["total_fees"]),
        "result": result,
    }
    async with api.db.begin() as conn:
        stmt = insert(BacktestRunRow).values(values | overrides).returning(BacktestRunRow.id)
        run_id: int = (await conn.execute(stmt)).scalar_one()
    return run_id


# --- Running one ------------------------------------------------------------------------


async def test_a_run_is_queued_for_the_backtester_as_a_job(api: Api) -> None:
    await clean(api)
    await api.login()

    response = await api.client.post("/backtests", json=REQUEST)

    assert response.status_code == 202
    job = BacktestJob.model_validate(response.json())
    assert (job.status, job.queued_at, job.run_id) == ("queued", T0, None)
    assert await read_job(api.redis, job.id) == job
    popped = await api.redis.rpop(BACKTEST_COMMANDS_KEY)
    assert popped is not None and not isinstance(popped, list)
    command = RunBacktestCommand.model_validate_json(popped)
    assert command.id == job.id
    assert (command.source, command.sent_at) == ("dashboard", T0)
    assert (command.strategy, command.symbol, command.timeframe, command.days) == (
        "ema_crossover",
        "BTC/USDT",
        "4h",
        60,
    )
    assert (command.cash, command.fee_rate, command.slippage) == (D(1000), D("0.001"), None)
    assert command.sizing == {"mode": "percent_of_equity", "percent": "50"}
    assert command.stops == {"stop_loss": {"kind": "percent", "percent": "3"}}


async def test_a_plain_run_queues_no_auto_switching(api: Api) -> None:
    await clean(api)
    await api.login()

    await api.client.post("/backtests", json=REQUEST)

    popped = await api.redis.rpop(BACKTEST_COMMANDS_KEY)
    assert popped is not None and not isinstance(popped, list)
    assert RunBacktestCommand.model_validate_json(popped).auto_switch is None


async def test_an_auto_switching_run_is_queued_with_its_mapping(api: Api) -> None:
    await clean(api)
    await api.login()
    # As the form sends it: no versions (the backtester fills them in), no detector.
    sent = {
        "mapping": {
            "trending_up": {"strategy": "ema_crossover", "params": {"fast": 9, "slow": 21}},
            "ranging": {"strategy": "rsi_mean_reversion", "params": {"period": 14}},
            "trending_down": {"strategy": "stay_flat", "params": {}},
            "high_volatility": {"strategy": "stay_flat", "params": {}},
        },
        "min_hold": 3,
        "policy": "keep",
    }

    response = await api.client.post("/backtests", json=REQUEST | {"auto_switch": sent})

    assert response.status_code == 202
    popped = await api.redis.rpop(BACKTEST_COMMANDS_KEY)
    assert popped is not None and not isinstance(popped, list)
    command = RunBacktestCommand.model_validate_json(popped)
    assert command.auto_switch == AutoSwitchSettings.model_validate(sent)
    assert command.auto_switch is not None
    assert command.auto_switch.mapping["ranging"].version is None  # the backtester's to fill


async def test_a_bad_request_is_a_422_and_queues_nothing(api: Api) -> None:
    await clean(api)
    await api.login()
    without_fee = {k: v for k, v in REQUEST.items() if k != "fee_rate"}
    unmapped = {"mapping": {"trending_up": {"strategy": "ema_crossover"}}}
    flat = {"strategy": "stay_flat"}
    every_regime = {r: flat for r in ("trending_up", "trending_down", "ranging", "high_volatility")}

    for body in (
        without_fee,
        REQUEST | {"days": 0},
        REQUEST | {"cash": "0"},
        REQUEST | {"slippage": "1"},
        REQUEST | {"withdraw": True},
        REQUEST | {"auto_switch": unmapped},
        REQUEST | {"auto_switch": {"mapping": every_regime, "min_hold": 0}},
        REQUEST | {"auto_switch": {"mapping": every_regime, "policy": "sell_everything"}},
    ):
        response = await api.client.post("/backtests", json=body)
        assert response.status_code == 422
        assert '"input"' not in response.text  # no 422 echoes what was sent (app.py)
    assert await api.redis.llen(BACKTEST_COMMANDS_KEY) == 0


async def test_the_job_reads_back_as_the_backtester_moves_it_on(api: Api) -> None:
    await clean(api)
    await api.login()
    job = BacktestJob(id=uuid4(), status="queued", queued_at=T0)
    await write_job(api.redis, job)
    assert (await api.client.get(f"/backtests/jobs/{job.id}")).json()["status"] == "queued"

    done = job.model_copy(update={"status": "done", "run_id": 7, "finished_at": T0 + HOUR})
    await write_job(api.redis, done)
    response = await api.client.get(f"/backtests/jobs/{job.id}")

    assert BacktestJob.model_validate(response.json()) == done


async def test_an_unknown_job_is_a_404(api: Api) -> None:
    await clean(api)
    await api.login()

    assert (await api.client.get(f"/backtests/jobs/{uuid4()}")).status_code == 404
    assert (await api.client.get("/backtests/jobs/not-a-uuid")).status_code == 422


# --- Stored runs ------------------------------------------------------------------------


async def test_the_list_is_newest_first_with_the_clis_metrics(api: Api) -> None:
    await clean(api)
    await api.login()
    older = await store_run(api)
    newer = await store_run(api, symbol="ETH/USDT")

    response = await api.client.get("/backtests")

    assert response.status_code == 200
    runs = response.json()
    assert [r["id"] for r in runs] == [newer, older]
    expected = metrics(D(1000), "1h", StoredResult.model_validate(RESULT))
    shown = runs[1]["metrics"]
    assert D(shown["total_return"]) == expected.total_return == D("0.07592")
    assert D(shown["max_drawdown"]) == expected.max_drawdown
    assert expected.sharpe is not None and D(shown["sharpe"]) == expected.sharpe
    assert shown["trade_count"] == 2
    assert D(shown["win_rate"]) == D("0.5")
    assert D(shown["profit_factor"]) == expected.profit_factor
    assert (shown["open_trade"], shown["final_equity"], shown["total_fees"]) == (
        False,
        "1075.92",
        "4.08",
    )
    assert (runs[0]["currency"], runs[1]["candles_sha256"]) == ("USDT", "ab" * 32)
    assert "trades" not in runs[0] and "equity" not in runs[0]


async def test_the_list_pages_back_with_before(api: Api) -> None:
    await clean(api)
    await api.login()
    ids = [await store_run(api) for _ in range(3)]

    page = await api.client.get("/backtests", params={"limit": 2})
    rest = await api.client.get("/backtests", params={"limit": 2, "before": page.json()[-1]["id"]})

    assert [r["id"] for r in page.json()] == [ids[2], ids[1]]
    assert [r["id"] for r in rest.json()] == [ids[0]]


async def test_a_run_shows_its_trades_equity_and_skipped_orders(api: Api) -> None:
    await clean(api)
    await api.login()
    run_id = await store_run(api)

    response = await api.client.get(f"/backtests/{run_id}")

    assert response.status_code == 200
    run = response.json()
    assert (run["id"], run["strategy"], run["params"]) == (
        run_id,
        "ema_crossover",
        {"fast": 9, "slow": 21},
    )
    assert run["stops"] == {"stop_loss": {"kind": "percent", "percent": "3"}}
    assert [(t["pnl"], t["exit"]) for t in run["trades"]] == [
        ("178.02", "stop_loss"),
        ("-102.1", None),
    ]
    # Each point's fall from the highest equity so far: 1178.02 at 02:00.
    assert [p["drawdown"] for p in run["equity"]] == [
        "0.000000",
        "0.000000",
        "0.000000",
        "0.043378",  # 51.10 / 1178.02
        "0.086671",  # 102.10 / 1178.02
        "0.086671",
    ]
    assert run["equity"][3] == {
        "ts": "2026-01-01T03:00:00Z",
        "equity": "1126.92",
        "drawdown": "0.043378",
    }
    assert run["skipped"] == [
        {"signal_ts": "2026-01-01T04:00:00Z", "side": "buy", "reason": "below min_cost"}
    ]
    assert (run["fills"], run["signals"], run["metrics"]["trade_count"]) == (4, 2, 2)
    # Stored before auto-switching runs existed: none, and no switches.
    assert (run["auto_switch"], run["switches"]) == (None, [])
    assert run["trades"][0]["decided_by"] is None


EMA = {"strategy": "ema_crossover", "version": 1, "params": {"fast": 9, "slow": 21}}
FLAT = {"strategy": "stay_flat", "version": 1, "params": {}}
AUTO_SWITCH = {
    "mapping": {
        "trending_up": EMA,
        "ranging": {"strategy": "rsi_mean_reversion", "version": 1, "params": {"period": 14}},
        "trending_down": FLAT,
        "high_volatility": FLAT,
    },
    "min_hold": 3,
    "policy": "close_first",
}


async def test_an_auto_switching_run_shows_its_mapping_switches_and_whose_trades(
    api: Api,
) -> None:
    await clean(api)
    await api.login()
    fills = [dict(f) for f in RESULT["fills"]]
    for buy in fills[0], fills[2]:
        buy["decided_by"] = EMA
    fills[1] = {**fills[1], "exit": "switch"}  # sold by a close_first switch to stay flat
    switch = {
        "ts": "2026-01-01T01:00:00Z",
        "regime": "trending_down",
        "from_config": EMA,
        "to_config": FLAT,
        "reason": "trending_down for 3 candles in a row (...): switch to stay_flat v1 {}",
    }
    run_id = await store_run(
        api,
        RESULT | {"fills": fills, "switches": [switch]},
        auto_switch=AUTO_SWITCH,
        regime_detector_version=1,
    )

    listed = (await api.client.get("/backtests")).json()[0]
    run = (await api.client.get(f"/backtests/{run_id}")).json()

    assert listed["auto_switch"] == AUTO_SWITCH
    assert run["auto_switch"] == AUTO_SWITCH
    assert run["switches"] == [switch]
    assert [(t["exit"], t["decided_by"]) for t in run["trades"]] == [
        ("switch", EMA),
        (None, EMA),
    ]


def baseline(label: str, config: dict[str, Any] | None, result: dict[str, Any]) -> dict[str, Any]:
    """A stored baseline (shared.backtest.BaselineResult): what its metrics need."""
    kept = ("fills", "equity_curve", "final_amount", "final_equity", "total_fees")
    return {"label": label, "config": config} | {k: result[k] for k in kept}


# Stay flat never buys: its equity is the starting cash throughout.
FLAT_RESULT = RESULT | {
    "fills": [],
    "equity_curve": [{"ts": p["ts"], "equity": "1000"} for p in RESULT["equity_curve"]],
    "final_equity": "1000",
    "total_fees": "0",
}


async def test_an_auto_switching_run_shows_its_baselines_metrics(api: Api) -> None:
    await clean(api)
    await api.login()
    stored = [
        baseline('ema_crossover v1 {"fast": 9, "slow": 21}', EMA, RESULT),
        baseline("stay_flat v1 {}", FLAT, FLAT_RESULT),
        baseline("buy and hold", None, RESULT),
    ]
    run_id = await store_run(
        api, auto_switch=AUTO_SWITCH, regime_detector_version=1, baselines=stored
    )

    run = (await api.client.get(f"/backtests/{run_id}")).json()

    assert [(b["label"], b["config"]) for b in run["baselines"]] == [
        ('ema_crossover v1 {"fast": 9, "slow": 21}', EMA),
        ("stay_flat v1 {}", FLAT),
        ("buy and hold", None),
    ]
    # Worked out when shown, with the function the run's own metrics use (D30).
    assert run["baselines"][0]["metrics"] == run["metrics"]
    flat = run["baselines"][1]["metrics"]
    assert (D(flat["total_return"]), flat["trade_count"], flat["win_rate"]) == (D(0), 0, None)
    # The list leaves them out, as it does the trades.
    assert "baselines" not in (await api.client.get("/backtests")).json()[0]


async def test_a_run_without_stored_baselines_shows_none(api: Api) -> None:
    await clean(api)
    await api.login()
    # A plain run, and an auto-switching one stored before baselines were (migration 0016).
    plain = await store_run(api)
    older = await store_run(api, auto_switch=AUTO_SWITCH, regime_detector_version=1)

    for run_id in plain, older:
        assert (await api.client.get(f"/backtests/{run_id}")).json()["baselines"] is None


async def test_an_unknown_run_is_a_404(api: Api) -> None:
    await clean(api)
    await api.login()

    assert (await api.client.get("/backtests/99")).status_code == 404


async def test_a_result_that_doesnt_read_is_shown_without_metrics(api: Api) -> None:
    await clean(api)
    await api.login()
    unreadable = RESULT | {"fills": [{"side": "short"}]}  # no such side
    run_id = await store_run(api, unreadable)

    listed = (await api.client.get("/backtests")).json()[0]
    shown = (await api.client.get(f"/backtests/{run_id}")).json()

    assert listed["metrics"] is None and "can't be read" in listed["problem"]
    assert shown["metrics"] is None and shown["trades"] == [] and shown["equity"] == []


# --- What's stored ----------------------------------------------------------------------


async def test_the_candle_range_says_whats_stored_of_a_series(api: Api) -> None:
    await clean(api)
    await api.login()
    candle = {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
    async with api.db.begin() as conn:
        await conn.execute(
            insert(CandleRow),
            [
                {
                    "exchange": "mexc",
                    "symbol": "BTC/USDT",
                    "timeframe": "4h",
                    "ts": T0 + h * 4 * HOUR,
                }
                | candle
                for h in range(3)
            ]
            + [{"exchange": "mexc", "symbol": "BTC/USDT", "timeframe": "1h", "ts": T0} | candle],
        )

    stored = await api.client.get(
        "/candles/range", params={"symbol": "BTC/USDT", "timeframe": "4h"}
    )
    none = await api.client.get("/candles/range", params={"symbol": "ETH/USDT", "timeframe": "4h"})

    assert stored.json() == {
        "exchange": "mexc",
        "symbol": "BTC/USDT",
        "timeframe": "4h",
        "first": "2026-10-26T12:00:05Z",
        "last": "2026-10-26T20:00:05Z",  # the third, 8 hours on
        "count": 3,
    }
    assert (none.json()["first"], none.json()["count"]) == (None, 0)
