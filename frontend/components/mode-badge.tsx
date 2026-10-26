import { Badge } from "@/components/ui/badge";
import type { Mode } from "@/lib/overview";
import { cn } from "@/lib/utils";

/**
 * PAPER or LIVE, as text as well as color (UX2): PAPER neutral, LIVE high-contrast red,
 * since red is kept for danger. Used for the global mode and for each bot.
 */
export function ModeBadge({ mode, className }: { mode: Mode; className?: string }) {
  return (
    <Badge
      className={cn(
        "font-mono tracking-wider",
        mode === "live"
          ? "bg-live text-live-foreground font-bold"
          : "bg-paper text-paper-foreground border-border",
        className,
      )}
    >
      {mode.toUpperCase()}
    </Badge>
  );
}
