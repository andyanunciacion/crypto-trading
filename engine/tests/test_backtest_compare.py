"""The baselines an auto-switching run is judged against (P6-5): each config alone, and
buy-and-hold, over the same candles."""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.backtest.compare import baselines, describe_baselines
from engine.backtest.models import BacktestConfig
from engine.backtest.simulator import BacktestError, config_ref, run_backtest
from engine.exchange.models import Candle, MarketRules
from engine.risk.sizing import PercentOfEquity
from engine.strategies.registry import build
from shared.backtest import metrics
from shared.regime import AutoSwitchSettings

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
D = Decimal
RULES = MarketRules(
    symbol="BTC/USDT",
    amount_step=D("0.0001"),
    price_step=D("0.01"),
    min_amount=D("0.0001"),
    min_cost=D("1"),
)
EMA = build("ema_crossover", 1, {"fast": 3, "slow": 8})
SETTINGS = AutoSwitchSettings.model_validate(
    {
        "mapping": {
            "trending_up": {
                "strategy": "ema_crossover",
                "version": 1,
                "params": {"fast": 3, "slow": 8},
            },
            "ranging": {"strategy": "rsi_mean_reversion", "version": 1, "params": {"period": 2}},
            "trending_down": {"strategy": "stay_flat", "version": 1},
            "high_volatility": {"strategy": "stay_flat", "version": 1},
        },
        "min_hold": 1,
    }
)
START = 199  # the detector's window


def wave(hour: int) -> Candle:
    close = D(str(round(100 + 10 * math.sin(hour / 15) + hour / 20, 2)))
    return Candle(
        ts=T0 + hour * HOUR, open=close, high=close + 1, low=close - 1, close=close, volume=D(1)
    )


CANDLES = [wave(h) for h in range(300)]


def config(**overrides: object) -> BacktestConfig:
    values: dict[str, object] = {
        "strategy": EMA.name,
        "strategy_version": EMA.version,
        "params": EMA.params.model_dump(mode="json"),
        "exchange": "mexc",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "start": T0 + START * HOUR,
        "end": T0 + len(CANDLES) * HOUR,
        "fee_rate": D("0.001"),
        "slippage_rate": D(0),
        "market_rules": RULES,
        "auto_switch": SETTINGS,
        "sizing": PercentOfEquity(percent=D(50)),
    }
    return BacktestConfig.model_validate(values | overrides)


def test_each_distinct_config_runs_alone_with_the_same_costs() -> None:
    found = baselines(EMA, CANDLES, config())

    # EMA is where it starts and the uptrend's config: once. Stay flat is two regimes': once.
    assert [b.label for b in found] == [
        'ema_crossover v1 {"fast": 3, "slow": 8}',
        'rsi_mean_reversion v1 {"period": 2, "oversold": 30.0, "overbought": 70.0}',
        "stay_flat v1 {}",
        "buy and hold",
    ]
    rsi = build("rsi_mean_reversion", 1, {"period": 2})
    alone = config(
        strategy=rsi.name,
        params=rsi.params.model_dump(mode="json"),
        auto_switch=None,
    )
    assert found[1].result == run_backtest(rsi, CANDLES, alone)
    assert found[1].result.switches == []
    assert found[2].result.fills == []  # stay flat never buys


def test_buy_and_hold_buys_everything_at_the_first_open_and_holds() -> None:
    [*_, hold] = baselines(EMA, CANDLES, config())

    # All in at the first open, whatever the run's sizing: 1000 / (p x 1.001), to 0.0001.
    first, last = CANDLES[START], CANDLES[-1]
    [buy] = hold.result.fills
    assert (buy.ts, buy.side, buy.price) == (first.ts, "buy", first.open)
    amount = (D(1000) / (first.open * D("1.001"))).quantize(D("0.0001"), rounding="ROUND_DOWN")
    assert buy.amount == amount
    assert hold.result.final_equity == buy.cash_after + amount * last.close


def test_each_baseline_names_its_config() -> None:
    found = baselines(EMA, CANDLES, config())

    assert [b.config for b in found[:-1]] == [
        config_ref(EMA),
        config_ref(build("rsi_mean_reversion", 1, {"period": 2})),
        config_ref(build("stay_flat", 1, {})),
    ]
    assert found[-1].config is None  # buy and hold isn't a config a run can switch to


def test_a_stored_baseline_keeps_what_its_metrics_need() -> None:
    cfg = config()
    ema = baselines(EMA, CANDLES, cfg)[0]

    stored = ema.stored()

    assert (stored.label, stored.config) == (ema.label, ema.config)
    assert stored.fills == ema.result.fills
    assert stored.equity_curve == ema.result.equity_curve
    # The metrics from the slim copy are the full result's, to the digit.
    assert metrics(cfg.initial_cash, cfg.timeframe, stored) == metrics(
        cfg.initial_cash, cfg.timeframe, ema.result
    )


def test_the_table_ranks_the_run_among_its_baselines() -> None:
    cfg = config()
    combined = run_backtest(EMA, CANDLES, cfg)

    lines = describe_baselines(cfg, combined, baselines(EMA, CANDLES, cfg)).splitlines()

    assert lines[0] == "  against each config alone, over the same candles:"
    assert len(lines) == 6
    assert sum("auto-switching (this run)" in line for line in lines) == 1
    returns = [D(line.split("%")[0].split()[-1]) for line in lines[1:]]
    assert returns == sorted(returns, reverse=True)


def test_a_plain_run_has_no_baselines() -> None:
    with pytest.raises(BacktestError, match="auto-switching"):
        baselines(EMA, CANDLES, config(auto_switch=None))
