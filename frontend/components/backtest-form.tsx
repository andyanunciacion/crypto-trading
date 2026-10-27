"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";

import { StrategySetup, initialSetup, setupPayload, type Setup } from "@/components/strategy-setup";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { ApiError, backtestJob, candleRange, runBacktest } from "@/lib/api";
import type { BacktestJob, CandleRange, NewBacktest } from "@/lib/backtests";
import { fieldProblem, type Catalog, type FieldSpec } from "@/lib/bots";
import { formatAge, formatManila, shiftPoint } from "@/lib/format";

/** How often the job is asked about while it runs. */
const POLL_MS = 1000;
/** A job still queued after this long: the backtester is probably down. */
const QUEUED_WARNING_S = 10;

function spec(name: string, overrides: Partial<FieldSpec>): FieldSpec {
  return {
    name,
    type: "decimal",
    default: null,
    minimum: null,
    exclusive_minimum: false,
    maximum: null,
    exclusive_maximum: false,
    description: null,
    ...overrides,
  };
}

// The CLI's defaults (engine.backtest.run), so the same clicks give the same run.
const DAYS = spec("days", { type: "integer", default: "365", minimum: "1", maximum: "3650" });
const CASH = spec("cash", { default: "1000", minimum: "0", exclusive_minimum: true });
const FEE = spec("fee_rate", {
  default: "0.001",
  minimum: "0",
  maximum: "1",
  exclusive_maximum: true,
});
const SLIPPAGE = spec("slippage", { minimum: "0", maximum: "1", exclusive_maximum: true });

type Times = { sent: number; started: number | null }; // this browser's clock, in ms

type Field = {
  spec: FieldSpec;
  label: string;
  value: string;
  set: (v: string) => void;
  hint: string;
};

/**
 * Backtests' run form (D48): the same choices as a new bot (`StrategySetup`), plus the
 * range and costs `engine.backtest.run` takes, with its defaults. Run queues a job for
 * the backtester and follows it; once it's stored, `onDone` gets the run's id. The
 * backtester checks everything, and its reason for refusing is shown here.
 */
export function BacktestForm({
  catalog,
  onDone,
}: {
  catalog: Catalog;
  onDone: (runId: number) => void;
}) {
  const [setup, setSetup] = useState<Setup>(() => initialSetup(catalog));
  const [symbol, setSymbol] = useState("BTC/USDT");
  const [timeframe, setTimeframe] = useState(
    catalog.timeframes.includes("1h") ? "1h" : (catalog.timeframes[0] ?? ""),
  );
  const [days, setDays] = useState(DAYS.default ?? "");
  const [cash, setCash] = useState(CASH.default ?? "");
  const [fee, setFee] = useState(FEE.default ?? "");
  const [slippage, setSlippage] = useState("");
  const [stored, setStored] = useState<CandleRange | null>(null);
  const [job, setJob] = useState<BacktestJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [times, setTimes] = useState<Times>({ sent: 0, started: null });
  const [now, setNow] = useState(() => Date.now());
  const errorRef = useRef<HTMLParagraphElement>(null);

  const quote = symbol.split("/")[1] || "the quote currency";
  const fields: Field[] = [
    {
      spec: DAYS,
      label: "Days",
      value: days,
      set: setDays,
      hint: "back from the last stored candle",
    },
    { spec: CASH, label: "Starting cash", value: cash, set: setCash, hint: `in ${quote}` },
    {
      spec: FEE,
      label: "Fee rate",
      value: fee,
      set: setFee,
      hint: `a fraction of each fill${percentHint(fee)}`,
    },
    {
      spec: SLIPPAGE,
      label: "Slippage",
      value: slippage,
      set: setSlippage,
      hint: `a fraction${percentHint(slippage)}; blank: 0.0005 for BTC/ETH, 0.003 otherwise`,
    },
  ];
  const problems = fields.map((f) =>
    f.spec === SLIPPAGE && f.value.trim() === "" ? null : fieldProblem(f.spec, f.value),
  );
  const payload = setupPayload(catalog, setup);
  const running = job !== null && (job.status === "queued" || job.status === "running");
  // Refused for too little stored history: the most days that would fit, offered as a fix.
  const fits = job?.status === "failed" ? job.max_days : null;
  const valid = symbol.includes("/") && problems.every((p) => p === null) && payload !== null;

  // What's stored of the series, so the days can be chosen to fit.
  useEffect(() => {
    let current = true;
    const wanted = symbol.trim().toUpperCase();
    if (!wanted.includes("/")) return;
    const timer = setTimeout(() => {
      candleRange(wanted, timeframe)
        .then((range) => current && setStored(range))
        .catch(() => current && setStored(null));
    }, 300);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [symbol, timeframe]);

  // Follow the job until it's stored or failed.
  useEffect(() => {
    if (job === null || !running) return;
    const timer = setTimeout(async () => {
      const at = Date.now();
      setNow(at);
      try {
        const next = await backtestJob(job.id);
        setJob(next);
        if (next.status === "running") setTimes((t) => ({ ...t, started: t.started ?? at }));
        if (next.status === "done" && next.run_id !== null) onDone(next.run_id);
        if (next.status === "failed") setError(`Refused: ${next.error ?? "no reason given"}`);
      } catch (problem) {
        setJob(null);
        setError(problem instanceof ApiError ? problem.message : "Couldn't reach the API.");
      }
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [job, running, onDone]);

  useEffect(() => {
    if (error !== null) errorRef.current?.focus();
  }, [error]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid || payload === null || running) return;
    const backtest: NewBacktest = {
      ...payload,
      symbol: symbol.trim().toUpperCase(),
      timeframe,
      days: Number(days.trim()),
      cash: cash.trim(),
      fee_rate: fee.trim(),
      slippage: slippage.trim() === "" ? null : slippage.trim(),
    };
    setError(null);
    try {
      const sent = Date.now();
      setJob(await runBacktest(backtest));
      setTimes({ sent, started: null });
      setNow(sent);
    } catch (problem) {
      setError(
        problem instanceof ApiError ? `Refused: ${problem.message}` : "Couldn't reach the API.",
      );
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-6" aria-label="New backtest">
      <fieldset className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <legend className="mb-2 text-sm font-semibold">Market</legend>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="backtest-symbol">Symbol</Label>
          <Input
            id="backtest-symbol"
            value={symbol}
            autoComplete="off"
            aria-describedby="backtest-symbol-hint"
            onChange={(event) => setSymbol(event.target.value)}
          />
          <p id="backtest-symbol-hint" className="text-muted-foreground text-xs">
            e.g. BTC/USDT; its candles must be backfilled
          </p>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="backtest-timeframe">Timeframe</Label>
          <NativeSelect
            id="backtest-timeframe"
            value={timeframe}
            onChange={(event) => setTimeframe(event.target.value)}
          >
            {catalog.timeframes.map((tf) => (
              <option key={tf} value={tf}>
                {tf}
              </option>
            ))}
          </NativeSelect>
        </div>
        <p className="text-muted-foreground text-xs sm:col-span-2" data-testid="stored-range">
          {storedText(stored)}
        </p>
        {fields.map((field, i) => {
          const id = `backtest-${field.spec.name}`;
          const problem = problems[i];
          return (
            <div key={field.spec.name} className="flex flex-col gap-1.5">
              <Label htmlFor={id}>{field.label}</Label>
              <Input
                id={id}
                value={field.value}
                inputMode={field.spec.type === "integer" ? "numeric" : "decimal"}
                className="font-mono"
                aria-invalid={problem !== null}
                aria-describedby={`${id}-hint`}
                onChange={(event) => field.set(event.target.value)}
              />
              <p id={`${id}-hint`} className="text-muted-foreground text-xs">
                {problem === null ? field.hint : <span className="text-loss">{problem}</span>}
              </p>
            </div>
          );
        })}
      </fieldset>

      <StrategySetup
        catalog={catalog}
        setup={setup}
        onChange={setSetup}
        sizingNote="How much each buy spends. Backtests don't apply the risk limits, as the CLI's don't."
      />

      {error !== null && (
        <p ref={errorRef} tabIndex={-1} role="alert" className="text-loss text-sm outline-none">
          {error}
        </p>
      )}
      {fits !== null && (
        <div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              // Days that fit what's stored; Run is then one Tab away.
              setDays(String(fits));
              setJob(null);
              setError(null);
              document.getElementById("backtest-days")?.focus();
            }}
          >
            Use {fits} days
          </Button>
        </div>
      )}
      {running && (
        <p role="status" className="text-sm">
          {progressText(job, times, now)}
        </p>
      )}
      <div className="flex justify-end">
        <Button type="submit" disabled={!valid || running}>
          {!running ? "Run backtest" : job?.status === "queued" ? "Queued…" : "Running…"}
        </Button>
      </div>
    </form>
  );
}

function percentHint(fraction: string): string {
  const value = fraction.trim();
  return /^\d*\.?\d+$|^\d+\.$/.test(value) ? ` (${shiftPoint(value, 2)}%)` : "";
}

function storedText(range: CandleRange | null): string {
  if (range === null) return "";
  if (range.count === 0 || range.first === null || range.last === null) {
    return `Nothing stored for ${range.symbol} ${range.timeframe}: backfill it first.`;
  }
  const days = Math.floor((Date.parse(range.last) - Date.parse(range.first)) / 86_400_000);
  return (
    `Stored: ${range.count.toLocaleString("en-US")} candles, ${formatManila(range.first)} to ` +
    `${formatManila(range.last)} (about ${days} days; the strategy's warm-up comes from these too).`
  );
}

/** Ages by this browser's clock (when it sent the job, when it saw it start), not the API's. */
function progressText(job: BacktestJob | null, times: Times, now: number): string {
  if (job === null) return "";
  if (job.status === "running") {
    return `Running… ${formatAge((now - (times.started ?? times.sent)) / 1000)}`;
  }
  const waited = (now - times.sent) / 1000;
  const late = waited >= QUEUED_WARNING_S ? ": is the backtester running?" : "";
  return `Queued, ${formatAge(waited)}${late}`;
}
