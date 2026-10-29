/**
 * The Backtests page (D48): a run sent to the backtester as a job, and the stored runs
 * with their metrics. Mirrors `shared/backtest_jobs.py` and `backend/backtests/store.py`.
 * Ratios (returns, drawdowns, win rate) are fractions, as the API sends them: 0.05 = 5%.
 */
import type { AutoSwitchSettings, MappedRegime, Settings } from "@/lib/bots";
import type { Decimal } from "@/lib/format";

/** `engine.backtest.run`'s arguments. */
export type NewBacktest = {
  strategy: string;
  params: Settings;
  symbol: string;
  timeframe: string;
  days: number;
  cash: Decimal;
  fee_rate: Decimal;
  slippage: Decimal | null; // null: the pair's default
  sizing: Settings;
  stops: Record<string, Settings | number>;
};

export type JobStatus = "queued" | "running" | "done" | "failed";

export type BacktestJob = {
  id: string;
  status: JobStatus;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  run_id: number | null;
  error: string | null;
  /** Failed for too little stored history: the most days that would fit (null: none). */
  max_days: number | null;
};

export type Metrics = {
  total_return: Decimal;
  max_drawdown: Decimal;
  sharpe: Decimal | null;
  trade_count: number;
  win_rate: Decimal | null;
  profit_factor: Decimal | null;
  open_trade: boolean;
  final_equity: Decimal;
  total_fees: Decimal;
};

export type RunSummary = {
  id: number;
  created_at: string;
  strategy: string;
  strategy_version: number;
  params: Record<string, unknown>;
  exchange: string;
  symbol: string;
  timeframe: string;
  currency: string;
  range_start: string;
  range_end: string;
  warmup_candles: number;
  initial_cash: Decimal;
  fee_rate: Decimal;
  slippage_rate: Decimal;
  sizing: Record<string, unknown>;
  stops: Record<string, unknown>;
  /** Auto-switching (P6-5): the strategy above is the config it started on. Null: a plain run. */
  auto_switch: AutoSwitchSettings | null;
  candle_count: number;
  candles_sha256: string;
  backtester_version: number;
  metrics: Metrics | null;
  problem: string | null;
};

/** A stop's sale, or a `close_first` auto-switch's. */
export type ExitKind = "stop_loss" | "trailing_stop" | "take_profit" | "switch";

/** A strategy at a version, with its params (`shared/backtest.py`'s ConfigRef). */
export type ConfigRef = { strategy: string; version: number; params: Record<string, unknown> };

/** A switch an auto-switching run made: confirmed at `ts`'s close, `to_config` from the next candle. */
export type BacktestSwitch = {
  ts: string;
  regime: MappedRegime;
  from_config: ConfigRef;
  to_config: ConfigRef;
  reason: string;
};

export type Trade = {
  entry_ts: string;
  exit_ts: string;
  amount: Decimal;
  entry_price: Decimal;
  exit_price: Decimal;
  fees: Decimal;
  pnl: Decimal;
  return_: Decimal;
  exit: ExitKind | null;
  /** In an auto-switching run, the config that bought it; null otherwise. */
  decided_by: ConfigRef | null;
};

export type EquityPoint = { ts: string; equity: Decimal; drawdown: Decimal };

export type SkippedOrder = { signal_ts: string; side: "buy" | "sell"; reason: string };

export type RunDetail = RunSummary & {
  trades: Trade[];
  equity: EquityPoint[];
  skipped: SkippedOrder[];
  switches: BacktestSwitch[];
  fills: number;
  signals: number;
};

export type CandleRange = {
  exchange: string;
  symbol: string;
  timeframe: string;
  first: string | null;
  last: string | null;
  count: number;
};

/** Whole days between a run's first candle and its end. */
export function rangeDays(run: RunSummary): number {
  return Math.round((Date.parse(run.range_end) - Date.parse(run.range_start)) / 86_400_000);
}

export const EXIT_LABELS: Record<ExitKind, string> = {
  stop_loss: "stop-loss",
  trailing_stop: "trailing stop",
  take_profit: "take-profit",
  switch: "switch (close first)",
};

/** "ema_crossover v1": a config's strategy and version (its params are shown beside it). */
export function describeConfig(config: { strategy: string; version?: number | null }): string {
  return config.version == null ? config.strategy : `${config.strategy} v${config.version}`;
}
