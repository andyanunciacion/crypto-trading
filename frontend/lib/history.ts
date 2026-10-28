/**
 * History (P5-6, D49): every order and fill, as `GET /api/history/orders|fills` send
 * them (mirrors `backend/history/store.py`), and the filters, which live in the page's
 * URL. The table and its CSV link are built from the same query, so an export is
 * exactly what the filters show.
 */
import type { Decimal } from "@/lib/format";

export type OrderRow = {
  id: number;
  created_at: string;
  updated_at: string;
  bot_id: number;
  bot_name: string;
  mode: string;
  exchange: string;
  symbol: string;
  client_order_id: string;
  side: "buy" | "sell";
  kind: string;
  status: string;
  price: Decimal; // the limit
  amount: Decimal;
  filled: Decimal;
  avg_fill_price: Decimal | null;
  fees: Decimal | null; // null: no fills, or fees in more than one currency
  fee_currency: string | null;
  reject_reason: string | null;
};

export type FillRow = {
  id: number;
  ts: string;
  order_id: number;
  client_order_id: string;
  bot_id: number;
  bot_name: string;
  symbol: string;
  side: "buy" | "sell";
  kind: string;
  price: Decimal;
  amount: Decimal;
  value: Decimal; // price × amount, in the quote currency
  fee: Decimal;
  fee_currency: string;
};

export type HistoryTab = "orders" | "fills";

/** Rows per page; the API takes up to 500. */
export const PAGE_ROWS = 100;

export const ORDER_STATUSES = ["open", "closed", "canceled", "rejected"] as const;

/** The filters as the URL holds them; "" means any. `from`/`to` are Manila days. */
export type HistoryFilters = {
  bot: string;
  symbol: string;
  side: string;
  status: string;
  kind: string;
  from: string; // "2026-10-25"
  to: string; // inclusive: the whole of that day
};

export const FILTER_NAMES = ["bot", "symbol", "side", "status", "kind", "from", "to"] as const;

export function filtersFrom(params: URLSearchParams): HistoryFilters {
  const get = (name: string) => params.get(name) ?? "";
  return {
    bot: get("bot"),
    symbol: get("symbol"),
    side: get("side"),
    status: get("status"),
    kind: get("kind"),
    from: get("from"),
    to: get("to"),
  };
}

const DAY = /^(\d{4})-(\d{2})-(\d{2})$/;
const MANILA_OFFSET_MS = 8 * 3600 * 1000; // UTC+8 all year: no daylight saving since 1978

/**
 * The instant a Manila day starts, `days` after `day` (an `<input type="date">` value),
 * as UTC: "2026-10-25" → "2026-10-24T16:00:00.000Z". Null for anything else.
 */
export function manilaMidnight(day: string, days = 0): string | null {
  const parts = DAY.exec(day);
  if (parts === null) return null;
  const [, year, month, date] = parts;
  const utc = Date.UTC(Number(year), Number(month) - 1, Number(date) + days);
  return new Date(utc - MANILA_OFFSET_MS).toISOString();
}

/** The API's query for `filters`: Manila days become a UTC range, `to` inclusive. */
export function historyQuery(tab: HistoryTab, filters: HistoryFilters): URLSearchParams {
  const query = new URLSearchParams();
  if (filters.bot) query.set("bot_id", filters.bot);
  if (filters.symbol) query.set("symbol", filters.symbol);
  if (filters.side) query.set("side", filters.side);
  if (filters.kind) query.set("kind", filters.kind);
  if (tab === "orders" && filters.status) query.set("status", filters.status);
  const start = manilaMidnight(filters.from);
  const end = manilaMidnight(filters.to, 1);
  if (start !== null) query.set("start", start);
  if (end !== null) query.set("end", end);
  return query;
}

/** What the browser saves the export as, named by the Manila days it covers. */
export function csvFileName(tab: HistoryTab, filters: HistoryFilters): string {
  return `${tab}_${filters.from || "first"}_to_${filters.to || "latest"}.csv`;
}
