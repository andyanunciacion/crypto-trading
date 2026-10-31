"use client";

import { TriangleAlert } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { BotWhy, StatusBadge } from "@/components/bot-status";
import { Money, Percent } from "@/components/money";
import { ModeBadge } from "@/components/mode-badge";
import { ageSeconds, feedStale, useFeed, useNow } from "@/components/overview-provider";
import { Timestamp } from "@/components/timestamp";
import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatAge } from "@/lib/format";
import { PRICE_STALE_SECONDS } from "@/lib/overview";
import { cn } from "@/lib/utils";

/** Coins: up to 8 decimals, none forced. */
const COIN = { min: 0, max: 8 };

function StaleLabel({ children }: { children: ReactNode }) {
  return (
    <span className="text-stale inline-flex items-center gap-1 text-xs font-semibold">
      <TriangleAlert aria-hidden className="size-3.5" />
      {children}
    </span>
  );
}

function Stat({
  title,
  children,
  note,
  stale,
}: {
  title: string;
  children: ReactNode;
  note?: ReactNode;
  stale: boolean;
}) {
  return (
    <Card className={cn("gap-2 py-4", stale && "opacity-60")}>
      <CardHeader className="px-4">
        <CardDescription>{title}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-1 px-4">
        <div className="text-2xl font-semibold">{children}</div>
        {note && <div className="text-muted-foreground text-xs">{note}</div>}
      </CardContent>
    </Card>
  );
}

/** Overview (DESIGN §4): the account, open positions and every bot, live (D45). */
export default function OverviewPage() {
  const feed = useFeed();
  const now = useNow();
  const overview = feed.overview;

  if (overview === null) {
    return <p className="text-muted-foreground">Connecting to the live feed…</p>;
  }

  const { account, positions, bots } = overview;
  const disconnected = feedStale(feed, now);
  const priceAge = (ts: string | null) => (ts === null ? null : ageSeconds(ts, feed, now));
  const stalePrice = (ts: string | null) => {
    const age = priceAge(ts);
    return age === null || age > PRICE_STALE_SECONDS;
  };
  // Equity values each position at its bid: one stale bid makes it stale too.
  const accountStale = disconnected || positions.some((p) => stalePrice(p.price_ts));
  const currency = account.currency;

  return (
    <>
      <section aria-labelledby="account" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 id="account" className="text-xl font-semibold">
            Paper account
          </h1>
          <span className="text-muted-foreground text-sm">
            every paper bot together, as the risk limits see it
          </span>
          {accountStale && <StaleLabel>stale: a price is old or missing</StaleLabel>}
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Stat title="Equity" stale={accountStale}>
            <Money value={account.equity} currency={currency} />
          </Stat>
          <Stat
            title="Today's P&L"
            stale={accountStale}
            note={
              <>
                <Percent value={account.today_pnl_pct} pnl /> since midnight in Manila
              </>
            }
          >
            <Money value={account.today_pnl} currency={currency} pnl />
          </Stat>
          <Stat
            title="Total P&L"
            stale={accountStale}
            note={
              <>
                <Percent value={account.total_pnl_pct} pnl /> on{" "}
                <Money value={account.deposits} currency={currency} /> deposited
              </>
            }
          >
            <Money value={account.total_pnl} currency={currency} pnl />
          </Stat>
          <Stat
            title="Drawdown"
            stale={accountStale}
            note={
              <>
                below the peak of <Money value={account.peak} currency={currency} />
              </>
            }
          >
            <Percent value={account.drawdown_pct} />
          </Stat>
        </div>
      </section>

      <section aria-labelledby="positions" className="flex flex-col gap-3">
        <h2 id="positions" className="text-lg font-semibold">
          Open positions
        </h2>
        {positions.length === 0 ? (
          <p className="text-muted-foreground text-sm">No open positions.</p>
        ) : (
          <Card className="py-2">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Bot</TableHead>
                  <TableHead>Symbol</TableHead>
                  <TableHead className="text-right">Amount</TableHead>
                  <TableHead className="text-right">Entry</TableHead>
                  <TableHead className="text-right">Bid</TableHead>
                  <TableHead className="text-right">Value</TableHead>
                  <TableHead className="text-right">P&L</TableHead>
                  <TableHead className="text-right">Stop</TableHead>
                  <TableHead className="text-right">Take-profit</TableHead>
                  <TableHead>Opened</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {positions.map((p) => {
                  const [coin, quote] = p.symbol.split("/");
                  const age = priceAge(p.price_ts);
                  const stale = disconnected || stalePrice(p.price_ts);
                  return (
                    <TableRow key={`${p.bot_id}-${p.symbol}`} className={cn(stale && "opacity-70")}>
                      <TableCell>{p.bot_name}</TableCell>
                      <TableCell>{p.symbol}</TableCell>
                      <TableCell className="text-right">
                        <Money value={p.amount} currency={coin} {...COIN} />
                      </TableCell>
                      <TableCell className="text-right">
                        <Money value={p.avg_entry} currency={quote} max={8} />
                      </TableCell>
                      <TableCell className="text-right">
                        {p.bid === null ? (
                          <StaleLabel>no price</StaleLabel>
                        ) : (
                          <div className="flex flex-col items-end">
                            <Money value={p.bid} currency={quote} max={8} />
                            {age !== null &&
                              (stale ? (
                                <StaleLabel>stale, {formatAge(age)} old</StaleLabel>
                              ) : (
                                <span className="text-muted-foreground text-xs">
                                  {formatAge(age)} ago
                                </span>
                              ))}
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        <Money value={p.value} currency={quote} />
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex flex-col items-end">
                          <Money value={p.pnl} currency={quote} pnl />
                          <Percent value={p.pnl_pct} pnl className="text-xs" />
                        </div>
                      </TableCell>
                      <TableCell className="text-right">
                        {p.stop === null ? "—" : <Money value={p.stop} currency={quote} max={8} />}
                      </TableCell>
                      <TableCell className="text-right">
                        {p.take_profit === null ? (
                          "—"
                        ) : (
                          <Money value={p.take_profit} currency={quote} max={8} />
                        )}
                      </TableCell>
                      <TableCell>
                        <Timestamp iso={p.opened_at} />
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </Card>
        )}
      </section>

      <section aria-labelledby="bots" className="flex flex-col gap-3">
        <h2 id="bots" className="text-lg font-semibold">
          Bots
        </h2>
        {bots.length === 0 ? (
          <p className="text-muted-foreground text-sm">
            No bots yet: add one on the{" "}
            <Link href="/bots" className="underline underline-offset-4">
              Bots
            </Link>{" "}
            page.
          </p>
        ) : (
          <Card className={cn("py-2", disconnected && "opacity-60")}>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Mode</TableHead>
                  <TableHead>Symbol</TableHead>
                  <TableHead>Timeframe</TableHead>
                  <TableHead>Strategy</TableHead>
                  <TableHead>Why</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {bots.map((bot) => (
                  <TableRow key={bot.id} data-bot={bot.name}>
                    <TableCell className="font-medium">
                      <Link
                        href={`/bots/${bot.id}`}
                        className="decoration-muted-foreground hover:decoration-foreground underline underline-offset-4"
                      >
                        {bot.name}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={bot.status} />
                    </TableCell>
                    <TableCell>
                      <ModeBadge mode={bot.mode} />
                    </TableCell>
                    <TableCell>{bot.symbol}</TableCell>
                    <TableCell>{bot.timeframe}</TableCell>
                    <TableCell>{bot.strategy ?? "—"}</TableCell>
                    <TableCell className="text-muted-foreground max-w-md truncate text-xs">
                      <BotWhy bot={bot} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        )}
      </section>
    </>
  );
}
