"use client";

import { OctagonX } from "lucide-react";
import { useState } from "react";

import { useFeed } from "@/components/overview-provider";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { ApiError, kill, type KillReply } from "@/lib/api";
import { formatDecimal } from "@/lib/format";

type State =
  | { step: "asking" }
  | { step: "sending"; closePositions: boolean }
  | { step: "done"; reply: KillReply }
  | { step: "failed"; message: string };

// Full red in both themes: the destructive variant fades to 60% in dark mode, and the
// kill switch must stand out on every screen (UX1).
const LIVE_RED = "bg-live text-live-foreground hover:bg-live/90 dark:bg-live dark:hover:bg-live/90";

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/**
 * The kill switch (UX1, D43): a red button on every screen. One click opens a dialog
 * with the two choices, so a stray click can't trigger it; focus starts on Cancel. The
 * engine's reply is shown in the dialog.
 */
export function KillSwitch() {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<State>({ step: "asking" });
  const bots = useFeed().overview?.bots ?? [];

  function botName(id: number): string {
    return bots.find((bot) => bot.id === id)?.name ?? `bot ${id}`;
  }

  async function trip(closePositions: boolean) {
    setState({ step: "sending", closePositions });
    try {
      setState({ step: "done", reply: await kill(closePositions) });
    } catch (error) {
      setState({
        step: "failed",
        message:
          error instanceof ApiError
            ? error.message
            : "Couldn't reach the API. Use the CLI: docker compose run --rm -T engine python -m engine.control.kill",
      });
    }
  }

  function changeOpen(next: boolean) {
    // Not while it's being sent: the answer is what you came for.
    if (state.step === "sending") return;
    setOpen(next);
    if (next) setState({ step: "asking" });
  }

  return (
    <AlertDialog open={open} onOpenChange={changeOpen}>
      <AlertDialogTrigger asChild>
        <Button variant="destructive" size="sm" className={`${LIVE_RED} font-semibold`}>
          <OctagonX aria-hidden />
          Kill switch
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Kill switch</AlertDialogTitle>
          <AlertDialogDescription>
            Stops every bot and cancels every open order. Bots stay stopped until you start them
            again.
          </AlertDialogDescription>
        </AlertDialogHeader>

        {state.step === "sending" && (
          <p role="status" className="text-sm">
            {state.closePositions ? "Stopping and closing positions…" : "Stopping…"}
          </p>
        )}
        {state.step === "failed" && (
          <p role="alert" className="text-loss text-sm">
            {state.message}
          </p>
        )}
        {state.step === "done" && <Outcome reply={state.reply} botName={botName} />}

        <AlertDialogFooter>
          {state.step === "asking" || state.step === "sending" ? (
            <>
              <AlertDialogCancel disabled={state.step === "sending"}>Cancel</AlertDialogCancel>
              <Button
                variant="outline"
                className="border-live text-live hover:text-live"
                disabled={state.step === "sending"}
                onClick={() => trip(true)}
              >
                Stop all + close positions
              </Button>
              <Button
                variant="destructive"
                className={LIVE_RED}
                disabled={state.step === "sending"}
                onClick={() => trip(false)}
              >
                Stop all bots
              </Button>
            </>
          ) : (
            <AlertDialogCancel>Close</AlertDialogCancel>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function Outcome({ reply, botName }: { reply: KillReply; botName: (id: number) => string }) {
  return (
    <div role="status" className="flex flex-col gap-2 text-sm">
      {reply.ok ? (
        <p className="font-medium">Done in {reply.took_ms} ms.</p>
      ) : (
        <p className="text-loss font-medium">The kill switch failed partway: {reply.error}</p>
      )}
      <ul className="list-disc pl-5">
        <li>
          Stopped {plural(reply.stopped.length, "bot", "bots")}
          {reply.stopped.length > 0 && `: ${reply.stopped.map(botName).join(", ")}`}
        </li>
        <li>Cancelled {plural(reply.canceled.length, "order", "orders")}</li>
        {reply.closing.length > 0 && (
          <li>
            Selling {plural(reply.closing.length, "position", "positions")} until gone:{" "}
            {reply.closing
              .map(
                (exit) =>
                  `${exit.symbol} (${botName(exit.bot_id)}), ${formatDecimal(exit.filled ?? "0", { min: 0, max: 8 })} of ${formatDecimal(exit.amount, { min: 0, max: 8 })} sold`,
              )
              .join("; ")}
          </li>
        )}
      </ul>
      {reply.not_closed.length > 0 && (
        <div role="alert" className="text-loss">
          Couldn&apos;t close:
          <ul className="list-disc pl-5">
            {reply.not_closed.map((left) => (
              <li key={`${left.bot_id}-${left.symbol}`}>
                {left.symbol} ({botName(left.bot_id)}): {left.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
