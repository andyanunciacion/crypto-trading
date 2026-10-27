/**
 * The Overview the API pushes over `/api/ws/overview` every second (D45), and how old
 * each part of it is (DESIGN UX5). Mirrors `backend/dashboard/overview.py`.
 */
import type { Decimal } from "@/lib/format";

export type Mode = "paper" | "live";

export type EngineHeartbeat = {
  ts: string;
  started_at: string;
  mode: Mode;
  beat: number;
  exchange: string;
  symbols: string[];
  /** The bots it runs, warmed up (D47): a bot just started is here seconds later. */
  bots: number[];
};

export type Account = {
  mode: "paper";
  currency: string;
  equity: Decimal;
  deposits: Decimal;
  today_pnl: Decimal;
  today_pnl_pct: Decimal | null;
  total_pnl: Decimal;
  total_pnl_pct: Decimal | null;
  peak: Decimal;
  drawdown_pct: Decimal;
};

export type Position = {
  bot_id: number;
  bot_name: string;
  exchange: string;
  symbol: string;
  amount: Decimal;
  avg_entry: Decimal;
  bid: Decimal | null;
  price_ts: string | null;
  value: Decimal;
  pnl: Decimal;
  pnl_pct: Decimal | null;
  stop: Decimal | null;
  take_profit: Decimal | null;
  opened_at: string;
};

export type BotStatus = "running" | "paused" | "stopped";

export type Bot = {
  id: number;
  name: string;
  exchange: string;
  symbol: string;
  timeframe: string;
  mode: Mode;
  status: BotStatus;
  strategy: string | null;
  paused_reason: string | null;
  paused_at: string | null;
  stopped_reason: string | null;
  stopped_at: string | null;
};

export type Price = {
  exchange: string;
  symbol: string;
  bid: Decimal | null;
  ask: Decimal | null;
  ts: string | null;
};

export type Overview = {
  server_time: string;
  mode: Mode;
  engine: EngineHeartbeat | null;
  account: Account;
  positions: Position[];
  bots: Bot[];
  prices: Price[];
};

/** The engine is stale after this long without a heartbeat (as Docker's health check). */
export const ENGINE_STALE_SECONDS = 30;
/** A price is stale after this long: the PaperBroker won't fill on an older one. */
export const PRICE_STALE_SECONDS = 10;
/** The feed itself is stale after this long without a snapshot (it sends one a second). */
export const FEED_STALE_SECONDS = 5;
