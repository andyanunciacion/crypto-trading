/**
 * The Backtests page (D48): a run sent to the backtester as a job, and the stored runs
 * with their metrics. Mirrors `shared/backtest_jobs.py` and `backend/backtests/store.py`.
 * Ratios (returns, drawdowns, win rate) are fractions, as the API sends them: 0.05 = 5%.
 */
import type { Settings } from "@/lib/bots";
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
  candle_count: number;
  candles_sha256: string;
  backtester_version: number;
  metrics: Metrics | null;
  problem: string | null;
};

export type ExitKind = "stop_loss" | "trailing_stop" | "take_profit";

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
};

export type EquityPoint = { ts: string; equity: Decimal; drawdown: Decimal };

export type SkippedOrder = { signal_ts: string; side: "buy" | "sell"; reason: string };

export type RunDetail = RunSummary & {
  trades: Trade[];
  equity: EquityPoint[];
  skipped: SkippedOrder[];
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
};
