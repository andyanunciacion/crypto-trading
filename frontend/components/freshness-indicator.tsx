"use client";

import { CircleCheck, TriangleAlert } from "lucide-react";

import { ageSeconds, feedStale, useFeed, useNow, type Feed } from "@/components/overview-provider";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatAge } from "@/lib/format";
import { ENGINE_STALE_SECONDS, PRICE_STALE_SECONDS, type Overview } from "@/lib/overview";
import { cn } from "@/lib/utils";

type Item = { label: string; value: string; stale: boolean; detail: string };

function engineItem(overview: Overview, feed: Feed, now: number): Item {
  const beat = overview.engine;
  if (beat === null) {
    return { label: "Engine", value: "never seen", stale: true, detail: "No heartbeat yet." };
  }
  const age = ageSeconds(beat.ts, feed, now);
  return {
    label: "Engine",
    value: `${formatAge(age)} ago`,
    stale: age > ENGINE_STALE_SECONDS,
    detail: `Heartbeat every 10 s; stale after ${ENGINE_STALE_SECONDS} s.`,
  };
}

function pricesItem(overview: Overview, feed: Feed, now: number): Item {
  const ages = overview.prices.map((price) => ({
    symbol: price.symbol,
    age: price.ts === null ? null : ageSeconds(price.ts, feed, now),
  }));
  const detail = ages
    .map(({ symbol, age }) => `${symbol}: ${age === null ? "no price" : `${formatAge(age)} ago`}`)
    .join(" · ");
  if (ages.length === 0) {
    return { label: "Prices", value: "none watched", stale: false, detail: "No prices streamed." };
  }
  if (ages.some(({ age }) => age === null)) {
    return { label: "Prices", value: "missing", stale: true, detail };
  }
  // The oldest one: a stuck stream shouldn't hide behind a busy one.
  const oldest = Math.max(...ages.map(({ age }) => age ?? 0));
  return {
    label: "Prices",
    value: `${formatAge(oldest)} ago`,
    stale: oldest > PRICE_STALE_SECONDS,
    detail: `${detail}. Stale after ${PRICE_STALE_SECONDS} s.`,
  };
}

/** How old the engine's heartbeat and the prices are (UX5); stale ones say so. */
export function FreshnessIndicator() {
  const feed = useFeed();
  const now = useNow();
  const overview = feed.overview;
  const disconnected = feedStale(feed, now);
  const items: Item[] = [];
  if (disconnected) {
    items.push({
      label: "Live feed",
      value: overview === null ? "connecting…" : "reconnecting…",
      stale: overview !== null,
      detail: "The numbers on screen stop updating until it's back.",
    });
  }
  if (overview !== null)
    items.push(engineItem(overview, feed, now), pricesItem(overview, feed, now));

  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs" aria-label="Freshness">
      {items.map((item) => (
        <li key={item.label}>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className={cn(
                  "flex items-center gap-1 rounded-sm",
                  item.stale ? "text-stale" : "text-muted-foreground",
                  disconnected && item.label !== "Live feed" && "opacity-50",
                )}
              >
                {item.stale ? (
                  <TriangleAlert aria-hidden className="size-3.5" />
                ) : (
                  <CircleCheck aria-hidden className="size-3.5" />
                )}
                <span>
                  {item.label}: <span className="tabular-nums">{item.value}</span>
                </span>
                {item.stale && <span className="font-semibold uppercase">stale</span>}
              </button>
            </TooltipTrigger>
            <TooltipContent>{item.detail}</TooltipContent>
          </Tooltip>
        </li>
      ))}
    </ul>
  );
}
