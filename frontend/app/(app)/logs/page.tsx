"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { useFeed } from "@/components/overview-provider";
import { Filter, PageTabs } from "@/components/page-tabs";
import { Timestamp } from "@/components/timestamp";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { problemText, usePages } from "@/components/use-pages";
import { logs as fetchLogs, riskEvents } from "@/lib/api";
import { manilaMidnight } from "@/lib/history";
import {
  LOG_VIEWS,
  REFRESH_SECONDS,
  RISK_DECISIONS,
  SERVICES,
  logQuery,
  summary,
  type LogEntry,
  type RiskEvent,
} from "@/lib/logs";
import { cn } from "@/lib/utils";

const TABS = [
  { id: "engine", label: "Engine log" },
  { id: "risk", label: "Risk events" },
];

/** A level as a word, colored too for warnings and errors (never color alone). */
function Level({ level }: { level: string }) {
  const tone =
    level === "error" || level === "critical"
      ? "text-loss font-semibold"
      : level === "warning"
        ? "text-stale font-semibold"
        : "text-muted-foreground";
  return <span className={cn("font-mono text-xs uppercase", tone)}>{level}</span>;
}

/** Everything a row holds, opened from its short summary (native, so the keyboard works). */
function Details({ short, full }: { short: string; full: unknown }) {
  return (
    <details>
      <summary className="cursor-pointer font-mono text-xs break-all">{short || "details"}</summary>
      <pre className="bg-muted mt-1 max-h-80 overflow-auto rounded p-2 text-xs whitespace-pre-wrap">
        {JSON.stringify(full, null, 2)}
      </pre>
    </details>
  );
}

/** A filter typed in, applied on Enter or on leaving the field (not on every key). Give it
 * `key={value}`, so a value changed elsewhere (a back button) starts a fresh draft. */
function TextFilter({
  label,
  value,
  placeholder,
  onApply,
}: {
  label: string;
  value: string;
  placeholder: string;
  onApply: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onApply(draft.trim());
      }}
    >
      <Filter label={label}>
        <Input
          value={draft}
          placeholder={placeholder}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={() => draft.trim() !== value && onApply(draft.trim())}
          className="font-mono"
        />
      </Filter>
    </form>
  );
}

type Lines = { key: string; entries: LogEntry[]; next: string | null; error: string | null };

/** The log lines for `query`. Give it `key={query}`: a new query starts afresh, at the
 * newest lines. */
function EngineLog({ query, botName }: { query: string; botName: (id: number) => string }) {
  const [lines, setLines] = useState<Lines | null>(null);
  const [lookingBack, setLookingBack] = useState(false); // older pages loaded: hold still
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (lookingBack) return;
    let current = true;
    fetchLogs(new URLSearchParams(query))
      .then((page) => {
        if (current) setLines({ key: query, ...page, error: null });
      })
      .catch((error: unknown) => {
        if (current) setLines({ key: query, entries: [], next: null, error: problemText(error) });
      });
    return () => {
      current = false;
    };
  }, [query, tick, lookingBack]);

  // The newest page is read again every few seconds, while it's the one shown.
  useEffect(() => {
    if (lookingBack) return;
    const timer = setInterval(() => setTick((n) => n + 1), REFRESH_SECONDS * 1000);
    return () => clearInterval(timer);
  }, [lookingBack]);

  const shown = lines?.key === query ? lines : null;

  function older() {
    if (shown?.next == null) return;
    setLookingBack(true);
    setLoadingOlder(true);
    fetchLogs(new URLSearchParams(query), shown.next)
      .then((page) =>
        setLines((before) =>
          before?.key === query
            ? { ...before, entries: [...before.entries, ...page.entries], next: page.next }
            : before,
        ),
      )
      .catch((error: unknown) =>
        setLines((before) => (before ? { ...before, error: problemText(error) } : before)),
      )
      .finally(() => setLoadingOlder(false));
  }

  if (shown === null) return <p className="text-muted-foreground text-sm">Loading…</p>;
  return (
    <>
      {shown.error && (
        <p role="alert" className="text-loss text-sm">
          {shown.error}
        </p>
      )}
      <p className="text-muted-foreground text-xs">
        {lookingBack ? (
          <>
            Holding still while you look back.{" "}
            <button
              type="button"
              className="underline underline-offset-4"
              onClick={() => setLookingBack(false)}
            >
              Back to the newest
            </button>
          </>
        ) : (
          `Newest first, read again every ${REFRESH_SECONDS} s. Info and above from the engine, the backtester and the API; the last ~50,000 lines are kept.`
        )}
      </p>
      {shown.entries.length === 0 ? (
        !shown.error && <p className="text-muted-foreground text-sm">No lines match.</p>
      ) : (
        <Card className="py-2">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Level</TableHead>
                <TableHead>Service</TableHead>
                <TableHead>Event</TableHead>
                <TableHead>Bot</TableHead>
                <TableHead>Details</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {shown.entries.map((entry) => (
                <TableRow key={entry.id} data-log={entry.event}>
                  <TableCell className="align-top">
                    {entry.ts ? <Timestamp iso={entry.ts} /> : "—"}
                  </TableCell>
                  <TableCell className="align-top">
                    <Level level={entry.level} />
                  </TableCell>
                  <TableCell className="text-muted-foreground align-top">{entry.service}</TableCell>
                  <TableCell className="align-top font-mono text-xs">{entry.event}</TableCell>
                  <TableCell className="align-top">
                    {entry.bot_id === null ? "" : botName(entry.bot_id)}
                  </TableCell>
                  <TableCell className="max-w-xl min-w-64 align-top whitespace-normal">
                    <Details short={summary(entry.fields)} full={entry} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
      {shown.next !== null && shown.entries.length > 0 && (
        <div>
          <Button variant="outline" size="sm" onClick={older} disabled={loadingOlder}>
            {loadingOlder ? "Loading…" : "Load older"}
          </Button>
        </div>
      )}
    </>
  );
}

function RiskEvents({ query }: { query: string }) {
  const { pages, older, loadingOlder } = usePages<RiskEvent>(riskEvents, query);
  if (pages === null) return <p className="text-muted-foreground text-sm">Loading…</p>;
  return (
    <>
      <p className="text-muted-foreground text-xs">
        Every buy a limit or circuit breaker blocked or shrank, with what it was checked against. A
        blocked buy never became an order, so this is the only place it shows.
      </p>
      {pages.error && (
        <p role="alert" className="text-loss text-sm">
          {pages.error}
        </p>
      )}
      {pages.rows.length === 0 ? (
        !pages.error && <p className="text-muted-foreground text-sm">No risk events match.</p>
      ) : (
        <Card className="py-2">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Bot</TableHead>
                <TableHead>Decision</TableHead>
                <TableHead>Rule</TableHead>
                <TableHead>What happened</TableHead>
                <TableHead>Inputs</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pages.rows.map((event) => (
                <TableRow key={event.id} data-risk-event={event.id}>
                  <TableCell className="align-top">
                    <Timestamp iso={event.ts} />
                  </TableCell>
                  <TableCell className="align-top">{event.bot_name}</TableCell>
                  <TableCell
                    className={cn(
                      "align-top font-semibold",
                      event.decision === "blocked" && "text-loss",
                    )}
                  >
                    {event.decision}
                  </TableCell>
                  <TableCell className="align-top font-mono text-xs">{event.rule}</TableCell>
                  <TableCell className="max-w-md min-w-48 align-top whitespace-normal">
                    {event.detail}
                    <div className="text-muted-foreground font-mono text-xs break-all">
                      {event.client_order_id}
                    </div>
                  </TableCell>
                  <TableCell className="max-w-md min-w-48 align-top whitespace-normal">
                    <Details short="the order, settings and account" full={event.inputs} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
      {pages.more && (
        <div>
          <Button variant="outline" size="sm" onClick={older} disabled={loadingOlder}>
            {loadingOlder ? "Loading…" : "Load older"}
          </Button>
        </div>
      )}
    </>
  );
}

function Logs() {
  const router = useRouter();
  const params = useSearchParams();
  const tab = params.get("tab") === "risk" ? "risk" : "engine";
  const get = (name: string) => params.get(name) ?? "";
  const bots = useFeed().overview?.bots ?? [];

  const botName = (id: number) => bots.find((bot) => bot.id === id)?.name ?? `bot ${id}`;

  function withParams(change: (next: URLSearchParams) => void): string {
    const next = new URLSearchParams(params);
    change(next);
    return `/logs?${next.toString()}`;
  }

  function set(name: string, value: string) {
    router.replace(
      withParams((next) => (value ? next.set(name, value) : next.delete(name))),
      { scroll: false },
    );
  }

  const botFilter = (
    <Filter label="Bot">
      <NativeSelect value={get("bot")} onChange={(e) => set("bot", e.target.value)}>
        <option value="">Any bot</option>
        {bots.map((bot) => (
          <option key={bot.id} value={bot.id}>
            {bot.name}
          </option>
        ))}
      </NativeSelect>
    </Filter>
  );

  let body;
  if (tab === "engine") {
    const query = logQuery(get("view"), get("event"), get("bot"), get("service")).toString();
    body = (
      <>
        <div className="flex flex-wrap items-end gap-3">
          <Filter label="Show">
            <NativeSelect
              value={get("view") || "all"}
              onChange={(e) => set("view", e.target.value)}
            >
              {Object.entries(LOG_VIEWS).map(([id, view]) => (
                <option key={id} value={id}>
                  {view.label}
                </option>
              ))}
            </NativeSelect>
          </Filter>
          <TextFilter
            key={get("event")}
            label="Event starts with"
            value={get("event")}
            placeholder="e.g. kill_switch."
            onApply={(value) => set("event", value)}
          />
          {botFilter}
          <Filter label="Service">
            <NativeSelect value={get("service")} onChange={(e) => set("service", e.target.value)}>
              <option value="">Every service</option>
              {SERVICES.map((service) => (
                <option key={service} value={service}>
                  {service}
                </option>
              ))}
            </NativeSelect>
          </Filter>
        </div>
        <EngineLog key={query} query={query} botName={botName} />
      </>
    );
  } else {
    const query = new URLSearchParams();
    if (get("bot")) query.set("bot_id", get("bot"));
    if (get("decision")) query.set("decision", get("decision"));
    if (get("rule")) query.set("rule", get("rule"));
    const start = manilaMidnight(get("from"));
    const end = manilaMidnight(get("to"), 1);
    if (start !== null) query.set("start", start);
    if (end !== null) query.set("end", end);
    body = (
      <>
        <div className="flex flex-wrap items-end gap-3">
          {botFilter}
          <Filter label="Decision">
            <NativeSelect value={get("decision")} onChange={(e) => set("decision", e.target.value)}>
              <option value="">Blocked or resized</option>
              {RISK_DECISIONS.map((decision) => (
                <option key={decision} value={decision}>
                  {decision}
                </option>
              ))}
            </NativeSelect>
          </Filter>
          <TextFilter
            key={get("rule")}
            label="Rule"
            value={get("rule")}
            placeholder="e.g. max_drawdown"
            onApply={(value) => set("rule", value)}
          />
          <Filter label="From (Manila day)">
            <Input type="date" value={get("from")} onChange={(e) => set("from", e.target.value)} />
          </Filter>
          <Filter label="To (Manila day, included)">
            <Input type="date" value={get("to")} onChange={(e) => set("to", e.target.value)} />
          </Filter>
        </div>
        <RiskEvents query={query.toString()} />
      </>
    );
  }

  return (
    <section aria-labelledby="logs" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 id="logs" className="text-xl font-semibold">
          Logs
        </h1>
        <PageTabs
          label="Log views"
          tabs={TABS}
          current={tab}
          href={(id) => withParams((next) => next.set("tab", id))}
        />
      </div>
      {body}
    </section>
  );
}

/** Logs (DESIGN §4, D49): what the services logged, and what the risk layer blocked. */
export default function LogsPage() {
  // useSearchParams needs a Suspense boundary, so the page can still prerender.
  return (
    <Suspense>
      <Logs />
    </Suspense>
  );
}
