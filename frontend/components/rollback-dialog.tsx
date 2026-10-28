"use client";

import { Undo2 } from "lucide-react";
import { useRef, useState } from "react";

import { SwitchPolicyFields } from "@/components/switch-policy-fields";
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
import { ApiError, rollback } from "@/lib/api";
import {
  describeParams,
  type BotReply,
  type BotSetup,
  type ConfigEntry,
  type SwitchPolicy,
} from "@/lib/bots";

/**
 * Rolling a bot back to one of its earlier strategy configs (P6-2, D52), from its detail
 * page: a dialog with focus on Cancel, saying what it runs now and what it goes back to,
 * with the switch policy for its open position. The engine adds a config copying the old
 * one and switches to it as Edit does, at the bot's next candle; its refusal is shown here.
 */
export function RollbackDialog({
  bot,
  config,
  holding,
  disabled,
  onRolledBack,
}: {
  bot: BotSetup;
  config: ConfigEntry;
  holding: boolean;
  disabled: string | null; // why it can't be rolled back to, or null
  onRolledBack: (reply: BotReply, policy: SwitchPolicy) => void;
}) {
  const [open, setOpen] = useState(false);
  const [policy, setPolicy] = useState<SwitchPolicy>(bot.switch_policy);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Once rolled back, focus goes to the page's message, not back to this button: it's
  // gone once the history reloads (the row it was on is no longer the one to go back to).
  const rolledBack = useRef(false);

  function changeOpen(next: boolean) {
    if (sending) return; // the answer is what you came for
    setOpen(next);
    if (next) {
      setPolicy(bot.switch_policy);
      setError(null);
    }
  }

  async function confirm() {
    setSending(true);
    setError(null);
    try {
      const reply = await rollback(bot.id, { config_id: config.id, policy });
      rolledBack.current = true;
      setSending(false);
      setOpen(false);
      onRolledBack(reply, policy);
    } catch (problem) {
      setSending(false);
      if (!(problem instanceof ApiError)) {
        setError("Couldn't reach the API.");
      } else {
        setError(problem.status === 422 ? `Refused: ${problem.message}` : problem.message);
      }
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={changeOpen}>
      <AlertDialogTrigger asChild>
        <Button
          size="sm"
          variant="outline"
          disabled={disabled !== null}
          title={disabled ?? undefined}
          aria-label={`Roll back ${bot.name} to #${config.number}`}
        >
          <Undo2 aria-hidden />
          Roll back
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent
        onCloseAutoFocus={(event) => {
          if (!rolledBack.current) return; // Cancel: back to the button, as usual
          rolledBack.current = false;
          event.preventDefault();
        }}
      >
        <AlertDialogHeader>
          <AlertDialogTitle>
            Roll back {bot.name} to #{config.number}?
          </AlertDialogTitle>
          <AlertDialogDescription>
            {bot.status === "stopped"
              ? "It's stopped: it starts with these settings."
              : "It trades with these settings from its next candle, with no restart."}{" "}
            They&apos;re added to its history as a new config; nothing is deleted.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-muted-foreground">Now</dt>
          <dd>
            {bot.strategy ?? "—"}{" "}
            <span className="font-mono text-xs">{describeParams(bot.params)}</span>
          </dd>
          <dt className="text-muted-foreground">Back to</dt>
          <dd>
            {config.strategy}{" "}
            <span className="font-mono text-xs">{describeParams(config.params)}</span>
          </dd>
        </dl>
        <SwitchPolicyFields
          idPrefix={`rollback-${config.id}`}
          policy={policy}
          holding={holding}
          onChange={setPolicy}
        />
        {error !== null && (
          <p role="alert" className="text-loss text-sm">
            {error}
          </p>
        )}
        {sending && (
          <p role="status" className="text-sm">
            Rolling back…
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={sending}>Cancel</AlertDialogCancel>
          <Button disabled={sending} onClick={confirm}>
            Roll back
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
