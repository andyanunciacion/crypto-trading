import Link from "next/link";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * A page's views as links that set `?tab=` (History, Logs), so a view can be linked to
 * and survives a reload. Links, not ARIA tabs: each is a different query of the page.
 */
export function PageTabs({
  label,
  tabs,
  current,
  href,
}: {
  label: string;
  tabs: { id: string; label: string }[];
  current: string;
  href: (id: string) => string;
}) {
  return (
    <nav aria-label={label} className="bg-muted inline-flex w-fit rounded-lg p-1 text-sm">
      {tabs.map((tab) => (
        <Link
          key={tab.id}
          href={href(tab.id)}
          replace
          aria-current={tab.id === current ? "page" : undefined}
          className={cn(
            "rounded-md px-3 py-1",
            tab.id === current
              ? "bg-background font-medium shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {tab.label}
        </Link>
      ))}
    </nav>
  );
}

/** A labelled filter control in a filter bar. */
export function Filter({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted-foreground text-xs">{label}</span>
      {children}
    </label>
  );
}
