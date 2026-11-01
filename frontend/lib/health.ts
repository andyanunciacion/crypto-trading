/**
 * Settings → System (P7-4, D62): every service's state, as `GET /health/services` gives it
 * (backend/health/report.py).
 */

/**
 * ok: alive. stale: its heartbeat is too old (down or stuck). down: didn't answer.
 * unknown: no heartbeat to judge (never ran, or Redis is down). off: not set up.
 */
export type ServiceState = "ok" | "stale" | "down" | "unknown" | "off";

export type ServiceHealth = {
  name: string;
  label: string;
  state: ServiceState;
  last_seen: string | null; // its latest heartbeat
  age_s: number | null; // how old that was when checked, by the API's clock
  since: string | null; // when it started, if it says
  detail: string | null;
};

export type ServicesHealth = {
  checked_at: string;
  services: ServiceHealth[];
};

/** How often the System tab asks again. */
export const HEALTH_REFRESH_MS = 5000;
