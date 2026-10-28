/**
 * The Settings page's data (D50): the risk limits and circuit breakers of each mode
 * (`risk_settings`, D41/D42), and the stored exchange accounts.
 */

import { ApiError } from "@/lib/api";

export type Mode = "paper" | "live";

export type SettingName =
  | "max_open_positions"
  | "max_coin_exposure_pct"
  | "max_daily_loss_pct"
  | "max_drawdown_pct"
  | "stale_after_candles"
  | "max_failed_orders"
  | "failed_orders_minutes"
  | "spike_pct"
  | "spike_minutes";

/** Whole numbers come as JSON numbers, percents as strings (Decimal, D9); null is off. */
export type SettingValue = number | string | null;
export type RiskSettings = Record<SettingName, SettingValue>;

export type StoredRiskSettings = { mode: Mode; settings: RiskSettings; updated_at: string };

/** A setting an edit loosens: a higher limit, a shorter window, or one turned off. */
export type Loosened = { field: SettingName; old: SettingValue; new: SettingValue };

/** A refusal for want of a right TOTP code: 403 (none, or wrong) or 429 (locked). */
export type CodeNeeded = { status: number; detail: string; loosened: Loosened[] };

export function codeNeeded(error: unknown): CodeNeeded | null {
  if (!(error instanceof ApiError)) return null;
  const body = error.body;
  if (typeof body !== "object" || body === null || !("needs_code" in body)) return null;
  const loosened = "loosened" in body && Array.isArray(body.loosened) ? body.loosened : [];
  return { status: error.status, detail: error.message, loosened: loosened as Loosened[] };
}

type Setting = { label: string; unit: string; whole: boolean };

export const SETTINGS: Record<SettingName, Setting> = {
  max_open_positions: { label: "Max open positions", unit: "positions", whole: true },
  max_coin_exposure_pct: { label: "Max exposure per coin", unit: "% of equity", whole: false },
  max_daily_loss_pct: { label: "Max daily loss", unit: "% of midnight's equity", whole: false },
  max_drawdown_pct: { label: "Max drawdown", unit: "% below the peak", whole: false },
  stale_after_candles: { label: "Stale data", unit: "candles", whole: true },
  max_failed_orders: { label: "Failed orders", unit: "orders", whole: true },
  failed_orders_minutes: { label: "Failed orders' window", unit: "min", whole: true },
  spike_pct: { label: "Price spike", unit: "%", whole: false },
  spike_minutes: { label: "Price spike's window", unit: "min", whole: true },
};

/** One row of the form: one setting, or a breaker's pair, which is on or off together. */
export type FormRow =
  | { kind: "one"; name: SettingName; hint: string }
  | {
      kind: "pair";
      label: string;
      names: [SettingName, SettingName];
      words: [string, string, string]; // around the two inputs
      hint: string;
    };

export const LIMITS: FormRow[] = [
  {
    kind: "one",
    name: "max_drawdown_pct",
    hint: "No buys once equity is this far below the most it was ever worth.",
  },
  {
    kind: "one",
    name: "max_daily_loss_pct",
    hint: "No buys for the rest of the Manila day once today's loss reaches this.",
  },
  {
    kind: "one",
    name: "max_open_positions",
    hint: "Open positions, plus buys still opening one, across every bot.",
  },
  {
    kind: "one",
    name: "max_coin_exposure_pct",
    hint: "A buy that would pass this is shrunk to fit.",
  },
];

export const BREAKERS: FormRow[] = [
  {
    kind: "one",
    name: "stale_after_candles",
    hint: "No buys while a price they rely on is older than this many of the bot's candles.",
  },
  {
    kind: "pair",
    label: "Error breaker",
    names: ["max_failed_orders", "failed_orders_minutes"],
    words: ["Pause a bot after", "failed orders within", "min"],
    hint: "Until you start it again from the Bots page.",
  },
  {
    kind: "pair",
    label: "Price spike",
    names: ["spike_pct", "spike_minutes"],
    words: ["No buys of a coin that swung", "% or more within", "min"],
    hint: "Clears by itself once the swing leaves the window.",
  },
];

export function showSetting(name: SettingName, value: SettingValue): string {
  if (value === null) return "off";
  const { unit } = SETTINGS[name];
  return unit.startsWith("%") ? `${value}${unit}` : `${value} ${unit}`;
}

export type Exchange = "mexc" | "binance";

export const EXCHANGE_NAMES: Record<Exchange, string> = {
  mexc: "MEXC",
  binance: "Binance",
};

/** What the API shows of a stored account: never its key or secret. */
export type ExchangeAccount = {
  id: number;
  exchange: Exchange;
  label: string;
  sandbox: boolean;
  key_hint: string; // the API key's last 4 characters
  created_at: string;
  updated_at: string;
};

export type NewKeys = { api_key: string; api_secret: string; code: string };
export type NewAccount = NewKeys & { exchange: Exchange; label: string; sandbox: boolean };
