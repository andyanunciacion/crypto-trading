"use client";

import { useEffect, useRef, useState } from "react";

import { Timestamp } from "@/components/timestamp";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, alertSettings, saveAlertSetting } from "@/lib/api";
import type { AlertEvent, AlertSetting, AlertsView } from "@/lib/alerts";

type Message = { kind: "status" | "alert"; text: string };

/**
 * Which alerts the alerts service sends to Telegram (D59): one box per event, saved as
 * soon as it's ticked or cleared, with no code (an alert changes nothing about trading).
 * Above them, whether Telegram is set up and the service is running.
 */
export function AlertSettingsForm() {
  const [view, setView] = useState<AlertsView | null>(null);
  const [saving, setSaving] = useState<AlertEvent | null>(null);
  const [message, setMessage] = useState<Message | null>(null);
  const messageRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    let current = true;
    alertSettings()
      .then((loaded) => {
        if (current) setView(loaded);
      })
      .catch((error: unknown) => {
        if (current) setMessage({ kind: "alert", text: problemText(error) });
      });
    return () => {
      current = false;
    };
  }, []);

  useEffect(() => {
    if (message?.kind === "alert") messageRef.current?.focus();
  }, [message]);

  if (view === null) {
    return message ? (
      <p role="alert" className="text-loss text-sm">
        {message.text}
      </p>
    ) : (
      <p className="text-muted-foreground text-sm">Loading…</p>
    );
  }

  function show(event: AlertEvent, change: Partial<AlertSetting>) {
    setView((old) =>
      old === null
        ? old
        : {
            ...old,
            settings: old.settings.map((s) => (s.event === event ? { ...s, ...change } : s)),
          },
    );
  }

  async function toggle(event: AlertEvent, enabled: boolean) {
    setSaving(event);
    setMessage(null);
    show(event, { enabled }); // ticked at once; put back if the save fails
    try {
      const saved = await saveAlertSetting(event, enabled);
      show(event, saved);
      setMessage({
        kind: "status",
        text: `${saved.label}: ${saved.enabled ? "on" : "off"}. It applies within a second.`,
      });
    } catch (error) {
      show(event, { enabled: !enabled });
      setMessage({ kind: "alert", text: problemText(error) });
    } finally {
      setSaving(null);
    }
  }

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <ServiceStatus view={view} />
      <Card className="gap-3 py-4">
        <CardHeader className="px-4">
          <CardTitle className="text-base">Send to Telegram</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col divide-y px-4">
          {view.settings.map((setting) => {
            const id = `alert-${setting.event}`;
            return (
              <label
                key={setting.event}
                htmlFor={id}
                className="flex items-start gap-3 py-3 first:pt-0 last:pb-0"
              >
                <input
                  id={id}
                  type="checkbox"
                  checked={setting.enabled}
                  disabled={saving !== null}
                  aria-labelledby={`${id}-label`}
                  aria-describedby={`${id}-hint`}
                  onChange={(e) => void toggle(setting.event, e.target.checked)}
                  className="accent-primary mt-0.5 size-4 shrink-0"
                />
                <span className="flex flex-col gap-1">
                  <span id={`${id}-label`} className="text-sm font-medium">
                    {setting.label}
                  </span>
                  <span id={`${id}-hint`} className="text-muted-foreground text-xs">
                    {setting.description}
                  </span>
                </span>
              </label>
            );
          })}
        </CardContent>
      </Card>
      {message && (
        <p
          ref={messageRef}
          tabIndex={-1}
          role={message.kind}
          className={message.kind === "alert" ? "text-loss text-sm" : "text-sm"}
        >
          {message.text}
        </p>
      )}
    </div>
  );
}

function ServiceStatus({ view }: { view: AlertsView }) {
  const status = view.status;
  if (status === null || view.stale) {
    return (
      <p role="status" className="text-loss text-sm">
        The alerts service isn&apos;t running
        {status !== null && (
          <>
            {" "}
            (last seen <Timestamp iso={status.ts} />)
          </>
        )}
        : nothing is sent. Start it with <code>docker compose up -d alerts</code>.
      </p>
    );
  }
  if (!status.configured) {
    return (
      <p role="status" className="text-sm">
        Telegram isn&apos;t set up: alerts are only written to the log. Set{" "}
        <code>TELEGRAM_BOT_TOKEN</code> and <code>TELEGRAM_CHAT_ID</code> in <code>.env</code> (see{" "}
        <code>.env.example</code>), then restart the alerts service.
      </p>
    );
  }
  return (
    <p role="status" className={status.last_error ? "text-loss text-sm" : "text-sm"}>
      Telegram is set up.{" "}
      {status.last_sent_at ? (
        <>
          Last message <Timestamp iso={status.last_sent_at} />.
        </>
      ) : (
        "No message sent yet."
      )}
      {status.last_error && <> The last one didn&apos;t go: {status.last_error}</>}
    </p>
  );
}

function problemText(error: unknown): string {
  if (!(error instanceof ApiError)) return "Couldn't reach the API.";
  return error.status === 422 ? `Refused: ${error.message}` : error.message;
}
