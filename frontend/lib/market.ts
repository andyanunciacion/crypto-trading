/**
 * The Market chart (D46): what `GET /api/bots/{id}/chart` and `WS /api/ws/bots/{id}/chart`
 * send, and why a trade happened (`GET /api/orders/{id}/explain`, UX9). Mirrors
 * `backend/market/chart.py` and `explain.py`. Money and prices are Decimal strings;
 * indicator values are numbers, as the strategies compute them.
 */
import type { Decimal } from "@/lib/format";

export type ChartBot = {
  id: number;
  name: string;
  exchange: string;
  symbol: string;
  timeframe: string;
  status: string;
  strategy: string | null;
  version: number | null;
  params: Record<string, unknown> | null;
};

export type Candle = {
  ts: string; // open time
  open: Decimal;
  high: Decimal;
  low: Decimal;
  close: Decimal;
  volume: Decimal;
};

/** The candle still open, as the engine last saw it on the exchange (display only). */
export type FormingCandle = Candle & {
  exchange: string;
  symbol: string;
  timeframe: string;
  received_at: string;
};

export type IndicatorPoint = { ts: string; values: Record<string, number> };

export type Indicators = {
  /** "price" lines share the candles' scale; "oscillator" ones (RSI) get a pane below. */
  pane: "price" | "oscillator";
  names: string[];
  guides: Record<string, number>;
  points: IndicatorPoint[];
};

export type TradeKind =
  "signal" | "stop_loss" | "trailing_stop" | "take_profit" | "kill_switch" | "close" | "switch";

export type TradeMarker = {
  order_id: number;
  client_order_id: string;
  side: "buy" | "sell";
  kind: TradeKind | string;
  ts: string; // its first fill
  price: Decimal; // the average fill price
  amount: Decimal; // filled so far
  status: string;
};

export type Chart = {
  server_time: string;
  bot: ChartBot;
  candles: Candle[];
  indicators: Indicators | null;
  forming: FormingCandle | null;
  trades: TradeMarker[];
};

export type ChartUpdate = {
  server_time: string;
  candles: Candle[];
  points: IndicatorPoint[];
  forming: FormingCandle | null;
  trades: TradeMarker[] | null; // null: unchanged
};

export type TradeExplanation = {
  order_id: number;
  bot_id: number;
  client_order_id: string;
  exchange: string;
  symbol: string;
  side: "buy" | "sell";
  price: Decimal;
  amount: Decimal;
  filled: Decimal;
  status: string;
  created_at: string;
  signal: {
    action: string;
    ts: string;
    reason: string;
    indicators: Record<string, number>;
    size_hint: Decimal | null;
    stop: Decimal | null;
  } | null;
  strategy_config: {
    id: number;
    strategy: string;
    version: number;
    params: Record<string, unknown>;
  } | null;
  sizing: { settings: Record<string, unknown>; detail: string } | null;
  stops: { settings: Record<string, unknown>; atr: Decimal | null } | null;
  exit: {
    kind: string;
    attempt: number;
    bid: Decimal;
    price_ts: string;
    levels: Record<string, Decimal | null> | null;
    command: string | null;
  } | null;
  risk: {
    decision: string;
    rule: string;
    detail: string;
    requested_amount: Decimal | null;
  } | null;
  risk_events: {
    ts: string;
    rule: string;
    decision: string;
    detail: string;
    inputs: Record<string, unknown>;
  }[];
  fills: { ts: string; price: Decimal; amount: Decimal; fee: Decimal; fee_currency: string }[];
  reason: Record<string, unknown>;
};

/** The forming candle is stale after this long without an update (the engine's own
 * kline watchdog reconnects after 30 s of silence). */
export const FORMING_STALE_SECONDS = 30;

/** Short marker labels, and what they stand for. */
export const KIND_LABEL: Record<string, { short: string; long: string }> = {
  signal: { short: "", long: "strategy signal" },
  stop_loss: { short: "SL", long: "stop-loss" },
  trailing_stop: { short: "TS", long: "trailing stop" },
  take_profit: { short: "TP", long: "take-profit" },
  kill_switch: { short: "KILL", long: "kill switch" },
  close: { short: "CLOSE", long: "close from the Bots page" },
  switch: { short: "SW", long: "closed for a strategy switch" },
};

export function tradeLabel(trade: Pick<TradeMarker, "side" | "kind">): string {
  const side = trade.side === "buy" ? "B" : "S";
  const kind = KIND_LABEL[trade.kind]?.short ?? trade.kind;
  return kind ? `${side} ${kind}` : side;
}

/** Digits after the point in a decimal string: "81819.48" → 2. */
export function decimalsOf(value: Decimal): number {
  const point = value.indexOf(".");
  return point === -1 ? 0 : value.length - point - 1;
}
