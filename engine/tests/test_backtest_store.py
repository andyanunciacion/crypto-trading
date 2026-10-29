"""Storing backtest runs and reproducing them, against a real TimescaleDB (see shared.testing)."""

import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import URL, text

from engine.backtest.models import BacktestConfig
from engine.backtest.run import backtest, reproduce
from engine.backtest.simulator import BacktestError, run_backtest
from engine.backtest.store import BacktestRunStore, candles_sha256
from engine.exchange.models import Candle, MarketRules
from engine.marketdata.store import CandleStore, Series
from engine.regime.detector import RegimeDetector
from engine.risk.sizing import AtrRisk
from engine.risk.stops import Percent, Stops
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from shared.db.session import make_engine
from shared.regime import AutoSwitchSettings

T0 = datetime(2025, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
HOURS = 300
BTC = Series("mexc", "BTC/USDT", "1h")
RULES = MarketRules(
    symbol="BTC/USDT",
    amount_step=Decimal("0.000001"),
    price_step=Decimal("0.01"),
    min_amount=Decimal("0.000001"),
    min_cost=Decimal("1"),
)
STRATEGY = EmaCrossover(EmaCrossoverParams(fast=3, slow=8))
CONFIG = BacktestConfig(
    strategy=STRATEGY.name,
    strategy_version=STRATEGY.version,
    params=STRATEGY.params.model_dump(mode="json"),
    exchange="mexc",
    symbol="BTC/USDT",
    timeframe="1h",
    start=T0 + STRATEGY.warmup_candles * HOUR,
    end=T0 + HOURS * HOUR,
    fee_rate=Decimal("0.001"),
    slippage_rate=Decimal("0.0005"),
    market_rules=RULES,
)


def wave(hour: int, wobble: float = 150) -> Candle:
    # A slow sine wave with a fast wobble on top, so the EMAs cross several times.
    close = round(30000 + 2000 * math.sin(hour / 20) + wobble * math.sin(hour), 2)
    open_ = round(close * (1 + 0.001 * math.cos(hour)), 2)
    o, c = Decimal(str(open_)), Decimal(str(close))
    return Candle(
        ts=T0 + hour * HOUR,
        open=o,
        high=max(o, c) + 10,
        low=min(o, c) - 10,
        close=c,
        volume=Decimal("12.5"),
    )


@dataclass(frozen=True)
class Stores:
    candles: CandleStore
    runs: BacktestRunStore


@pytest.fixture
async def stores(db_url: URL) -> AsyncIterator[Stores]:
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE candles, backtest_runs"))
    candles = CandleStore(engine)
    await candles.upsert(BTC, [wave(h) for h in range(HOURS)])
    yield Stores(candles, BacktestRunStore(engine))
    await engine.dispose()


async def test_a_run_reproduces_from_its_stored_inputs(stores: Stores) -> None:
    run_id, result = await backtest(stores.candles, stores.runs, STRATEGY, CONFIG)
    assert len(result.fills) >= 4  # the wave really traded, both ways

    assert await reproduce(stores.candles, stores.runs, run_id) == result


async def test_save_and_load_round_trip_exactly(stores: Stores) -> None:
    run_id, result = await backtest(stores.candles, stores.runs, STRATEGY, CONFIG)

    stored = await stores.runs.load(run_id)

    assert stored.id == run_id
    assert stored.config == CONFIG
    assert stored.result == result  # every Decimal and every float indicator
    assert stored.warmup_candles == STRATEGY.warmup_candles
    assert stored.candle_count == HOURS
    assert stored.candles_sha256 == candles_sha256([wave(h) for h in range(HOURS)])
    assert stored.created_at.tzinfo is not None


async def test_an_atr_sized_run_stores_its_sizing_and_reproduces(stores: Stores) -> None:
    # ATR over 14 candles needs 6 x 14 + 1 = 85 before the start, more than the
    # strategy's own 25: the run loads and stores the larger warm-up.
    sizing = AtrRisk(risk_percent=Decimal("0.5"))
    config = CONFIG.model_copy(update={"start": T0 + 85 * HOUR, "sizing": sizing})
    run_id, result = await backtest(stores.candles, stores.runs, STRATEGY, config)
    # Half a percent at risk buys less than all the cash would, on the same range.
    all_in = CONFIG.model_copy(update={"start": config.start})
    first_all_in = run_backtest(STRATEGY, [wave(h) for h in range(HOURS)], all_in).fills[0]
    assert result.fills and result.fills[0].ts == first_all_in.ts
    assert result.fills[0].amount < first_all_in.amount

    stored = await stores.runs.load(run_id)

    assert stored.config.sizing == sizing
    assert stored.warmup_candles == 85
    assert await reproduce(stores.candles, stores.runs, run_id) == result


async def test_a_run_with_stops_stores_them_and_reproduces(stores: Stores) -> None:
    # A tight trailing stop on the wave: some trades end at a stop, not the strategy's sell.
    stops = Stops(stop_loss=Percent(percent=Decimal(2)), trailing=Percent(percent=Decimal(1)))
    config = CONFIG.model_copy(update={"stops": stops})
    run_id, result = await backtest(stores.candles, stores.runs, STRATEGY, config)
    assert any(f.exit is not None for f in result.fills)

    stored = await stores.runs.load(run_id)

    assert stored.config.stops == stops
    assert stored.result == result
    assert await reproduce(stores.candles, stores.runs, run_id) == result


AUTO = AutoSwitchSettings.model_validate(
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
        "policy": "close_first",
    }
)
# The detector's 199 candles before the first reading.
AUTO_CONFIG = CONFIG.model_copy(update={"start": T0 + 199 * HOUR, "auto_switch": AUTO})


async def test_an_auto_switching_run_stores_its_settings_and_reproduces(stores: Stores) -> None:
    run_id, result = await backtest(stores.candles, stores.runs, STRATEGY, AUTO_CONFIG)
    assert result.switches  # the wave goes from trending to ranging and back

    stored = await stores.runs.load(run_id)

    assert stored.config.auto_switch == AUTO
    assert stored.regime_detector_version == RegimeDetector.version
    assert stored.warmup_candles == 199
    assert stored.result == result  # its switches, and which config made each fill
    assert await reproduce(stores.candles, stores.runs, run_id) == result


async def test_a_plain_run_stores_no_auto_switching(stores: Stores) -> None:
    run_id, _ = await backtest(stores.candles, stores.runs, STRATEGY, CONFIG)

    stored = await stores.runs.load(run_id)

    assert stored.config.auto_switch is None
    assert stored.regime_detector_version is None
    assert stored.result.switches == []


async def test_a_new_detector_version_refuses_to_reproduce(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _ = await backtest(stores.candles, stores.runs, STRATEGY, AUTO_CONFIG)
    monkeypatch.setattr(RegimeDetector, "version", 2)  # as if its rules changed since

    with pytest.raises(BacktestError, match="regime detector version 1, this is version 2"):
        await reproduce(stores.candles, stores.runs, run_id)


async def test_changed_candles_refuse_to_reproduce(stores: Stores) -> None:
    run_id, _ = await backtest(stores.candles, stores.runs, STRATEGY, CONFIG)
    await stores.candles.upsert(BTC, [wave(100, wobble=151)])  # say, a re-download differs

    with pytest.raises(BacktestError, match="changed"):
        await reproduce(stores.candles, stores.runs, run_id)


async def test_a_new_strategy_version_refuses_to_reproduce(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _ = await backtest(stores.candles, stores.runs, STRATEGY, CONFIG)
    monkeypatch.setattr(EmaCrossover, "version", 2)  # as if its logic changed since

    with pytest.raises(ValueError, match="version"):
        await reproduce(stores.candles, stores.runs, run_id)


async def test_loading_a_missing_run_is_an_error(stores: Stores) -> None:
    with pytest.raises(LookupError, match="no backtest run 999"):
        await stores.runs.load(999)


def test_the_candle_fingerprint_ignores_trailing_zeros_only() -> None:
    candle = wave(1)
    padded = candle.model_copy(update={"close": Decimal(f"{candle.close:.6f}")})  # 30123.450000
    moved = candle.model_copy(update={"close": candle.close + Decimal("0.01")})

    assert candles_sha256([padded]) == candles_sha256([candle])
    assert candles_sha256([moved]) != candles_sha256([candle])
