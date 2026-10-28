"use client";

import { Download } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type ReactNode } from "react";

import { Money } from "@/components/money";
import { useFeed } from "@/components/overview-provider";
import { Filter, PageTabs } from "@/components/page-tabs";
import { Timestamp } from "@/components/timestamp";
import { TradeExplainPanel } from "@/components/trade-explain-panel";
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
import { usePages } from "@/components/use-pages";
import { historyFills, historyOrders } from "@/lib/api";
import { formatDecimal, priceDigits } from "@/lib/format";
import {
  ORDER_STATUSES,
  csvFileName,
  filtersFrom,
  historyQuery,
  type FillRow,
  type HistoryFilters,
  type HistoryTab,
  type OrderRow,
} from "@/lib/history";
import { KIND_LABEL } from "@/lib/market";

const TABS = [
  { id: "orders", label: "Orders" },
  { id: "fills", label: "Fills" },
];

function why(kind: string): string {
  return KIND_LABEL[kind]?.long ?? kind;
}

function quoteOf(symbol: string): string {
  return symbol.split("/")[1] ?? "";
}

function coinOf(symbol: string): string {
  return symbol.split("/")[0] ?? "";
}

function Price({ value, currency }: { value: string | null; currency: string }) {
  if (value === null) return <span className="text-muted-foreground">—</span>;
  return <Money value={value} currency={currency} max={priceDigits(value)} />;
}

/** The time of a row, as the button that explains its order (UX9; the keyboard's way in). */
function ExplainTime({
  iso,
  orderId,
  what,
  onExplain,
}: {
  iso: string;
  orderId: number;
  what: string;
  onExplain: (orderId: number) => void;
}) {
  return (
    <button
      type="button"
      className="hover:underline focus-visible:underline"
      onClick={() => onExplain(orderId)}
      aria-label={`Explain ${what} (order ${orderId})`}
    >
      <Timestamp iso={iso} />
    </button>
  );
}

function Rows<T extends { id: number }>({
  noun,
  rows,
  more,
  error,
  loadingOlder,
  onOlder,
  head,
  row,
}: {
  noun: string;
  rows: T[] | null;
  more: boolean;
  error: string | null;
  loadingOlder: boolean;
  onOlder: () => void;
  head: ReactNode;
  row: (item: T) => ReactNode;
}) {
  if (rows === null) return <p className="text-muted-foreground text-sm">Loading…</p>;
  return (
    <>
      {error && (
        <p role="alert" className="text-loss text-sm">
          {error}
        </p>
      )}
      {rows.length === 0 ? (
        !error && <p className="text-muted-foreground text-sm">No {noun} match these filters.</p>
      ) : (
        <Card className="py-2">
          <Table>
            <TableHeader>
              <TableRow>{head}</TableRow>
            </TableHeader>
            <TableBody>{rows.map(row)}</TableBody>
          </Table>
        </Card>
      )}
      <div className="flex items-center gap-3 text-sm">
        <span className="text-muted-foreground" role="status">
          {rows.length} {noun}
          {more ? " so far" : ""}
        </span>
        {more && (
          <Button variant="outline" size="sm" onClick={onOlder} disabled={loadingOlder}>
            {loadingOlder ? "Loading…" : "Load older"}
          </Button>
        )}
      </div>
    </>
  );
}

function Orders({ query, onExplain }: { query: string; onExplain: (id: number) => void }) {
  const { pages, older, loadingOlder } = usePages<OrderRow>(historyOrders, query);
  return (
    <Rows
      noun="orders"
      rows={pages?.rows ?? null}
      more={pages?.more ?? false}
      error={pages?.error ?? null}
      loadingOlder={loadingOlder}
      onOlder={older}
      head={
        <>
          <TableHead>Placed</TableHead>
          <TableHead>Bot</TableHead>
          <TableHead>Side</TableHead>
          <TableHead>Why</TableHead>
          <TableHead>Status</TableHead>
          <TableHead className="text-right">Limit</TableHead>
          <TableHead className="text-right">Avg fill</TableHead>
          <TableHead className="text-right">Filled / amount</TableHead>
          <TableHead className="text-right">Fees</TableHead>
        </>
      }
      row={(order) => (
        <TableRow key={order.id} data-order={order.id}>
          <TableCell>
            <ExplainTime
              iso={order.created_at}
              orderId={order.id}
              what={`the ${order.side}`}
              onExplain={onExplain}
            />
          </TableCell>
          <TableCell>
            <div>{order.bot_name}</div>
            <div className="text-muted-foreground text-xs">{order.symbol}</div>
          </TableCell>
          <TableCell className="uppercase">{order.side}</TableCell>
          <TableCell>{why(order.kind)}</TableCell>
          <TableCell className="max-w-56 whitespace-normal">
            {order.status}
            {order.reject_reason && (
              <div className="text-muted-foreground text-xs">{order.reject_reason}</div>
            )}
          </TableCell>
          <TableCell className="text-right">
            <Price value={order.price} currency={quoteOf(order.symbol)} />
          </TableCell>
          <TableCell className="text-right">
            <Price value={order.avg_fill_price} currency={quoteOf(order.symbol)} />
          </TableCell>
          <TableCell className="text-right font-mono tabular-nums">
            {formatDecimal(order.filled, { min: 0, max: 8 })}
            {" / "}
            <Money value={order.amount} currency={coinOf(order.symbol)} min={0} max={8} />
          </TableCell>
          <TableCell className="text-right">
            {order.fees === null || order.fee_currency === null ? (
              <span className="text-muted-foreground">—</span>
            ) : (
              <Money value={order.fees} currency={order.fee_currency} max={8} />
            )}
          </TableCell>
        </TableRow>
      )}
    />
  );
}

function Fills({ query, onExplain }: { query: string; onExplain: (id: number) => void }) {
  const { pages, older, loadingOlder } = usePages<FillRow>(historyFills, query);
  return (
    <Rows
      noun="fills"
      rows={pages?.rows ?? null}
      more={pages?.more ?? false}
      error={pages?.error ?? null}
      loadingOlder={loadingOlder}
      onOlder={older}
      head={
        <>
          <TableHead>Filled</TableHead>
          <TableHead>Bot</TableHead>
          <TableHead>Side</TableHead>
          <TableHead>Why</TableHead>
          <TableHead className="text-right">Price</TableHead>
          <TableHead className="text-right">Amount</TableHead>
          <TableHead className="text-right">Value</TableHead>
          <TableHead className="text-right">Fee</TableHead>
        </>
      }
      row={(fill) => (
        <TableRow key={fill.id} data-fill={fill.id}>
          <TableCell>
            <ExplainTime
              iso={fill.ts}
              orderId={fill.order_id}
              what={`fill ${fill.id}`}
              onExplain={onExplain}
            />
          </TableCell>
          <TableCell>
            <div>{fill.bot_name}</div>
            <div className="text-muted-foreground text-xs">{fill.symbol}</div>
          </TableCell>
          <TableCell className="uppercase">{fill.side}</TableCell>
          <TableCell>{why(fill.kind)}</TableCell>
          <TableCell className="text-right">
            <Price value={fill.price} currency={quoteOf(fill.symbol)} />
          </TableCell>
          <TableCell className="text-right">
            <Money value={fill.amount} currency={coinOf(fill.symbol)} min={0} max={8} />
          </TableCell>
          <TableCell className="text-right">
            <Money value={fill.value} currency={quoteOf(fill.symbol)} />
          </TableCell>
          <TableCell className="text-right">
            <Money value={fill.fee} currency={fill.fee_currency} max={8} />
          </TableCell>
        </TableRow>
      )}
    />
  );
}

function Filters({
  tab,
  filters,
  onChange,
}: {
  tab: HistoryTab;
  filters: HistoryFilters;
  onChange: (name: keyof HistoryFilters, value: string) => void;
}) {
  const bots = useFeed().overview?.bots ?? [];
  const symbols = [...new Set(bots.map((bot) => bot.symbol))].sort();
  return (
    <div className="flex flex-wrap items-end gap-3">
      <Filter label="Bot">
        <NativeSelect value={filters.bot} onChange={(e) => onChange("bot", e.target.value)}>
          <option value="">Any bot</option>
          {bots.map((bot) => (
            <option key={bot.id} value={bot.id}>
              {bot.name}
            </option>
          ))}
        </NativeSelect>
      </Filter>
      <Filter label="Market">
        <NativeSelect value={filters.symbol} onChange={(e) => onChange("symbol", e.target.value)}>
          <option value="">Any market</option>
          {symbols.map((symbol) => (
            <option key={symbol} value={symbol}>
              {symbol}
            </option>
          ))}
        </NativeSelect>
      </Filter>
      <Filter label="Side">
        <NativeSelect value={filters.side} onChange={(e) => onChange("side", e.target.value)}>
          <option value="">Buys and sells</option>
          <option value="buy">Buy</option>
          <option value="sell">Sell</option>
        </NativeSelect>
      </Filter>
      {tab === "orders" && (
        <Filter label="Status">
          <NativeSelect value={filters.status} onChange={(e) => onChange("status", e.target.value)}>
            <option value="">Any status</option>
            {ORDER_STATUSES.map((status) => (
              <option key={status} value={status}>
                {status}
              </option>
            ))}
          </NativeSelect>
        </Filter>
      )}
      <Filter label="Why">
        <NativeSelect value={filters.kind} onChange={(e) => onChange("kind", e.target.value)}>
          <option value="">Any reason</option>
          {Object.entries(KIND_LABEL).map(([kind, label]) => (
            <option key={kind} value={kind}>
              {label.long}
            </option>
          ))}
        </NativeSelect>
      </Filter>
      <Filter label="From (Manila day)">
        <Input
          type="date"
          value={filters.from}
          max={filters.to || undefined}
          onChange={(e) => onChange("from", e.target.value)}
        />
      </Filter>
      <Filter label="To (Manila day, included)">
        <Input
          type="date"
          value={filters.to}
          min={filters.from || undefined}
          onChange={(e) => onChange("to", e.target.value)}
        />
      </Filter>
    </div>
  );
}

function History() {
  const router = useRouter();
  const params = useSearchParams();
  const tab: HistoryTab = params.get("tab") === "fills" ? "fills" : "orders";
  const filters = filtersFrom(params);
  const query = historyQuery(tab, filters).toString();
  const [explaining, setExplaining] = useState<number | null>(null);

  function withParams(change: (next: URLSearchParams) => void): string {
    const next = new URLSearchParams(params);
    change(next);
    return `/history?${next.toString()}`;
  }

  function setFilter(name: keyof HistoryFilters, value: string) {
    router.replace(
      withParams((next) => (value ? next.set(name, value) : next.delete(name))),
      { scroll: false },
    );
  }

  return (
    <section aria-labelledby="history" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 id="history" className="text-xl font-semibold">
          History
        </h1>
        <PageTabs
          label="History views"
          tabs={TABS}
          current={tab}
          href={(id) => withParams((next) => next.set("tab", id))}
        />
        <Button asChild variant="outline" size="sm" className="ml-auto">
          {/* Same origin, so the session cookie goes along; the same query as the table. */}
          <a href={`/api/history/${tab}.csv?${query}`} download={csvFileName(tab, filters)}>
            <Download aria-hidden />
            Export CSV
          </a>
        </Button>
      </div>
      <Filters tab={tab} filters={filters} onChange={setFilter} />
      <p className="text-muted-foreground text-xs">
        Days are Manila time; the export has every matching row, with exact amounts and UTC times.
        Choose a row&apos;s time to see why it happened.
      </p>
      {tab === "orders" ? (
        <Orders query={query} onExplain={setExplaining} />
      ) : (
        <Fills query={query} onExplain={setExplaining} />
      )}
      <TradeExplainPanel orderId={explaining} onClose={() => setExplaining(null)} />
    </section>
  );
}

/** History (DESIGN §4, D49): every order and fill, filtered, explained and exported. */
export default function HistoryPage() {
  // useSearchParams needs a Suspense boundary, so the page can still prerender.
  return (
    <Suspense>
      <History />
    </Suspense>
  );
}
