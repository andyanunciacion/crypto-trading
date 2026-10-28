/**
 * Calls to the API, which the browser reaches at /api on the dashboard's own origin
 * (next.config.ts rewrites it to the API service). Same origin means the httpOnly
 * session cookie goes along by itself, and no CORS is needed (D44).
 */

import type { BacktestJob, CandleRange, NewBacktest, RunDetail, RunSummary } from "@/lib/backtests";
import type {
  BotDetail,
  BotReply,
  BotSetup,
  Catalog,
  NewBot,
  Rollback,
  StrategySwitch,
} from "@/lib/bots";
import { PAGE_ROWS, type FillRow, type OrderRow } from "@/lib/history";
import type { LogPage, RiskEvent } from "@/lib/logs";
import type { Chart, TradeExplanation } from "@/lib/market";
import type {
  ExchangeAccount,
  Mode,
  NewAccount,
  NewKeys,
  RiskSettings,
  StoredRiskSettings,
} from "@/lib/settings";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** The answer's JSON, for refusals that carry more than a message (`needs_code`). */
    readonly body: unknown = null,
  ) {
    super(message);
  }
}

async function call(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    throw new ApiError(response.status, detailOf(body) ?? `HTTP ${response.status}`, body);
  }
  return response;
}

/** FastAPI's `detail`: a message, or for a request that didn't validate, a list of them. */
function detailOf(body: unknown): string | null {
  if (typeof body !== "object" || body === null || !("detail" in body)) return null;
  const { detail } = body;
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return null;
  const messages = detail.flatMap((problem: unknown) => {
    if (typeof problem !== "object" || problem === null || !("msg" in problem)) return [];
    const where =
      "loc" in problem && Array.isArray(problem.loc) ? problem.loc.slice(1).join(".") : "";
    return [where ? `${where}: ${String(problem.msg)}` : String(problem.msg)];
  });
  return messages.length > 0 ? messages.join("; ") : null;
}

export async function signIn(username: string, password: string, code: string): Promise<void> {
  await call("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password, code }),
  });
}

export async function signOut(): Promise<void> {
  await call("/auth/logout", { method: "POST" });
}

export type Me = { username: string };

export async function me(): Promise<Me> {
  return (await (await call("/auth/me")).json()) as Me;
}

/** A bot's Market chart (D46): its newest `limit` candles, indicators and trades. */
export async function chart(botId: number, limit?: number): Promise<Chart> {
  const query = limit === undefined ? "" : `?limit=${limit}`;
  return (await (await call(`/bots/${botId}/chart${query}`)).json()) as Chart;
}

/** Why a trade happened (UX9): its signal, risk check and fills. */
export async function explain(orderId: number): Promise<TradeExplanation> {
  return (await (await call(`/orders/${orderId}/explain`)).json()) as TradeExplanation;
}

/** The engine's answer to the kill switch (`shared/commands.py`). */
export type KillReply = {
  id: string;
  ok: boolean;
  error: string | null;
  stopped: number[];
  canceled: string[];
  closing: {
    bot_id: number;
    symbol: string;
    amount: string;
    client_order_id: string | null;
    status: string | null;
    filled: string | null;
  }[];
  not_closed: { bot_id: number; symbol: string; reason: string }[];
  took_ms: number;
};

/**
 * The kill switch (D43, D45): stop every bot and cancel every open order, and with
 * `closePositions` sell every position too. Throws an ApiError with status 504 if the
 * engine didn't answer in time: the command then stays queued for when it starts.
 */
export async function kill(closePositions: boolean): Promise<KillReply> {
  const response = await call("/engine/kill", {
    method: "POST",
    body: JSON.stringify({ close_positions: closePositions }),
  });
  return (await response.json()) as KillReply;
}

/** What a new bot can be set up with, as the engine described it (D47). 503: not yet. */
export async function catalog(): Promise<Catalog> {
  return (await (await call("/strategies")).json()) as Catalog;
}

/** Every bot with its setup. */
export async function bots(): Promise<BotSetup[]> {
  return (await (await call("/bots")).json()) as BotSetup[];
}

/** One bot's setup and every strategy config it has had (D52). 404: no such bot. */
export async function bot(botId: number): Promise<BotDetail> {
  return (await (await call(`/bots/${botId}`)).json()) as BotDetail;
}

/**
 * The Bots page's commands, carried out by the engine (D47). Throws an ApiError: 422
 * with the engine's reason when it refuses, 504 when it didn't answer in time (the
 * command is then dropped).
 */
export async function createBot(bot: NewBot): Promise<BotReply> {
  const response = await call("/bots", { method: "POST", body: JSON.stringify(bot) });
  return (await response.json()) as BotReply;
}

/** Switch a bot's strategy or params, with a policy for its open position (D51). */
export async function switchStrategy(botId: number, change: StrategySwitch): Promise<BotReply> {
  const response = await call(`/bots/${botId}/strategy`, {
    method: "POST",
    body: JSON.stringify(change),
  });
  return (await response.json()) as BotReply;
}

/** Switch a bot back to one of its earlier configs, as a new config copying it (D52). */
export async function rollback(botId: number, change: Rollback): Promise<BotReply> {
  const response = await call(`/bots/${botId}/rollback`, {
    method: "POST",
    body: JSON.stringify(change),
  });
  return (await response.json()) as BotReply;
}

/**
 * Queue a backtest for the backtester (D48); answers at once with the job, `queued`.
 * Follow it with `backtestJob`: the backtester checks everything and fails the job
 * with the reason.
 */
export async function runBacktest(backtest: NewBacktest): Promise<BacktestJob> {
  const response = await call("/backtests", { method: "POST", body: JSON.stringify(backtest) });
  return (await response.json()) as BacktestJob;
}

export async function backtestJob(jobId: string): Promise<BacktestJob> {
  return (await (await call(`/backtests/jobs/${jobId}`)).json()) as BacktestJob;
}

/** The stored runs, newest first, with their metrics. */
export async function backtests(limit?: number): Promise<RunSummary[]> {
  const query = limit === undefined ? "" : `?limit=${limit}`;
  return (await (await call(`/backtests${query}`)).json()) as RunSummary[];
}

export async function backtest(runId: number): Promise<RunDetail> {
  return (await (await call(`/backtests/${runId}`)).json()) as RunDetail;
}

/** What's stored of a series, so the run form can say how far back it goes. */
export async function candleRange(symbol: string, timeframe: string): Promise<CandleRange> {
  const query = new URLSearchParams({ symbol, timeframe });
  return (await (await call(`/candles/range?${query.toString()}`)).json()) as CandleRange;
}

/** A page of History (D49), newest first; `before` is the last id shown. */
export async function historyOrders(query: URLSearchParams, before?: number): Promise<OrderRow[]> {
  return (await (await call(`/history/orders?${paged(query, before)}`)).json()) as OrderRow[];
}

export async function historyFills(query: URLSearchParams, before?: number): Promise<FillRow[]> {
  return (await (await call(`/history/fills?${paged(query, before)}`)).json()) as FillRow[];
}

function paged(query: URLSearchParams, before?: number | string): string {
  const page = new URLSearchParams(query);
  page.set("limit", String(PAGE_ROWS));
  if (before !== undefined) page.set("before", String(before));
  return page.toString();
}

/** The services' log lines (D49), newest first; `before` is the last page's `next`. */
export async function logs(query: URLSearchParams, before?: string): Promise<LogPage> {
  return (await (await call(`/logs?${paged(query, before)}`)).json()) as LogPage;
}

/** Buys the risk layer blocked or shrank (D41, D42), newest first. */
export async function riskEvents(query: URLSearchParams, before?: number): Promise<RiskEvent[]> {
  return (await (await call(`/risk-events?${paged(query, before)}`)).json()) as RiskEvent[];
}

/** Both modes' risk limits and circuit breakers (D41, D42), paper first. */
export async function riskSettings(): Promise<StoredRiskSettings[]> {
  return (await (await call("/risk-settings")).json()) as StoredRiskSettings[];
}

/**
 * Save a mode's settings (D50). Loosening any of them needs a TOTP `code` (UX4): without
 * a right one it throws an ApiError, 403 (or 429 while locked), whose body `codeNeeded`
 * reads.
 */
export async function saveRiskSettings(
  mode: Mode,
  settings: RiskSettings,
  code?: string,
): Promise<StoredRiskSettings> {
  const response = await call(`/risk-settings/${mode}`, {
    method: "PUT",
    body: JSON.stringify({ settings, code }),
  });
  return (await response.json()) as StoredRiskSettings;
}

/** The stored exchange accounts: never their keys, only the API key's last 4 characters. */
export async function exchangeAccounts(): Promise<ExchangeAccount[]> {
  return (await (await call("/exchange-accounts")).json()) as ExchangeAccount[];
}

/** Store an exchange's keys, encrypted (D50). Needs a TOTP code, as `saveRiskSettings`. */
export async function addExchangeAccount(account: NewAccount): Promise<ExchangeAccount> {
  const response = await call("/exchange-accounts", {
    method: "POST",
    body: JSON.stringify(account),
  });
  return (await response.json()) as ExchangeAccount;
}

export async function replaceExchangeKeys(id: number, keys: NewKeys): Promise<ExchangeAccount> {
  const response = await call(`/exchange-accounts/${id}`, {
    method: "PUT",
    body: JSON.stringify(keys),
  });
  return (await response.json()) as ExchangeAccount;
}

export async function deleteExchangeAccount(id: number): Promise<void> {
  await call(`/exchange-accounts/${id}`, { method: "DELETE" });
}

export async function botCommand(
  botId: number,
  action: "start" | "pause" | "stop",
  closePosition = false,
): Promise<BotReply> {
  const response = await call(`/bots/${botId}/${action}`, {
    method: "POST",
    body: JSON.stringify(action === "stop" ? { close_position: closePosition } : {}),
  });
  return (await response.json()) as BotReply;
}
