"use client";

import { CircleCheck, CircleMinus, CircleX, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";

import { Timestamp } from "@/components/timestamp";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError, servicesHealth } from "@/lib/api";
import { formatAge } from "@/lib/format";
import { HEALTH_REFRESH_MS, type ServiceState, type ServicesHealth } from "@/lib/health";
import { cn } from "@/lib/utils";

const STATES: Record<ServiceState, { word: string; className: string; Icon: typeof CircleCheck }> =
  {
    ok: { word: "OK", className: "text-muted-foreground", Icon: CircleCheck },
    stale: { word: "Stale", className: "text-stale", Icon: TriangleAlert },
    unknown: { word: "Unknown", className: "text-stale", Icon: TriangleAlert },
    down: { word: "Down", className: "text-loss", Icon: CircleX },
    off: { word: "Off", className: "text-muted-foreground", Icon: CircleMinus },
  };

/**
 * Settings → System (D62): every service's state, asked again every 5 s. The state is a
 * word with an icon, never a colour alone. The engine, backtester and alerts service are
 * judged by their heartbeats in Redis; the database and Redis are checked live.
 */
export function SystemHealth() {
  const [health, setHealth] = useState<ServicesHealth | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const loaded = await servicesHealth();
        if (!current) return;
        setHealth(loaded);
        setProblem(null);
      } catch (error) {
        if (!current) return;
        setProblem(
          error instanceof ApiError
            ? `The API answered ${error.status}: ${error.message}`
            : "Couldn't reach the API: it may be down.",
        );
      }
      if (current) timer = setTimeout(() => void load(), HEALTH_REFRESH_MS);
    }
    void load();
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, []);

  if (health === null) {
    return problem ? (
      <p role="alert" className="text-loss text-sm">
        {problem}
      </p>
    ) : (
      <p className="text-muted-foreground text-sm">Loading…</p>
    );
  }

  return (
    <div className="flex max-w-4xl flex-col gap-3">
      <Table aria-label="Services">
        <TableHeader>
          <TableRow>
            <TableHead>Service</TableHead>
            <TableHead>State</TableHead>
            <TableHead>Last seen</TableHead>
            <TableHead>Detail</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {health.services.map((service) => {
            const { word, className, Icon } = STATES[service.state];
            return (
              <TableRow key={service.name} data-service={service.name}>
                <TableCell className="font-medium">{service.label}</TableCell>
                <TableCell>
                  <span className={cn("flex items-center gap-1.5", className)}>
                    <Icon aria-hidden className="size-4" />
                    {word}
                  </span>
                </TableCell>
                <TableCell className="text-muted-foreground tabular-nums">
                  {service.age_s === null ? "—" : `${formatAge(service.age_s)} ago`}
                </TableCell>
                <TableCell className="text-sm whitespace-normal">
                  {service.detail}
                  {service.since !== null && (
                    <span className="text-muted-foreground">
                      {service.detail ? " · " : ""}up since <Timestamp iso={service.since} />
                    </span>
                  )}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      <p role="status" className={cn("text-xs", problem ? "text-loss" : "text-muted-foreground")}>
        {problem ?? (
          <>
            Checked <Timestamp iso={health.checked_at} />; refreshes every{" "}
            {HEALTH_REFRESH_MS / 1000} s. <code>docker compose ps</code> shows Docker&apos;s view.
          </>
        )}
      </p>
    </div>
  );
}
