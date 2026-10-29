"""Saves and loads backtest runs in the `backtest_runs` table."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.backtest.models import BacktestConfig, BacktestResult
from engine.backtest.simulator import BACKTESTER_VERSION
from engine.exchange.models import Candle, MarketRules
from engine.regime.detector import RegimeDetector
from engine.risk.sizing import SIZING
from engine.risk.stops import STOPS
from shared.db.models import BacktestRunRow
from shared.regime import AutoSwitchSettings


def candles_sha256(candles: Sequence[Candle]) -> str:
    """A fingerprint of the exact candle values, so a re-run can prove it used the same data.

    Decimals are normalized first: 100.50 and 100.5 are the same price.
    """
    digest = hashlib.sha256()
    for c in candles:
        values = (c.open, c.high, c.low, c.close, c.volume)
        line = ",".join([c.ts.astimezone(UTC).isoformat(), *(f"{v.normalize():f}" for v in values)])
        digest.update(f"{line}\n".encode())
    return digest.hexdigest()


@dataclass(frozen=True)
class StoredRun:
    id: int
    created_at: datetime
    config: BacktestConfig
    warmup_candles: int
    candle_count: int
    candles_sha256: str
    backtester_version: int
    regime_detector_version: int | None  # auto-switching runs only
    result: BacktestResult


class BacktestRunStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def save(
        self,
        config: BacktestConfig,
        warmup_candles: int,
        candles: Sequence[Candle],
        result: BacktestResult,
    ) -> int:
        """Store a finished run; returns its id. `candles` are all it ran on, warm-up included."""
        auto = config.auto_switch
        stmt = (
            insert(BacktestRunRow)
            .values(
                strategy=config.strategy,
                strategy_version=config.strategy_version,
                params=config.params,
                exchange=config.exchange,
                symbol=config.symbol,
                timeframe=config.timeframe,
                range_start=config.start,
                range_end=config.end,
                warmup_candles=warmup_candles,
                initial_cash=config.initial_cash,
                fee_rate=config.fee_rate,
                slippage_rate=config.slippage_rate,
                market_rules=config.market_rules.model_dump(mode="json"),
                sizing=SIZING.dump_python(config.sizing, mode="json"),
                stops=STOPS.dump_python(config.stops, mode="json"),
                auto_switch=None if auto is None else auto.model_dump(mode="json"),
                regime_detector_version=None if auto is None else RegimeDetector.version,
                candle_count=len(candles),
                candles_sha256=candles_sha256(candles),
                backtester_version=BACKTESTER_VERSION,
                final_equity=result.final_equity,
                total_fees=result.total_fees,
                result=result.model_dump(mode="json"),
            )
            .returning(BacktestRunRow.id)
        )
        async with self._engine.begin() as conn:
            run_id: int = (await conn.execute(stmt)).scalar_one()
        return run_id

    async def load(self, run_id: int) -> StoredRun:
        query = select(BacktestRunRow.__table__).where(BacktestRunRow.id == run_id)
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).mappings().one_or_none()
        if row is None:
            raise LookupError(f"no backtest run {run_id}")
        config = BacktestConfig(
            strategy=row["strategy"],
            strategy_version=row["strategy_version"],
            params=row["params"],
            exchange=row["exchange"],
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            start=row["range_start"],
            end=row["range_end"],
            initial_cash=row["initial_cash"],
            fee_rate=row["fee_rate"],
            slippage_rate=row["slippage_rate"],
            market_rules=MarketRules.model_validate(row["market_rules"]),
            sizing=SIZING.validate_python(row["sizing"]),
            stops=STOPS.validate_python(row["stops"]),
            auto_switch=None
            if row["auto_switch"] is None
            else AutoSwitchSettings.model_validate(row["auto_switch"]),
        )
        return StoredRun(
            id=row["id"],
            created_at=row["created_at"],
            config=config,
            warmup_candles=row["warmup_candles"],
            candle_count=row["candle_count"],
            candles_sha256=row["candles_sha256"],
            backtester_version=row["backtester_version"],
            regime_detector_version=row["regime_detector_version"],
            result=BacktestResult.model_validate(row["result"]),
        )
