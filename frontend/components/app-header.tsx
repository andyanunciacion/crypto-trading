"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { FreshnessIndicator } from "@/components/freshness-indicator";
import { KillSwitch } from "@/components/kill-switch";
import { ModeBadge } from "@/components/mode-badge";
import { useFeed } from "@/components/overview-provider";
import { ThemeToggle } from "@/components/theme-toggle";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApiError, me, signOut } from "@/lib/api";
import { cn } from "@/lib/utils";

// The screens built so far; the others join as their slices land (DESIGN §4).
const NAV = [
  { href: "/", label: "Overview" },
  { href: "/market", label: "Market" },
  { href: "/bots", label: "Bots" },
];

/**
 * On every screen (DESIGN §4, UX principle 1): the mode, how fresh the data is, and the
 * kill switch, plus who's signed in.
 */
export function AppHeader() {
  const router = useRouter();
  const pathname = usePathname();
  const mode = useFeed().overview?.mode;
  const [username, setUsername] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    me()
      .then((user) => {
        if (current) setUsername(user.username);
      })
      .catch((error: unknown) => {
        // The session expired or was ended elsewhere: sign in again.
        if (current && error instanceof ApiError && error.status === 401) {
          router.replace(`/login?next=${encodeURIComponent(pathname)}`);
        }
      });
    return () => {
      current = false;
    };
  }, [router, pathname]);

  async function signOutEverywhere() {
    await signOut().catch(() => undefined); // signed out already, or the API is down
    router.replace("/login");
  }

  return (
    <header className="bg-background/95 sticky top-0 z-40 border-b backdrop-blur">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
        <div className="flex items-center gap-3">
          <span className="font-semibold">crypto-trading</span>
          {mode ? (
            <ModeBadge mode={mode} />
          ) : (
            <Badge variant="outline" className="text-muted-foreground font-mono">
              MODE …
            </Badge>
          )}
        </div>
        <nav aria-label="Main" className="flex items-center gap-1 text-sm">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              aria-current={pathname === item.href ? "page" : undefined}
              className={cn(
                "hover:bg-accent rounded-md px-3 py-1.5",
                pathname === item.href ? "bg-accent font-medium" : "text-muted-foreground",
              )}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <FreshnessIndicator />
        <div className="ml-auto flex items-center gap-2">
          <KillSwitch />
          <ThemeToggle />
          {username && (
            <>
              <span className="text-muted-foreground hidden text-sm sm:inline">
                Signed in as {username}
              </span>
              <Button variant="outline" size="sm" onClick={signOutEverywhere}>
                Sign out
              </Button>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
