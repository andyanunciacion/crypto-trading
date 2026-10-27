/**
 * Calls to the API, which the browser reaches at /api on the dashboard's own origin
 * (next.config.ts rewrites it to the API service). Same origin means the httpOnly
 * session cookie goes along by itself, and no CORS is needed (D44).
 */

import type { BotReply, BotSetup, Catalog, NewBot } from "@/lib/bots";
import type { Chart, TradeExplanation } from "@/lib/market";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
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
    throw new ApiError(response.status, detailOf(body) ?? `HTTP ${response.status}`);
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

/**
 * The Bots page's commands, carried out by the engine (D47). Throws an ApiError: 422
 * with the engine's reason when it refuses, 504 when it didn't answer in time (the
 * command is then dropped).
 */
export async function createBot(bot: NewBot): Promise<BotReply> {
  const response = await call("/bots", { method: "POST", body: JSON.stringify(bot) });
  return (await response.json()) as BotReply;
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
