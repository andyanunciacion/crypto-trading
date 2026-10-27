"use client";

import { useState, type ReactNode } from "react";

import { Money } from "@/components/money";
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
import type { BotStatus, Position } from "@/lib/overview";

/**
 * Stopping one bot (D47): a dialog, like the kill switch's (UX1), with focus on Cancel.
 * "Stop" leaves its position open, and then nobody watches its stops, so it says so;
 * "Stop + close position" sells it until it's gone. A stopped bot that still holds a
 * position gets only the close.
 */
export function StopBotDialog({
  name,
  status,
  position,
  disabled,
  onStop,
  children,
}: {
  name: string;
  status: BotStatus;
  position: Position | undefined;
  disabled: boolean;
  onStop: (closePosition: boolean) => void;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const stopped = status === "stopped";
  const coin = position?.symbol.split("/")[0] ?? "";

  function choose(close: boolean) {
    setOpen(false);
    onStop(close);
  }

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>
        <Button
          size="sm"
          variant="outline"
          disabled={disabled}
          aria-label={`${stopped ? "Close the position of" : "Stop"} ${name}`}
        >
          {children}
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {stopped ? `Close ${name}'s position?` : `Stop ${name}?`}
          </AlertDialogTitle>
          <AlertDialogDescription>
            {stopped
              ? "It's stopped already. Closing sells its position until it's gone."
              : "It stops trading at once, and its open orders are cancelled. It stays stopped until you start it again."}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {position !== undefined && (
          <p className="text-sm">
            It holds <Money value={position.amount} currency={coin} min={0} max={8} />
            {!stopped && (
              <>
                . Stop only, and the position stays open with{" "}
                <strong>no one watching its stops</strong> until you start the bot again.
              </>
            )}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          {(stopped || position !== undefined) && (
            <Button
              variant="outline"
              className="border-live text-live hover:text-live"
              onClick={() => choose(true)}
            >
              {stopped ? "Close position" : "Stop + close position"}
            </Button>
          )}
          {!stopped && (
            <Button variant="destructive" onClick={() => choose(false)}>
              Stop
            </Button>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
