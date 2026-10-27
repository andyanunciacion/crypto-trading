/**
 * The Bots page (D47): what a new bot can be set up with (the engine's catalog), each
 * bot's setup, and the engine's replies. Mirrors `shared/catalog.py`,
 * `backend/bots/store.py` and `shared/commands.py`.
 */
import type { Decimal } from "@/lib/format";
import type { BotStatus, Mode } from "@/lib/overview";

export type FieldType = "integer" | "number" | "decimal";

/** One setting, read by the engine from its pydantic model. Numbers are strings. */
export type FieldSpec = {
  name: string;
  /** "decimal" is money-like (sizing, stops) and goes back as a string, never a float. */
  type: FieldType;
  default: string | null; // null: required
  minimum: string | null;
  exclusive_minimum: boolean;
  maximum: string | null;
  exclusive_maximum: boolean;
  description: string | null;
};

/** A strategy, a sizing mode or a stop kind, with its settings. */
export type Choice = {
  name: string;
  title: string;
  version: number | null;
  fields: FieldSpec[];
};

export type Catalog = {
  strategies: Choice[];
  sizing: Choice[];
  stops: Record<string, Choice[]>; // by slot: stop_loss, take_profit, trailing
  stops_fields: FieldSpec[]; // e.g. atr_period
  timeframes: string[];
  written_at: string;
};

export type BotSetup = {
  id: number;
  name: string;
  created_at: string;
  exchange: string;
  symbol: string;
  timeframe: string;
  mode: Mode;
  status: BotStatus;
  strategy: string | null;
  strategy_version: number | null;
  params: Record<string, unknown> | null;
  sizing: Record<string, unknown>;
  stops: Record<string, unknown>;
  currency: string;
  cash: Decimal;
  paused_reason: string | null;
  paused_at: string | null;
  stopped_reason: string | null;
  stopped_at: string | null;
};

/** The engine's answer to a Bots page command (`BotReply`). */
export type BotReply = {
  id: string;
  ok: boolean;
  error: string | null;
  bot_id: number | null;
  status: BotStatus | null;
  canceled: string[];
  closing: {
    bot_id: number;
    symbol: string;
    amount: Decimal;
    client_order_id: string | null;
    status: string | null;
    filled: Decimal | null;
  }[];
  not_closed: { bot_id: number; symbol: string; reason: string }[];
  took_ms: number;
};

export type Settings = Record<string, number | string>;

export type NewBot = {
  name: string;
  strategy: string;
  params: Settings;
  symbol: string;
  timeframe: string;
  cash: Decimal;
  sizing: Settings;
  stops: Record<string, Settings | number>;
};

export const STOP_SLOTS = [
  {
    key: "stop_loss",
    label: "Stop-loss",
    hint: "sells if the price falls this far below the entry",
  },
  {
    key: "take_profit",
    label: "Take-profit",
    hint: "sells once the price rises this far above it",
  },
  {
    key: "trailing",
    label: "Trailing stop",
    hint: "follows the highest price since the entry, this far below",
  },
] as const;

/** What the inputs start with: each setting's default, or empty if it has none. */
export function initialValues(fields: FieldSpec[]): Record<string, string> {
  return Object.fromEntries(fields.map((f) => [f.name, f.default ?? ""]));
}

const INTEGER = /^-?\d+$/;
const DECIMAL = /^-?(\d+\.?\d*|\.\d+)$/;

/**
 * What's wrong with `text` for `spec`, or null. Only a hint before sending: the engine
 * checks every value again, exactly (the bounds here are compared as JS numbers).
 */
export function fieldProblem(spec: FieldSpec, text: string): string | null {
  const value = text.trim();
  if (value === "")
    return spec.default === null ? "required" : `required (the default is ${spec.default})`;
  if (!(spec.type === "integer" ? INTEGER : DECIMAL).test(value)) {
    return spec.type === "integer" ? "a whole number" : "a number";
  }
  const n = Number(value);
  if (spec.minimum !== null) {
    const min = Number(spec.minimum);
    if (spec.exclusive_minimum ? n <= min : n < min) {
      return `${spec.exclusive_minimum ? "above" : "at least"} ${spec.minimum}`;
    }
  }
  if (spec.maximum !== null) {
    const max = Number(spec.maximum);
    if (spec.exclusive_maximum ? n >= max : n > max) {
      return `${spec.exclusive_maximum ? "below" : "at most"} ${spec.maximum}`;
    }
  }
  return null;
}

/** The bounds as a hint, e.g. "≥ 2", "> 0 and < 100". */
export function boundsHint(spec: FieldSpec): string | null {
  const parts: string[] = [];
  if (spec.minimum !== null) parts.push(`${spec.exclusive_minimum ? ">" : "≥"} ${spec.minimum}`);
  if (spec.maximum !== null) parts.push(`${spec.exclusive_maximum ? "<" : "≤"} ${spec.maximum}`);
  return parts.length === 0 ? null : parts.join(" and ");
}

/**
 * The values as the engine takes them: whole numbers and indicator settings as JSON
 * numbers (indicator math is float anyway), decimals as strings, so a money-like
 * setting keeps every digit (D9).
 */
export function toSettings(fields: FieldSpec[], values: Record<string, string>): Settings {
  return Object.fromEntries(
    fields.map((f) => {
      const value = (values[f.name] ?? "").trim();
      return [f.name, f.type === "decimal" ? value : Number(value)];
    }),
  );
}

function settingsText(settings: Record<string, unknown>, skip: string): string {
  return Object.entries(settings)
    .filter(([key, value]) => key !== skip && value !== null)
    .map(([key, value]) => `${key} ${String(value)}`)
    .join(", ");
}

/** "percent_of_equity: percent 25", or "all_in". */
export function describeSizing(sizing: Record<string, unknown>): string {
  const mode = String(sizing.mode ?? "all_in");
  const rest = settingsText(sizing, "mode");
  return rest ? `${mode}: ${rest}` : mode;
}

/** A stop's distance: "3%", "2 × ATR" or "2R". */
function distance(stop: Record<string, unknown>): string {
  if (stop.kind === "percent") return `${String(stop.percent)}%`;
  if (stop.kind === "atr") return `${String(stop.multiple)} × ATR`;
  if (stop.kind === "r") return `${String(stop.multiple)}R`;
  return settingsText(stop, "");
}

/** "stop-loss 3%; trailing stop 2 × ATR", or "none". */
export function describeStops(stops: Record<string, unknown>): string {
  const parts = STOP_SLOTS.flatMap(({ key, label }) => {
    const stop = stops[key];
    if (typeof stop !== "object" || stop === null) return [];
    return [`${label.toLowerCase()} ${distance(stop as Record<string, unknown>)}`];
  });
  return parts.length === 0 ? "none" : parts.join("; ");
}

/** "fast 9, slow 21". */
export function describeParams(params: Record<string, unknown> | null): string {
  return params === null ? "—" : settingsText(params, "") || "defaults";
}
