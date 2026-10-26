"use client";

import { TriangleAlert } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useState, type ReactNode } from "react";

import { CandleChart } from "@/components/candle-chart";
import { Money } from "@/components/money";
import { useFeed, useNow } from "@/components/overview-provider";
import { Timestamp } from "@/components/timestamp";
import { TradeExplainPanel } from "@/components/trade-explain-panel";
import { Card } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useLiveChart, type Live } from "@/components/use-live-chart";
import { formatAge } from "@/lib/format";
import { FORMING_STALE_SECONDS, KIND_LABEL } from "@/lib/market";
import type { Bot } from "@/lib/overview";

function StaleLabel({ children }: { children: ReactNode }) {
  return (
    <span className="text-stale inline-flex items-center gap-1 text-xs font-semibold">
      <TriangleAlert aria-hidden className="size-3.5" />
      {children}
    </span>
  );
}

/** How current the chart is (UX5): the socket, and the forming candle's age. */
function Freshness({
  live,
  connected,
  skewMs,
}: {
  live: Live;
  connected: boolean;
  skewMs: number;
}) {
  const now = useNow();
  if (!connected) return <StaleLabel>stale: reconnecting to the live chart…</StaleLabel>;
  const forming = live.forming;
  if (forming === null) {
    return (
      <span className="text-muted-foreground text-xs">
        Closed candles only: the engine streams the forming candle while this bot runs.
      </span>
    );
  }
  const age = (now + skewMs - Date.parse(forming.received_at)) / 1000;
  if (age > FORMING_STALE_SECONDS) {
    return <StaleLabel>stale: the forming candle last moved {formatAge(age)} ago</StaleLabel>;
  }
  return (
    <span className="text-muted-foreground text-xs">Live: forming candle {formatAge(age)} ago</span>
  );
}

function Trades({ live, onSelect }: { live: Live; onSelect: (orderId: number) => void }) {
  const [coin, quote] = live.bot.symbol.split("/");
  if (live.trades.length === 0) {
    return <p className="text-muted-foreground text-sm">No trades on this chart yet.</p>;
  }
  return (
    <Card className="py-2">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>First fill</TableHead>
            <TableHead>Side</TableHead>
            <TableHead>Why</TableHead>
            <TableHead className="text-right">Price</TableHead>
            <TableHead className="text-right">Filled</TableHead>
            <TableHead>Status</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {[...live.trades].reverse().map((trade) => (
            <TableRow key={trade.order_id} data-order={trade.order_id}>
              <TableCell>
                {/* The keyboard's way to a trade's explanation; the marker is the mouse's. */}
                <button
                  type="button"
                  className="hover:underline focus-visible:underline"
                  onClick={() => onSelect(trade.order_id)}
                  aria-label={`Explain the ${trade.side} of order ${trade.order_id}`}
                >
                  <Timestamp iso={trade.ts} />
                </button>
              </TableCell>
              <TableCell className="uppercase">{trade.side}</TableCell>
              <TableCell>{KIND_LABEL[trade.kind]?.long ?? trade.kind}</TableCell>
              <TableCell className="text-right">
                <Money value={trade.price} currency={quote} max={8} />
              </TableCell>
              <TableCell className="text-right">
                <Money value={trade.amount} currency={coin} min={0} max={8} />
              </TableCell>
              <TableCell>{trade.status}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Card>
  );
}

function pick(bots: Bot[], wanted: string | null): Bot | undefined {
  const id = wanted === null ? NaN : Number(wanted);
  return (
    bots.find((bot) => bot.id === id) ?? bots.find((bot) => bot.status === "running") ?? bots.at(0)
  );
}

function Market() {
  const router = useRouter();
  const params = useSearchParams();
  const bots = useFeed().overview?.bots ?? null;
  const bot = bots === null ? undefined : pick(bots, params.get("bot"));
  // How many candles back, e.g. `?limit=5000` (the API takes 1 to 5000; default 1000).
  const limit = params.get("limit") === null ? undefined : Number(params.get("limit"));
  const { live, error, connected, skewMs } = useLiveChart(bot?.id ?? null, limit);
  const [explaining, setExplaining] = useState<number | null>(null);
  const select = useCallback((orderId: number) => setExplaining(orderId), []);

  if (bots === null) return <p className="text-muted-foreground">Connecting to the live feed…</p>;
  if (bot === undefined) {
    return (
      <p className="text-muted-foreground text-sm">
        No bots yet: <code>python -m engine.bots.create</code> adds one.
      </p>
    );
  }

  return (
    <>
      <section aria-labelledby="market" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <h1 id="market" className="text-xl font-semibold">
            Market
          </h1>
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted-foreground">Bot</span>
            <select
              className="bg-background rounded-md border px-2 py-1"
              value={bot.id}
              onChange={(event) => {
                const next = new URLSearchParams(params);
                next.set("bot", event.target.value);
                router.replace(`/market?${next}`);
              }}
            >
              {bots.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name} · {b.symbol} {b.timeframe} ({b.status})
                </option>
              ))}
            </select>
          </label>
          {live && (
            <span className="text-muted-foreground text-sm">
              {live.bot.strategy ?? "no strategy"}{" "}
              {live.bot.params && (
                <code className="text-xs">{JSON.stringify(live.bot.params)}</code>
              )}
            </span>
          )}
          {live && <Freshness live={live} connected={connected} skewMs={skewMs} />}
        </div>
        {error && <p className="text-loss text-sm">{error}</p>}
        {!live && !error && <p className="text-muted-foreground text-sm">Loading the chart…</p>}
        {live && live.candles.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No candles stored for {live.bot.symbol} {live.bot.timeframe} yet: the engine stores them
            as the bot runs, or backfill them with <code>engine.marketdata.backfill</code>.
          </p>
        )}
        {live && <CandleChart key={live.bot.id} live={live} onSelect={select} />}
      </section>

      {live && (
        <section aria-labelledby="trades" className="flex flex-col gap-3">
          <h2 id="trades" className="text-lg font-semibold">
            Trades
          </h2>
          <Trades live={live} onSelect={select} />
        </section>
      )}

      <TradeExplainPanel orderId={explaining} onClose={() => setExplaining(null)} />
    </>
  );
}

/** Market (DESIGN §4): a bot's candles, indicators and trades, live, each trade explained (D46). */
export default function MarketPage() {
  // useSearchParams needs a Suspense boundary, so the page can still prerender.
  return (
    <Suspense>
      <Market />
    </Suspense>
  );
}
