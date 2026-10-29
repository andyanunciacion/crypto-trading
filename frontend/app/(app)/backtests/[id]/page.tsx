"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { Plain, Ratio, metricData } from "@/components/backtest-metrics";
import { EquityChart } from "@/components/equity-chart";
import { Money } from "@/components/money";
import { Timestamp } from "@/components/timestamp";
import { Card } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError, backtest as fetchRun } from "@/lib/api";
import {
  EXIT_LABELS,
  describeConfig,
  rangeDays,
  type ConfigRef,
  type Metrics,
  type RunDetail,
} from "@/lib/backtests";
import {
  REGIMES,
  describeParams,
  describeSizing,
  describeStops,
  policyLabel,
  regimeLabel,
  type AutoSwitchSettings,
  type MappedConfig,
} from "@/lib/bots";
import { priceDigits, shiftPoint } from "@/lib/format";

/**
 * One backtest's results (D48): its metrics (worked out by the same function as the
 * CLI's report), the equity curve with its drawdown, every round trip, the signals that
 * didn't become fills, and what it was run with.
 */
export default function BacktestPage() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const runId = Number(id);
  const validId = Number.isInteger(runId) && runId >= 1;
  const error = validId ? loadError : `No backtest run ${id}.`;

  useEffect(() => {
    if (!validId) return;
    let current = true;
    fetchRun(runId)
      .then((loaded) => current && setRun(loaded))
      .catch((problem: unknown) => {
        if (current) {
          setLoadError(problem instanceof ApiError ? problem.message : "Couldn't reach the API.");
        }
      });
    return () => {
      current = false;
    };
  }, [runId, validId]);

  return (
    <section aria-labelledby="run" className="flex flex-col gap-4">
      <Link
        href="/backtests"
        className="text-muted-foreground flex items-center gap-1 text-sm hover:underline"
      >
        <ArrowLeft aria-hidden className="size-4" />
        Backtests
      </Link>
      {error !== null ? (
        <p role="alert" className="text-loss text-sm">
          {error}
        </p>
      ) : run === null ? (
        <p role="status" className="text-muted-foreground text-sm">
          Loading…
        </p>
      ) : (
        <Results run={run} />
      )}
    </section>
  );
}

function Results({ run }: { run: RunDetail }) {
  const quote = run.currency;
  const auto = run.auto_switch;
  return (
    <>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 id="run" className="text-xl font-semibold">
          Run #{run.id}: {auto === null ? run.strategy : "auto-switching"} on {run.symbol}{" "}
          {run.timeframe}
        </h1>
        <span className="text-muted-foreground text-sm">
          {rangeDays(run)} days, <Timestamp iso={run.range_start} /> to{" "}
          <Timestamp iso={run.range_end} /> (Manila)
        </span>
      </div>

      {run.metrics === null ? (
        <p role="alert" className="text-loss text-sm">
          No metrics: {run.problem}
        </p>
      ) : (
        <MetricCards metrics={run.metrics} quote={quote} initialCash={run.initial_cash} />
      )}

      <Card className="px-4 py-3">
        <h2 className="text-lg font-semibold">Equity</h2>
        {run.equity.length > 0 ? (
          <EquityChart points={run.equity} initialCash={run.initial_cash} currency={quote} />
        ) : (
          <p className="text-muted-foreground text-sm">No equity curve.</p>
        )}
      </Card>

      {auto !== null && <AutoSwitching settings={auto} run={run} />}

      <h2 id="trades" className="text-lg font-semibold">
        Trades
      </h2>
      {run.trades.length === 0 ? (
        <p className="text-muted-foreground text-sm">
          No closed trades{run.metrics?.open_trade ? "; one was still open at the end" : ""}.
        </p>
      ) : (
        <Card className="py-2">
          <Table aria-labelledby="trades">
            <TableHeader>
              <TableRow>
                <TableHead>Entry</TableHead>
                <TableHead>Exit</TableHead>
                <TableHead className="text-right">Amount</TableHead>
                <TableHead className="text-right">Entry price</TableHead>
                <TableHead className="text-right">Exit price</TableHead>
                <TableHead className="text-right">Fees</TableHead>
                <TableHead className="text-right">P&amp;L</TableHead>
                <TableHead className="text-right">Return</TableHead>
                {auto !== null && <TableHead>Bought by</TableHead>}
                <TableHead>Sold by</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {run.trades.map((trade) => (
                <TableRow key={trade.entry_ts}>
                  {/* A backtest can span years, so its trades carry theirs. */}
                  <TableCell>
                    <Timestamp iso={trade.entry_ts} year />
                  </TableCell>
                  <TableCell>
                    <Timestamp iso={trade.exit_ts} year />
                  </TableCell>
                  <TableCell className="text-right">
                    <Money
                      value={trade.amount}
                      currency={run.symbol.split("/")[0]}
                      min={0}
                      max={8}
                    />
                  </TableCell>
                  {/* A fill price carries its slippage's digits: rounded, the exact one on hover. */}
                  <TableCell className="text-right" title={`${trade.entry_price} ${quote}`}>
                    <Money
                      value={trade.entry_price}
                      currency={quote}
                      max={priceDigits(trade.entry_price)}
                    />
                  </TableCell>
                  <TableCell className="text-right" title={`${trade.exit_price} ${quote}`}>
                    <Money
                      value={trade.exit_price}
                      currency={quote}
                      max={priceDigits(trade.exit_price)}
                    />
                  </TableCell>
                  <TableCell className="text-right">
                    <Money value={trade.fees} currency={quote} />
                  </TableCell>
                  <TableCell className="text-right">
                    <Money value={trade.pnl} currency={quote} pnl />
                  </TableCell>
                  <TableCell className="text-right">
                    <Ratio value={trade.return_} pnl />
                  </TableCell>
                  {auto !== null && (
                    <TableCell className="text-xs">
                      {trade.decided_by === null ? "—" : <ConfigName config={trade.decided_by} />}
                    </TableCell>
                  )}
                  <TableCell className="text-muted-foreground text-xs">
                    {trade.exit === null ? "sell signal" : EXIT_LABELS[trade.exit]}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}

      {run.skipped.length > 0 && (
        <>
          <h2 id="skipped" className="text-lg font-semibold">
            Skipped orders
          </h2>
          <Card className="py-2">
            <Table aria-labelledby="skipped">
              <TableHeader>
                <TableRow>
                  <TableHead>Signal</TableHead>
                  <TableHead>Side</TableHead>
                  <TableHead>Why</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {run.skipped.map((order) => (
                  <TableRow key={`${order.signal_ts}-${order.side}`}>
                    <TableCell>
                      <Timestamp iso={order.signal_ts} />
                    </TableCell>
                    <TableCell>{order.side}</TableCell>
                    <TableCell className="text-muted-foreground text-xs whitespace-normal">
                      {order.reason}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        </>
      )}

      <h2 className="text-lg font-semibold">Run with</h2>
      <Card className="px-6 py-4">
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
          <Setting term={auto === null ? "Strategy" : "Started on"}>
            {run.strategy} v{run.strategy_version}:{" "}
            <span className="font-mono">{describeParams(run.params)}</span>
          </Setting>
          <Setting term="Starting cash">
            <Money value={run.initial_cash} currency={quote} />
          </Setting>
          <Setting term="Fee · slippage">
            <span className="font-mono">
              {shiftPoint(run.fee_rate, 2)}% · {shiftPoint(run.slippage_rate, 2)}%
            </span>{" "}
            of each fill
          </Setting>
          <Setting term="Sizing">{describeSizing(run.sizing)}</Setting>
          <Setting term="Stops">{describeStops(run.stops)}</Setting>
          <Setting term="Candles">
            {run.candle_count.toLocaleString("en-US")}, of which {run.warmup_candles} warm-up before
            the range; {run.fills} fills from {run.signals} signals
          </Setting>
          <Setting term="Reproduce">
            <span className="font-mono text-xs break-all">
              candles sha256 {run.candles_sha256}, backtester v{run.backtester_version}
            </span>
          </Setting>
        </dl>
      </Card>
    </>
  );
}

/** A strategy config: its name and version, then its params, small. */
function ConfigName({ config }: { config: ConfigRef | MappedConfig }) {
  return (
    <>
      {describeConfig(config)}
      <span className="text-muted-foreground block font-mono">{describeParams(config.params)}</span>
    </>
  );
}

/**
 * An auto-switching run's regime mapping and every switch it made (P6-5), as a bot's
 * page shows its own (D54). A switch confirmed at a candle's close trades from the next.
 */
function AutoSwitching({ settings, run }: { settings: AutoSwitchSettings; run: RunDetail }) {
  const n = run.switches.length;
  return (
    <section aria-labelledby="auto" className="flex flex-col gap-2">
      <h2 id="auto" className="text-lg font-semibold">
        Auto-switching
      </h2>
      <p className="text-muted-foreground -mt-1 text-sm">
        Started on {run.strategy}; switched once a new regime held for {settings.min_hold} closed
        candle{settings.min_hold === 1 ? "" : "s"} in a row. An open position on a switch:{" "}
        {policyLabel(settings.policy).toLowerCase()}. {n} switch{n === 1 ? "" : "es"}.
      </p>
      <Card className="py-2">
        <Table aria-labelledby="auto">
          <TableHeader>
            <TableRow>
              <TableHead>Regime</TableHead>
              <TableHead>Strategy</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {REGIMES.map(({ value, label }) => (
              <TableRow key={value} data-regime={value}>
                <TableCell>{label}</TableCell>
                <TableCell className="text-xs">
                  <ConfigName config={settings.mapping[value]} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Card>
      {n > 0 && (
        <Card className="py-2">
          <Table aria-label="Switches">
            <TableHeader>
              <TableRow>
                <TableHead>Confirmed</TableHead>
                <TableHead>Regime</TableHead>
                <TableHead>From</TableHead>
                <TableHead>To</TableHead>
                <TableHead>Why</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {run.switches.map((s) => (
                <TableRow key={s.ts} data-switch={s.ts}>
                  <TableCell>
                    <Timestamp iso={s.ts} />
                  </TableCell>
                  <TableCell>{regimeLabel(s.regime)}</TableCell>
                  <TableCell className="text-xs">
                    <ConfigName config={s.from_config} />
                  </TableCell>
                  <TableCell className="text-xs">
                    <ConfigName config={s.to_config} />
                  </TableCell>
                  <TableCell className="min-w-60 text-xs whitespace-normal">{s.reason}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </section>
  );
}

function Setting({ term, children }: { term: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{term}</dt>
      <dd>{children}</dd>
    </>
  );
}

/** The report's headline numbers (D30), each with its unit (UX principle 4). */
function MetricCards({
  metrics,
  quote,
  initialCash,
}: {
  metrics: Metrics;
  quote: string;
  initialCash: string;
}) {
  const tiles: { label: string; name: keyof Metrics; value: ReactNode; note?: string }[] = [
    {
      label: "Total return",
      name: "total_return",
      value: <Ratio value={metrics.total_return} pnl />,
      note: metrics.open_trade ? "still holding at the end" : undefined,
    },
    {
      label: "Final equity",
      name: "final_equity",
      value: <Money value={metrics.final_equity} currency={quote} />,
      note: `from ${initialCash} ${quote}`,
    },
    { label: "Max drawdown", name: "max_drawdown", value: <Ratio value={metrics.max_drawdown} /> },
    {
      label: "Sharpe",
      name: "sharpe",
      value: <Plain value={metrics.sharpe} />,
      note: "annualized",
    },
    {
      label: "Trades",
      name: "trade_count",
      value: <span className="font-mono tabular-nums">{metrics.trade_count}</span>,
      note: "closed round trips",
    },
    { label: "Win rate", name: "win_rate", value: <Ratio value={metrics.win_rate} /> },
    {
      label: "Profit factor",
      name: "profit_factor",
      value: <Plain value={metrics.profit_factor} />,
      note:
        metrics.profit_factor === null && metrics.trade_count > 0 ? "no losing trades" : undefined,
    },
    {
      label: "Fees",
      name: "total_fees",
      value: <Money value={metrics.total_fees} currency={quote} />,
    },
  ];
  return (
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {tiles.map((tile) => (
        <Card key={tile.name} className="gap-1 px-4 py-3">
          <dt className="text-muted-foreground text-xs">{tile.label}</dt>
          <dd className="text-lg" {...metricData(metrics, tile.name)}>
            {tile.value}
          </dd>
          {tile.note && <dd className="text-muted-foreground text-xs">{tile.note}</dd>}
        </Card>
      ))}
    </dl>
  );
}
