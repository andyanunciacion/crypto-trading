"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { ExchangeKeys } from "@/components/exchange-keys";
import { PageTabs } from "@/components/page-tabs";
import { RiskLimitsForm } from "@/components/risk-limits-form";

const TABS = [
  { id: "limits", label: "Risk limits" },
  { id: "keys", label: "Exchange keys" },
];

function Settings() {
  const tab = useSearchParams().get("tab") === "keys" ? "keys" : "limits";
  return (
    <section aria-labelledby="settings" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 id="settings" className="text-xl font-semibold">
          Settings
        </h1>
        <PageTabs
          label="Settings views"
          tabs={TABS}
          current={tab}
          href={(id) => `/settings?tab=${id}`}
        />
      </div>
      {tab === "limits" ? <RiskLimitsForm /> : <ExchangeKeys />}
      <p className="text-muted-foreground max-w-3xl text-xs">
        Alerts come with Telegram (P7). Two-factor sign-in is managed on the server:{" "}
        <code>python -m backend.users reset-totp</code>.
      </p>
    </section>
  );
}

/** Settings (DESIGN §4, D50): the risk limits and circuit breakers, and exchange keys. */
export default function SettingsPage() {
  // useSearchParams needs a Suspense boundary, so the page can still prerender.
  return (
    <Suspense>
      <Settings />
    </Suspense>
  );
}
