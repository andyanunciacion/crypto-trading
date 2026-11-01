"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { AlertSettingsForm } from "@/components/alert-settings-form";
import { ExchangeKeys } from "@/components/exchange-keys";
import { PageTabs } from "@/components/page-tabs";
import { RiskLimitsForm } from "@/components/risk-limits-form";
import { SystemHealth } from "@/components/system-health";

const TABS = [
  { id: "limits", label: "Risk limits" },
  { id: "keys", label: "Exchange keys" },
  { id: "alerts", label: "Alerts" },
  { id: "system", label: "System" },
] as const;
type Tab = (typeof TABS)[number]["id"];

function Settings() {
  const asked = useSearchParams().get("tab");
  const tab: Tab = TABS.find((t) => t.id === asked)?.id ?? "limits";
  return (
    <section aria-labelledby="settings" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 id="settings" className="text-xl font-semibold">
          Settings
        </h1>
        <PageTabs
          label="Settings views"
          tabs={[...TABS]}
          current={tab}
          href={(id) => `/settings?tab=${id}`}
        />
      </div>
      {tab === "limits" ? (
        <RiskLimitsForm />
      ) : tab === "keys" ? (
        <ExchangeKeys />
      ) : tab === "alerts" ? (
        <AlertSettingsForm />
      ) : (
        <SystemHealth />
      )}
      <p className="text-muted-foreground max-w-3xl text-xs">
        Two-factor sign-in is managed on the server: <code>python -m backend.users reset-totp</code>
      </p>
    </section>
  );
}

/**
 * Settings (DESIGN §4, D50, D59, D62): risk limits and circuit breakers, exchange keys,
 * alerts, and every service's state.
 */
export default function SettingsPage() {
  // useSearchParams needs a Suspense boundary, so the page can still prerender.
  return (
    <Suspense>
      <Settings />
    </Suspense>
  );
}
