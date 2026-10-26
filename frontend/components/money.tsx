import { cn } from "@/lib/utils";
import { formatDecimal, signOf, type Decimal } from "@/lib/format";

type MoneyProps = {
  value: Decimal;
  /** Always shown (UX4): "USDT", "BTC". */
  currency: string;
  /** Digits after the point; quantities of coins take up to 8. */
  min?: number;
  max?: number;
  /** For P&L: always signed, and green or red as well (never color alone, DESIGN §10). */
  pnl?: boolean;
  className?: string;
};

/** A Decimal string with its currency, in tabular figures (UX6). */
export function Money({ value, currency, min = 2, max = 2, pnl = false, className }: MoneyProps) {
  return (
    <span className={cn("font-mono tabular-nums", pnl && pnlColor(value), className)}>
      {formatDecimal(value, { min, max, sign: pnl })}
      <span className="text-muted-foreground ml-1 text-[0.85em]">{currency}</span>
    </span>
  );
}

/** A percentage; signed and colored for P&L. "—" when there's nothing to divide by. */
export function Percent({
  value,
  pnl = false,
  className,
}: {
  value: Decimal | null;
  pnl?: boolean;
  className?: string;
}) {
  if (value === null) return <span className={cn("text-muted-foreground", className)}>—</span>;
  return (
    <span className={cn("font-mono tabular-nums", pnl && pnlColor(value), className)}>
      {formatDecimal(value, { sign: pnl })}%
    </span>
  );
}

function pnlColor(value: Decimal): string {
  const sign = signOf(value);
  return sign > 0 ? "text-profit" : sign < 0 ? "text-loss" : "";
}
