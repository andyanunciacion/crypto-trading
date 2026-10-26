"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

import { Money } from "@/components/money";
import { Timestamp } from "@/components/timestamp";
import { Badge } from "@/components/ui/badge";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { ApiError, explain } from "@/lib/api";
import { KIND_LABEL, type TradeExplanation } from "@/lib/market";

/** Coins: up to 8 decimals, none forced. */
const COIN = { min: 0, max: 8 };

function Section({ title, children }: { title: string; children: ReactNode }) {
  // Named by its heading, so it's a landmark a screen reader can jump to.
  return (
    <section aria-label={title} className="flex flex-col gap-2">
      <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
        {title}
      </h3>
      {children}
    </section>
  );
}

/** Label/value rows, e.g. a strategy's params or an indicator's values. */
function Rows({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="font-mono tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A stored JSON value, shown as it is (settings, params, account numbers). */
function plain(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function entries(record: Record<string, unknown>): [string, ReactNode][] {
  return Object.entries(record).map(([key, value]) => [key, plain(value)]);
}

/** An indicator value: a float from the strategy's own math, shown to 6 significant digits. */
function indicator(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumSignificantDigits: 6 }).format(value);
}

function Explanation({ trade }: { trade: TradeExplanation }) {
  const [coin, quote] = trade.symbol.split("/");
  const { signal, exit, risk } = trade;
  return (
    <div className="flex flex-col gap-6">
      <Section title="Order">
        <Rows
          rows={[
            ["placed", <Timestamp key="t" iso={trade.created_at} />],
            ["limit", <Money key="p" value={trade.price} currency={quote} max={8} />],
            ["amount", <Money key="a" value={trade.amount} currency={coin} {...COIN} />],
            ["filled", <Money key="f" value={trade.filled} currency={coin} {...COIN} />],
            ["status", trade.status],
            ["client order ID", trade.client_order_id],
          ]}
        />
      </Section>

      {signal && (
        <Section title="Signal">
          <p className="text-sm">
            <span className="font-semibold uppercase">{signal.action}</span>: {signal.reason}
          </p>
          <p className="text-muted-foreground text-xs">
            On the candle that opened <Timestamp iso={signal.ts} />; the values it decided on:
          </p>
          <Rows
            rows={Object.entries(signal.indicators).map(([name, value]) => [
              name,
              indicator(value),
            ])}
          />
        </Section>
      )}

      {exit && (
        <Section title="Exit">
          <p className="text-sm">
            The {KIND_LABEL[exit.kind]?.long ?? exit.kind} fired at a bid of{" "}
            <Money value={exit.bid} currency={quote} max={8} /> (<Timestamp iso={exit.price_ts} />
            ), attempt {exit.attempt}.
          </p>
          {exit.levels && <Rows rows={entries(exit.levels)} />}
          {exit.command && (
            <p className="text-muted-foreground text-xs">Kill switch command {exit.command}</p>
          )}
        </Section>
      )}

      {risk && (
        <Section title="Risk check">
          <p className="flex flex-wrap items-center gap-2 text-sm">
            <Badge
              variant="outline"
              className={risk.decision === "allowed" ? "" : "border-stale text-stale"}
            >
              {risk.decision}
            </Badge>
            <span className="font-mono text-xs">{risk.rule}</span>
          </p>
          <p className="text-sm">{risk.detail}</p>
          {risk.requested_amount !== null && (
            <p className="text-sm">
              Asked for <Money value={risk.requested_amount} currency={coin} {...COIN} />, placed{" "}
              <Money value={trade.amount} currency={coin} {...COIN} />.
            </p>
          )}
          {trade.risk_events.map((event) => (
            <div key={event.ts + event.rule} className="flex flex-col gap-1">
              <p className="text-muted-foreground text-xs">
                Checked against (<Timestamp iso={event.ts} />
                ):
              </p>
              <Rows rows={entries(event.inputs)} />
            </div>
          ))}
        </Section>
      )}

      {trade.strategy_config && (
        <Section title="Strategy">
          <p className="text-sm">
            {trade.strategy_config.strategy} v{trade.strategy_config.version} (config{" "}
            {trade.strategy_config.id})
          </p>
          <Rows rows={entries(trade.strategy_config.params)} />
        </Section>
      )}

      {trade.sizing && (
        <Section title="Sizing">
          <p className="text-sm">{trade.sizing.detail}</p>
          <Rows rows={entries(trade.sizing.settings)} />
        </Section>
      )}

      {trade.stops && (
        <Section title="Stops">
          <Rows
            rows={[...entries(trade.stops.settings), ["ATR at the signal", trade.stops.atr ?? "—"]]}
          />
        </Section>
      )}

      <Section title="Fills">
        {trade.fills.length === 0 ? (
          <p className="text-muted-foreground text-sm">None.</p>
        ) : (
          <ul className="flex flex-col gap-1 text-sm">
            {trade.fills.map((f) => (
              <li key={f.ts + f.amount} className="flex flex-wrap gap-x-3">
                <Timestamp iso={f.ts} />
                <Money value={f.amount} currency={coin} {...COIN} />
                <span>
                  at <Money value={f.price} currency={quote} max={8} />
                </span>
                <span className="text-muted-foreground">
                  fee <Money value={f.fee} currency={f.fee_currency} max={8} />
                </span>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <details className="text-xs">
        <summary className="text-muted-foreground cursor-pointer">Everything stored</summary>
        <pre className="bg-muted mt-2 overflow-x-auto rounded-md p-3">
          {JSON.stringify(trade.reason, null, 2)}
        </pre>
      </details>
    </div>
  );
}

/**
 * Why a trade happened (UX9): its signal and the indicator values behind it, the risk
 * check, the strategy, sizing and stops, and its fills, all as the engine stored them
 * when it placed the order (D46).
 */
export function TradeExplainPanel({
  orderId,
  onClose,
}: {
  orderId: number | null;
  onClose: () => void;
}) {
  const [loaded, setLoaded] = useState<{ id: number; trade?: TradeExplanation; error?: string }>();
  const opener = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (orderId === null) return;
    let current = true;
    explain(orderId)
      .then((trade) => {
        if (current) setLoaded({ id: orderId, trade });
      })
      .catch((error: unknown) => {
        const message = error instanceof ApiError ? error.message : String(error);
        if (current) setLoaded({ id: orderId, error: message });
      });
    return () => {
      current = false;
    };
  }, [orderId]);

  const shown = loaded?.id === orderId ? loaded : undefined;
  const trade = shown?.trade;
  const kind = trade?.exit ? (KIND_LABEL[trade.exit.kind]?.long ?? trade.exit.kind) : "signal";

  return (
    <Sheet open={orderId !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent
        aria-describedby={undefined}
        data-testid="trade-explain-panel"
        // Radix returns focus to its Trigger, and this panel has none (a marker or a
        // table row opens it): so focus goes back to whatever had it, e.g. that row.
        onOpenAutoFocus={() => {
          opener.current =
            document.activeElement instanceof HTMLElement ? document.activeElement : null;
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          if (opener.current?.isConnected) opener.current.focus();
        }}
      >
        <SheetHeader>
          <SheetTitle>
            {trade ? `${trade.side === "buy" ? "Buy" : "Sell"} ${trade.symbol}` : "Trade"}
          </SheetTitle>
          {trade && (
            <SheetDescription>
              Order {trade.order_id}, from a {kind}
            </SheetDescription>
          )}
        </SheetHeader>
        {shown?.error && <p className="text-loss text-sm">{shown.error}</p>}
        {!shown && <p className="text-muted-foreground text-sm">Loading…</p>}
        {trade && <Explanation trade={trade} />}
      </SheetContent>
    </Sheet>
  );
}
