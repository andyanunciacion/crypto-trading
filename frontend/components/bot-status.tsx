import type { ReactNode } from "react";

import { Timestamp } from "@/components/timestamp";
import { Badge } from "@/components/ui/badge";
import type { BotStatus } from "@/lib/overview";

const STATUS_STYLE: Record<BotStatus, string> = {
  running: "border-profit text-profit",
  paused: "border-stale text-stale",
  stopped: "text-muted-foreground",
};

/** A bot's status, as a word (never color alone, DESIGN §10). */
export function StatusBadge({ status }: { status: BotStatus }) {
  return (
    <Badge variant="outline" className={STATUS_STYLE[status]}>
      {status}
    </Badge>
  );
}

type Why = {
  status: BotStatus;
  paused_reason: string | null;
  paused_at: string | null;
  stopped_reason: string | null;
  stopped_at: string | null;
};

/** Why a bot is paused or stopped, and since when; nothing for a running one. */
export function BotWhy({ bot }: { bot: Why }): ReactNode {
  if (bot.status === "paused" && bot.paused_at) {
    return (
      <>
        {bot.paused_reason ?? "paused"} · since <Timestamp iso={bot.paused_at} />
      </>
    );
  }
  if (bot.status === "stopped" && bot.stopped_at) {
    return (
      <>
        {bot.stopped_reason ?? "stopped"} · since <Timestamp iso={bot.stopped_at} />
      </>
    );
  }
  return null;
}
