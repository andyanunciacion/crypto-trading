/**
 * The Settings page's alert preferences (D59): which alerts the alerts service sends to
 * Telegram (`alert_settings`), and how that service is doing.
 */

export type AlertEvent =
  | "trade"
  | "stop"
  | "risk_block"
  | "risk_resize"
  | "breaker"
  | "switch"
  | "kill_switch"
  | "error"
  | "daily_summary"
  | "engine_down";

export type AlertSetting = {
  event: AlertEvent;
  label: string;
  description: string;
  enabled: boolean;
  updated_at: string;
};

/** Written by the alerts service every 10 s and after each message. */
export type AlertsStatus = {
  ts: string;
  configured: boolean; // false: no Telegram bot set up, messages are only logged
  last_sent_at: string | null;
  last_error: string | null;
};

export type AlertsView = {
  settings: AlertSetting[];
  status: AlertsStatus | null; // null: the service has never run
  stale: boolean; // over 30 s old: the service isn't running
};
