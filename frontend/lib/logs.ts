/**
 * The Logs page (P5-6, D49): the services' mirrored log lines (`GET /api/logs`) and the
 * risk events (`GET /api/risk-events`). Mirrors `backend/logs/store.py` and
 * `shared/logs.py`.
 */

export type LogEntry = {
  id: string; // the Redis stream id, which orders and pages them
  ts: string | null;
  service: string; // engine, backtester or api
  level: string; // info, warning, error, critical
  event: string; // e.g. "risk.decision"
  bot_id: number | null;
  fields: Record<string, unknown>; // everything else on the line
};

export type LogPage = {
  entries: LogEntry[]; // newest first
  next: string | null; // `before` for the next page; null at the oldest line kept
};

export type RiskEvent = {
  id: number;
  ts: string;
  mode: string;
  bot_id: number;
  bot_name: string;
  client_order_id: string;
  rule: string;
  decision: string; // blocked or resized
  detail: string;
  inputs: Record<string, unknown>; // the order, the settings and the account numbers
};

/** How often the newest page of log lines is read again. */
export const REFRESH_SECONDS = 5;

/** What the "Show" picker offers, as the API's level and event prefixes. */
export const LOG_VIEWS: Record<string, { label: string; level?: string; events?: string[] }> = {
  all: { label: "Everything" },
  decisions: {
    label: "Decisions",
    events: [
      "strategy.signal",
      "risk.",
      "bot.order",
      "bot.exit",
      "bot.switch",
      "bot.keeper_done",
      "bot_command.",
      "kill_switch.",
    ],
  },
  warnings: { label: "Warnings and errors", level: "warning" },
  errors: { label: "Errors", level: "error" },
};

export const SERVICES = ["engine", "backtester", "api"] as const;
export const RISK_DECISIONS = ["blocked", "resized"] as const;

/** The API's query for the "Show" view, an event prefix (which replaces the view's
 * prefixes), a bot and a service; "" means any. */
export function logQuery(view: string, event: string, bot: string, service: string) {
  const query = new URLSearchParams();
  const chosen = LOG_VIEWS[view] ?? LOG_VIEWS.all;
  if (chosen.level) query.set("level", chosen.level);
  const prefixes = event.trim() ? [event.trim()] : (chosen.events ?? []);
  for (const prefix of prefixes) query.append("event", prefix);
  if (bot) query.set("bot_id", bot);
  if (service) query.set("service", service);
  return query;
}

/** A line's other fields, short: `side=buy amount=0.5 reason=…`. */
export function summary(fields: Record<string, unknown>, max = 160): string {
  const text = Object.entries(fields)
    .map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(" ");
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
