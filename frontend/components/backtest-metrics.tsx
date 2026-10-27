import { Percent } from "@/components/money";
import type { Metrics } from "@/lib/backtests";
import { formatDecimal, shiftPoint, type Decimal } from "@/lib/format";
import { cn } from "@/lib/utils";

/** A fraction from the API (0.0759) as a percent (7.59%), exactly; "—" for none. */
export function Ratio({
  value,
  pnl = false,
  className,
}: {
  value: Decimal | null;
  pnl?: boolean;
  className?: string;
}) {
  return (
    <Percent value={value === null ? null : shiftPoint(value, 2)} pnl={pnl} className={className} />
  );
}

/** A plain ratio to two places (Sharpe, profit factor); "—" for none. */
export function Plain({ value, className }: { value: Decimal | null; className?: string }) {
  if (value === null) return <span className={cn("text-muted-foreground", className)}>—</span>;
  return (
    <span className={cn("font-mono tabular-nums", className)}>
      {formatDecimal(value, { min: 2, max: 2 })}
    </span>
  );
}

/**
 * The metric a cell shows, with its exact value in `data-value` (what the e2e test
 * compares with the CLI's report).
 */
export function metricData(metrics: Metrics, name: keyof Metrics) {
  const value = metrics[name];
  return { "data-metric": name, "data-value": value === null ? "" : String(value) };
}
