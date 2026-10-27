"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { BacktestForm } from "@/components/backtest-form";
import { Plain, Ratio } from "@/components/backtest-metrics";
import { Timestamp } from "@/components/timestamp";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError, backtests as fetchRuns, catalog as fetchCatalog } from "@/lib/api";
import { rangeDays, type RunSummary } from "@/lib/backtests";
import { describeParams, type Catalog } from "@/lib/bots";

function problemText(error: unknown): string {
  return error instanceof ApiError ? error.message : "Couldn't reach the API.";
}

/**
 * Backtests (DESIGN §4, D48): the run form, then every stored run with its metrics,
 * newest first; a run opens its results. A run from here is the CLI's
 * `engine.backtest.run` with the same arguments, carried out by the backtester service,
 * so its numbers are the CLI's.
 */
export default function BacktestsPage() {
  const router = useRouter();
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    fetchCatalog()
      .then((loaded) => current && setCatalog(loaded))
      .catch((error: unknown) => current && setCatalogError(problemText(error)));
    fetchRuns()
      .then((loaded) => current && setRuns(loaded))
      .catch((error: unknown) => current && setRunsError(problemText(error)));
    return () => {
      current = false;
    };
  }, []);

  const opened = useCallback((runId: number) => router.push(`/backtests/${runId}`), [router]);

  return (
    <section aria-labelledby="backtests" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 id="backtests" className="text-xl font-semibold">
          Backtests
        </h1>
        <span className="text-muted-foreground text-sm">
          stored candles, filled at the next candle&apos;s open, with fees and slippage
        </span>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>
            <h2>New backtest</h2>
          </CardTitle>
        </CardHeader>
        <CardContent>
          {catalogError !== null ? (
            <p role="alert" className="text-loss text-sm">
              {catalogError}
            </p>
          ) : catalog === null ? (
            <p role="status" className="text-muted-foreground text-sm">
              Loading the strategies…
            </p>
          ) : (
            <BacktestForm catalog={catalog} onDone={opened} />
          )}
        </CardContent>
      </Card>

      <h2 className="text-lg font-semibold">Runs</h2>
      {runsError && (
        <p role="alert" className="text-loss text-sm">
          {runsError}
        </p>
      )}
      {runs === null ? (
        !runsError && <p className="text-muted-foreground text-sm">Loading…</p>
      ) : runs.length === 0 ? (
        <p className="text-muted-foreground text-sm">No runs yet.</p>
      ) : (
        <Card className="py-2">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Strategy</TableHead>
                <TableHead>Market</TableHead>
                <TableHead className="text-right">Days</TableHead>
                <TableHead className="text-right">Return</TableHead>
                <TableHead className="text-right">Max drawdown</TableHead>
                <TableHead className="text-right">Sharpe</TableHead>
                <TableHead className="text-right">Trades</TableHead>
                <TableHead className="text-right">Won</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                // The whole row opens the run; the #id link is the keyboard's way in (a row
                // can't take focus), and handles its own click.
                <TableRow
                  key={run.id}
                  data-run={run.id}
                  className="cursor-pointer"
                  onClick={(event) => {
                    if ((event.target as HTMLElement).closest("a") === null) opened(run.id);
                  }}
                >
                  <TableCell>
                    <Link
                      href={`/backtests/${run.id}`}
                      className="font-medium underline-offset-4 hover:underline"
                    >
                      #{run.id}
                    </Link>
                    <div className="text-muted-foreground text-xs">
                      <Timestamp iso={run.created_at} />
                    </div>
                  </TableCell>
                  <TableCell className="min-w-40 whitespace-normal">
                    <div>{run.strategy}</div>
                    <div className="text-muted-foreground font-mono text-xs">
                      {describeParams(run.params)}
                    </div>
                  </TableCell>
                  <TableCell>
                    {run.symbol} · {run.timeframe}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">
                    {rangeDays(run)}
                  </TableCell>
                  {run.metrics === null ? (
                    <TableCell
                      colSpan={5}
                      className="text-muted-foreground text-xs whitespace-normal"
                    >
                      {run.problem}
                    </TableCell>
                  ) : (
                    <>
                      <TableCell className="text-right">
                        <Ratio value={run.metrics.total_return} pnl />
                      </TableCell>
                      <TableCell className="text-right">
                        <Ratio value={run.metrics.max_drawdown} />
                      </TableCell>
                      <TableCell className="text-right">
                        <Plain value={run.metrics.sharpe} />
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {run.metrics.trade_count}
                      </TableCell>
                      <TableCell className="text-right">
                        <Ratio value={run.metrics.win_rate} />
                      </TableCell>
                    </>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </section>
  );
}
