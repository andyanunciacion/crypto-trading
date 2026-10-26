/**
 * How numbers and times are shown (DESIGN UX4, UX6, UX7).
 *
 * Money and quantities arrive from the API as strings, never JSON numbers, so they keep
 * every digit (D9). They're formatted straight from the string: `Intl.NumberFormat`
 * reads a numeric string as an exact decimal, so nothing passes through a float.
 */

/** A decimal number as the API sends it, e.g. "3050.000". */
export type Decimal = string;

export const MANILA = "Asia/Manila";

type DecimalStyle = {
  /** Digits after the point: at least `min`, at most `max` (rounded half-even). */
  min?: number;
  max?: number;
  /** Always show the sign: "+12.30", "-4.00" (zero has none). */
  sign?: boolean;
};

export function formatDecimal(value: Decimal, { min = 2, max = 2, sign = false }: DecimalStyle) {
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: min,
    maximumFractionDigits: max,
    roundingMode: "halfEven",
    signDisplay: sign ? "exceptZero" : "auto",
  }).format(value as Intl.StringNumericLiteral);
}

/** Whether a decimal string is below / above zero, without parsing it as a float. */
export function signOf(value: Decimal): -1 | 0 | 1 {
  if (value.trim().startsWith("-")) return /[1-9]/.test(value) ? -1 : 0;
  return /[1-9]/.test(value) ? 1 : 0;
}

/** "12 s", "4 min", "3 h", "2 d": how long ago, for freshness (UX5). */
export function formatAge(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} d`;
}

const manilaTime = new Intl.DateTimeFormat("en-GB", {
  timeZone: MANILA,
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/** A UTC timestamp in Manila time, e.g. "26 Oct, 20:00" (UX7). */
export function formatManila(iso: string): string {
  return manilaTime.format(new Date(iso));
}

/** The same instant in UTC, for hovering, e.g. "2026-10-26 12:00:05 UTC". */
export function formatUtc(iso: string): string {
  return `${new Date(iso).toISOString().replace("T", " ").slice(0, 19)} UTC`;
}
