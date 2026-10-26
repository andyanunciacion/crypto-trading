"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { ApiError, me } from "@/lib/api";
import { FEED_STALE_SECONDS, type Overview } from "@/lib/overview";

/** What the Overview socket has received, shared by the header and the pages. */
export type Feed = {
  overview: Overview | null;
  /** Whether the socket is open right now. */
  connected: boolean;
  /** When the last snapshot arrived (the browser's clock, ms). */
  receivedAt: number | null;
  /** The API's clock minus the browser's, so ages don't depend on the browser's clock. */
  skewMs: number;
};

const FeedContext = createContext<Feed | null>(null);

const MAX_RETRY_MS = 10_000;

/**
 * Keeps one WebSocket to `/api/ws/overview` open (through the Next.js rewrite, so the
 * session cookie goes with it) and shares what it receives. If it drops, it reconnects
 * after 1 s, then 2, 4, 8, at most 10. If the session is over, it goes to sign-in.
 */
export function OverviewProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  // Read in the socket's handlers, which outlive a render.
  const where = useRef(pathname);
  useEffect(() => {
    where.current = pathname;
  }, [pathname]);
  const [feed, setFeed] = useState<Feed>({
    overview: null,
    connected: false,
    receivedAt: null,
    skewMs: 0,
  });

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    let unmounted = false;

    function signIn() {
      router.replace(`/login?next=${encodeURIComponent(where.current)}`);
    }

    function connect() {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const opened = new WebSocket(`${scheme}://${window.location.host}/api/ws/overview`);
      socket = opened;
      let wasOpen = false;
      opened.onopen = () => {
        wasOpen = true;
        attempt = 0;
        setFeed((current) => ({ ...current, connected: true }));
      };
      opened.onmessage = (event: MessageEvent<string>) => {
        const overview = JSON.parse(event.data) as Overview;
        const now = Date.now();
        setFeed({
          overview,
          connected: true,
          receivedAt: now,
          skewMs: Date.parse(overview.server_time) - now,
        });
      };
      opened.onclose = (event) => {
        if (unmounted) return;
        setFeed((current) => ({ ...current, connected: false }));
        // Closed after opening with 1008: the session ended (signed out elsewhere, expired).
        if (event.code === 1008) return signIn();
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

    connect();
    return () => {
      unmounted = true;
      clearTimeout(retry);
      socket?.close();
    };
  }, [router]);

  return <FeedContext value={feed}>{children}</FeedContext>;
}

export function useFeed(): Feed {
  const feed = useContext(FeedContext);
  if (feed === null) throw new Error("useFeed needs an OverviewProvider above it");
  return feed;
}

/** The time now, updated every second, so ages count up between snapshots. */
export function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

/** Seconds since `iso`, by the API's clock. */
export function ageSeconds(iso: string, feed: Feed, now: number): number {
  return (now + feed.skewMs - Date.parse(iso)) / 1000;
}

/** Whether the feed itself has gone quiet: then nothing on screen is current. */
export function feedStale(feed: Feed, now: number): boolean {
  return (
    !feed.connected ||
    feed.receivedAt === null ||
    (now - feed.receivedAt) / 1000 > FEED_STALE_SECONDS
  );
}
