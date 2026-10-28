"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";

import { CodeDialog } from "@/components/code-dialog";
import { Filter } from "@/components/page-tabs";
import { Timestamp } from "@/components/timestamp";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { ApiError, riskSettings, saveRiskSettings } from "@/lib/api";
import {
  BREAKERS,
  LIMITS,
  SETTINGS,
  codeNeeded,
  showSetting,
  type FormRow,
  type Loosened,
  type Mode,
  type RiskSettings,
  type SettingName,
  type StoredRiskSettings,
} from "@/lib/settings";

/** The form's values: strings as typed, null for a setting that's off. */
type Draft = Record<SettingName, string | null>;
type Message = { kind: "status" | "alert"; text: string };

function draftOf(settings: RiskSettings): Draft {
  const entries = Object.entries(settings).map(([name, value]) => [
    name,
    value === null ? null : String(value),
  ]);
  return Object.fromEntries(entries) as Draft;
}

/** A hint only: the API checks every bound exactly. */
function problemOf(name: SettingName, value: string | null): string | null {
  if (value === null) return null;
  if (value === "") return "required (or turn it off)";
  if (SETTINGS[name].whole ? !/^\d+$/.test(value) : !/^\d+(\.\d+)?$/.test(value)) {
    return SETTINGS[name].whole ? "a whole number" : "a number, like 2.5";
  }
  return null;
}

/**
 * The risk limits and circuit breakers of a mode (D41, D42), saved by the API (D50).
 * Each can be turned off (Off). Loosening any of them (a higher limit, a shorter window,
 * one turned off) asks for a TOTP code (UX4): the API says what the edit loosens, and
 * the dialog lists it. A save applies from the next buy, with no restart.
 */
export function RiskLimitsForm() {
  const [stored, setStored] = useState<StoredRiskSettings[] | null>(null);
  const [mode, setMode] = useState<Mode>("paper");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [message, setMessage] = useState<Message | null>(null);
  const [asking, setAsking] = useState<Loosened[] | null>(null);
  const [codeError, setCodeError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const messageRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    let current = true;
    riskSettings()
      .then((loaded) => {
        if (current) setStored(loaded);
      })
      .catch((error: unknown) => {
        if (current) setMessage({ kind: "alert", text: problemText(error) });
      });
    return () => {
      current = false;
    };
  }, []);

  const row = stored?.find((s) => s.mode === mode) ?? null;
  // Until edited, the form shows what's stored.
  const values = draft ?? (row === null ? null : draftOf(row.settings));
  useEffect(() => {
    if (message !== null) messageRef.current?.focus();
  }, [message]);

  if (stored === null || row === null || values === null) {
    return message ? (
      <p role="alert" className="text-loss text-sm">
        {message.text}
      </p>
    ) : (
      <p className="text-muted-foreground text-sm">Loading…</p>
    );
  }

  const problems = (Object.keys(values) as SettingName[]).filter(
    (name) => problemOf(name, values[name]) !== null,
  );
  const changed = (Object.keys(values) as SettingName[]).some(
    (name) => values[name] !== draftOf(row.settings)[name],
  );

  function set(name: SettingName, value: string) {
    setMessage(null);
    setDraft({ ...(values as Draft), [name]: value });
  }

  function setOff(names: SettingName[], off: boolean) {
    setMessage(null);
    const next = { ...(values as Draft) };
    for (const name of names) {
      // Turned on again: back to what's stored, or an empty field to fill in.
      const was = row?.settings[name];
      next[name] = off ? null : was === null || was === undefined ? "" : String(was);
    }
    setDraft(next);
  }

  async function save(code?: string) {
    if (values === null) return;
    setSaving(true);
    try {
      // Strings as typed: the API reads them exactly (Decimal), never through a float.
      const saved = await saveRiskSettings(mode, values, code);
      setStored((old) => (old ?? []).map((s) => (s.mode === saved.mode ? saved : s)));
      setDraft(null);
      setAsking(null);
      setCodeError(null);
      setMessage({ kind: "status", text: "Saved. It applies from the next buy." });
    } catch (error) {
      const needed = codeNeeded(error);
      if (needed !== null && code === undefined && needed.status === 403) {
        setAsking(needed.loosened);
      } else if (needed !== null) {
        setCodeError(needed.detail);
      } else {
        setAsking(null);
        setMessage({ kind: "alert", text: problemText(error) });
      }
    } finally {
      setSaving(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (changed && problems.length === 0 && !saving) void save();
  }

  function onMode(next: Mode) {
    setMode(next);
    setDraft(null);
    setMessage(null);
  }

  return (
    <>
      <form onSubmit={submit} className="flex max-w-3xl flex-col gap-4" aria-label="Risk limits">
        <div className="flex flex-wrap items-end gap-3">
          <Filter label="Mode">
            <NativeSelect value={mode} onChange={(e) => onMode(e.target.value as Mode)}>
              <option value="paper">Paper</option>
              <option value="live">Live (from P9)</option>
            </NativeSelect>
          </Filter>
          <p className="text-muted-foreground text-xs">
            Account-wide: they count every {mode} bot. Saved <Timestamp iso={row.updated_at} />.
          </p>
        </div>

        <Section title="Limits" rows={LIMITS} values={values} onSet={set} onOff={setOff} />
        <Section
          title="Circuit breakers"
          rows={BREAKERS}
          values={values}
          onSet={set}
          onOff={setOff}
        />

        <p className="text-muted-foreground text-xs">
          Lowering a limit saves at once; raising one, a shorter window or turning one off asks for
          a code from your authenticator app. Sells and stops are never blocked by any of these.
        </p>
        <div className="flex items-center gap-3">
          <Button type="submit" disabled={!changed || problems.length > 0 || saving}>
            {saving && asking === null ? "Saving…" : "Save"}
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={!changed || saving}
            onClick={() => setDraft(null)}
          >
            Undo changes
          </Button>
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
      </form>

      {/* Outside the form: React events bubble through portals, so the dialog's own
          submit would otherwise submit this form too (a second save, without the code). */}
      <CodeDialog
        open={asking !== null}
        title="Loosen the risk limits?"
        confirm="Save"
        busy={saving}
        error={codeError}
        onSubmit={(code) => void save(code)}
        onCancel={() => {
          setAsking(null);
          setCodeError(null);
        }}
      >
        <p>This lets the bots take more risk:</p>
        <ul className="mt-1 list-disc pl-5">
          {(asking ?? []).map((change) => (
            <li key={change.field}>
              {SETTINGS[change.field].label}: {showSetting(change.field, change.old)} →{" "}
              <strong>{showSetting(change.field, change.new)}</strong>
            </li>
          ))}
        </ul>
      </CodeDialog>
    </>
  );
}

function problemText(error: unknown): string {
  if (!(error instanceof ApiError)) return "Couldn't reach the API.";
  return error.status === 422 ? `Refused: ${error.message}` : error.message;
}

function Section({
  title,
  rows,
  values,
  onSet,
  onOff,
}: {
  title: string;
  rows: FormRow[];
  values: Draft;
  onSet: (name: SettingName, value: string) => void;
  onOff: (names: SettingName[], off: boolean) => void;
}) {
  return (
    <Card className="gap-3 py-4">
      <CardHeader className="px-4">
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col divide-y px-4">
        {rows.map((row) => {
          const names = row.kind === "one" ? [row.name] : row.names;
          const off = names.every((name) => values[name] === null);
          const label = row.kind === "one" ? SETTINGS[row.name].label : row.label;
          const id = names[0];
          return (
            // A labelled group, not a fieldset: a legend names a fieldset only as its first
            // child, which this layout can't keep.
            <div
              key={id}
              role="group"
              aria-labelledby={`setting-${id}`}
              className="flex items-start gap-3 py-3 first:pt-0 last:pb-0"
            >
              <div className="flex flex-1 flex-col gap-1">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span id={`setting-${id}`} className="min-w-44 text-sm font-medium">
                    {label}
                  </span>
                  {off ? (
                    <span className="text-muted-foreground text-sm">Off</span>
                  ) : row.kind === "one" ? (
                    <span className="flex items-center gap-2 text-sm">
                      <Value name={row.name} label={label} value={values[row.name]} onSet={onSet} />
                      <span className="text-muted-foreground">{SETTINGS[row.name].unit}</span>
                    </span>
                  ) : (
                    <span className="flex flex-wrap items-center gap-2 text-sm">
                      {row.words[0]}
                      <Value
                        name={row.names[0]}
                        label={SETTINGS[row.names[0]].label}
                        value={values[row.names[0]]}
                        onSet={onSet}
                      />
                      {row.words[1]}
                      <Value
                        name={row.names[1]}
                        label={SETTINGS[row.names[1]].label}
                        value={values[row.names[1]]}
                        onSet={onSet}
                      />
                      {row.words[2]}
                    </span>
                  )}
                </div>
                <p className="text-muted-foreground text-xs">{row.hint}</p>
              </div>
              {/* Its own column, so a long sentence never pushes it onto a line of its own. */}
              <label className="flex shrink-0 items-center gap-1.5 pt-1.5 text-sm">
                <input
                  type="checkbox"
                  aria-label={`${label}: off`}
                  checked={off}
                  onChange={(e) => onOff(names, e.target.checked)}
                  className="accent-primary size-4"
                />
                Off
              </label>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

function Value({
  name,
  label,
  value,
  onSet,
}: {
  name: SettingName;
  label: string;
  value: string | null;
  onSet: (name: SettingName, value: string) => void;
}) {
  const problem = problemOf(name, value);
  return (
    <span className="flex flex-col">
      <Input
        aria-label={label}
        name={name}
        inputMode={SETTINGS[name].whole ? "numeric" : "decimal"}
        value={value ?? ""}
        onChange={(e) => onSet(name, e.target.value.trim())}
        aria-invalid={problem !== null}
        className="h-8 w-20 text-right font-mono tabular-nums"
      />
      {problem && <span className="text-loss text-xs">{problem}</span>}
    </span>
  );
}
