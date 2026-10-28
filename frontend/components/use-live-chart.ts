"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { ApiError, chart, me } from "@/lib/api";
import type {
  Candle,
  ChartBot,
  ChartUpdate,
  FormingCandle,
  IndicatorPoint,
  Indicators,
  SwitchMarker,
  TradeMarker,
} from "@/lib/market";

/** A bot's chart as it stands: what `GET` sent, plus everything the socket added since. */
export type Live = {
  bot: ChartBot;
  /** The lines' layout; their points are in `points`. */
  indicators: Omit<Indicators, "points"> | null;
  /** Oldest first. Only ever appended to, so the chart can draw just what's new. */
  candles: Candle[];
  points: IndicatorPoint[];
  forming: FormingCandle | null;
  trades: TradeMarker[];
  switches: SwitchMarker[];
};

export type LiveChart = {
  live: Live | null;
  error: string | null;
  /** Whether the socket is open right now. */
  connected: boolean;
  /** The API's clock minus the browser's (ages are worked out against the API's). */
  skewMs: number;
};

const MAX_RETRY_MS = 10_000;
/** The API's close code for a bot that doesn't exist (backend/market/routes.py). */
const NO_SUCH_BOT = 4404;

function apply(live: Live, update: ChartUpdate): Live {
  return {
    ...live,
    candles: update.candles.length ? [...live.candles, ...update.candles] : live.candles,
    points: update.points.length ? [...live.points, ...update.points] : live.points,
    forming: update.forming,
    trades: update.trades ?? live.trades,
    switches: update.switches ?? live.switches,
  };
}

/**
 * Loads a bot's chart, then keeps it current over `/api/ws/bots/{id}/chart` (D46): new
 * closed candles with their indicator points, the forming candle, the trades and the
 * strategy switches. If the socket drops, it reconnects after 1 s, then 2, 4, 8, at most
 * 10, carrying on from the newest candle it has, so none is missed. If the session is
 * over, it goes to sign-in.
 *
 * A new switch (P6-4) loads the chart again: the bot's indicators are the new strategy's
 * from then on, and the socket only ever sends points for the lines it started with.
 */
export function useLiveChart(botId: number | null, limit?: number): LiveChart {
  const router = useRouter();
  const pathname = usePathname();
  const where = useRef(pathname);
  useEffect(() => {
    where.current = pathname;
  }, [pathname]);
  const [state, setState] = useState<LiveChart>({
    live: null,
    error: null,
    connected: false,
    skewMs: 0,
  });
  const [loads, setLoads] = useState(0); // bumped by a new switch: load it all again

  useEffect(() => {
    if (botId === null) return;
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    let unmounted = false;
    // The newest and oldest candle the browser has: where the socket carries on from.
    let after: string | null = null;
    let since: string | null = null;
    // The switches the chart was loaded with; one more means other indicator lines.
    let known: Set<number> | null = null;

    function signIn() {
      router.replace(`/login?next=${encodeURIComponent(where.current)}`);
    }

    function connect() {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const query = new URLSearchParams();
      if (after) query.set("after", after);
      if (since) query.set("since", since);
      const opened = new WebSocket(
        `${scheme}://${window.location.host}/api/ws/bots/${botId}/chart?${query}`,
      );
      socket = opened;
      let wasOpen = false;
      opened.onopen = () => {
        wasOpen = true;
        attempt = 0;
        setState((current) => ({ ...current, connected: true }));
      };
      opened.onmessage = (event: MessageEvent<string>) => {
        const update = JSON.parse(event.data) as ChartUpdate;
        if (known !== null && update.switches?.some((s) => !known!.has(s.id))) {
          setLoads((n) => n + 1);
          return;
        }
        if (update.candles.length) {
          after = update.candles[update.candles.length - 1].ts;
          since ??= update.candles[0].ts;
        }
        const skewMs = Date.parse(update.server_time) - Date.now();
        setState((current) =>
          current.live === null
            ? current
            : { ...current, live: apply(current.live, update), connected: true, skewMs },
        );
      };
      opened.onclose = (event) => {
        if (unmounted) return;
        setState((current) => ({ ...current, connected: false }));
        if (event.code === 1008) return signIn();
        if (event.code === NO_SUCH_BOT) {
          setState((current) => ({ ...current, error: `There's no bot ${botId}.` }));
          return;
        }
        const delay = Math.min(1000 * 2 ** attempt, MAX_RETRY_MS);
        attempt += 1;
        retry = setTimeout(connect, delay);
        // Refused at the handshake, the browser only says 1006. Ask the API why.
        if (!wasOpen) {
          me().catch((error: unknown) => {
            if (error instanceof ApiError && error.status === 401) {
              clearTimeout(retry);
              signIn();
            }
          });
        }
      };
    }

    chart(botId, limit)
      .then((loaded) => {
        if (unmounted) return;
        const lines = loaded.indicators;
        const live: Live = {
          bot: loaded.bot,
          indicators: lines && { pane: lines.pane, names: lines.names, guides: lines.guides },
          candles: loaded.candles,
          points: lines?.points ?? [],
          forming: loaded.forming,
          trades: loaded.trades,
          switches: loaded.switches,
        };
        after = loaded.candles.at(-1)?.ts ?? null;
        since = loaded.candles.at(0)?.ts ?? null;
        known = new Set(loaded.switches.map((s) => s.id));
        setState({
          live,
          error: null,
          connected: false,
          skewMs: Date.parse(loaded.server_time) - Date.now(),
        });
        connect();
      })
      .catch((error: unknown) => {
        if (unmounted) return;
        if (error instanceof ApiError && error.status === 401) return signIn();
        const message = error instanceof Error ? error.message : String(error);
        setState((current) => ({ ...current, error: message }));
      });

    return () => {
      unmounted = true;
      clearTimeout(retry);
      socket?.close();
      setState({ live: null, error: null, connected: false, skewMs: 0 });
    };
  }, [botId, limit, router, loads]);

  return state;
}
