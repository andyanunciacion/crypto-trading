"use client";

import { Shuffle } from "lucide-react";
import { useRef, useState, type FormEvent } from "react";

import {
  AutoSwitchFields,
  autoSwitchPayload,
  initialAutoSwitch,
} from "@/components/auto-switch-fields";
import { SwitchPolicyFields } from "@/components/switch-policy-fields";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { ApiError, catalog as fetchCatalog, setAutoSwitch } from "@/lib/api";
import {
  type AutoSwitchSettings,
  type BotReply,
  type BotSetup,
  type Catalog,
  type SwitchPolicy,
} from "@/lib/bots";

/**
 * Auto-switching (P6-4, D54), from the bot's page: a strategy for each regime, how many
 * closed candles in a row a new regime must read before the bot switches, and what a
 * switch does with an open position. A side sheet like Edit, drawn from the engine's
 * own strategies; the engine checks every regime's params and refuses with the reason.
 */
export function AutoSwitchSheet({
  bot,
  holding,
  onSaved,
}: {
  bot: BotSetup;
  holding: boolean;
  onSaved: (reply: BotReply, settings: AutoSwitchSettings) => void;
}) {
  const [open, setOpen] = useState(false);
  const [opened, setOpened] = useState(0); // a fresh form each time
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const saved = useRef(false);
  const on = bot.auto_switch !== null;

  async function changeOpen(next: boolean) {
    if (sending) return; // the answer is what you came for
    setOpen(next);
    if (!next) return;
    setOpened((n) => n + 1);
    setLoadError(null);
    try {
      setCatalog(await fetchCatalog());
    } catch (problem) {
      setLoadError(problem instanceof ApiError ? problem.message : "Couldn't reach the API.");
    }
  }

  return (
    <Sheet open={open} onOpenChange={changeOpen}>
      <SheetTrigger asChild>
        <Button
          size="sm"
          variant="outline"
          aria-label={`${on ? "Change" : "Set up"} auto-switching`}
        >
          <Shuffle aria-hidden />
          {on ? "Change" : "Set up"}
        </Button>
      </SheetTrigger>
      <SheetContent
        className="sm:max-w-xl"
        onCloseAutoFocus={(event) => {
          // After a save the page focuses its result message (as after a rollback); the
          // button that opened this may be gone. Cancel goes back to it, as usual.
          if (!saved.current) return;
          saved.current = false;
          event.preventDefault();
        }}
      >
        <SheetHeader>
          <SheetTitle>Auto-switching for {bot.name}</SheetTitle>
          <SheetDescription>
            On each closed candle, the engine reads the market&apos;s regime from ADX, ATR and the
            EMA&apos;s slope. Once a new regime has held for the minimum number of candles in a row,
            the bot switches to that regime&apos;s strategy, as Edit would. While the reading is
            unclear (ADX between 20 and 25, or the signs disagree), it keeps the strategy it has.
          </SheetDescription>
        </SheetHeader>
        {loadError !== null ? (
          <p role="alert" className="text-loss text-sm">
            {loadError}
          </p>
        ) : catalog === null ? (
          <p role="status" className="text-muted-foreground text-sm">
            Loading the engine&apos;s strategies…
          </p>
        ) : (
          <AutoSwitchForm
            key={opened}
            bot={bot}
            holding={holding}
            catalog={catalog}
            onSending={setSending}
            onCancel={() => setOpen(false)}
            onSaved={(reply, settings) => {
              saved.current = true;
              setOpen(false);
              onSaved(reply, settings);
            }}
          />
        )}
      </SheetContent>
    </Sheet>
  );
}

function AutoSwitchForm({
  bot,
  holding,
  catalog,
  onSending,
  onCancel,
  onSaved,
}: {
  bot: BotSetup;
  holding: boolean;
  catalog: Catalog;
  onSending: (sending: boolean) => void;
  onCancel: () => void;
  onSaved: (reply: BotReply, settings: AutoSwitchSettings) => void;
}) {
  const [inputs, setInputs] = useState(() => initialAutoSwitch(catalog, bot.auto_switch));
  const [policy, setPolicy] = useState<SwitchPolicy>(bot.auto_switch?.policy ?? bot.switch_policy);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const payload = autoSwitchPayload(catalog, inputs);

  function sendingNow(value: boolean) {
    setSending(value);
    onSending(value);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (payload === null) return;
    const settings: AutoSwitchSettings = {
      ...payload,
      policy,
      // Kept as stored: the dashboard doesn't edit the detector's settings yet.
      ...(bot.auto_switch?.detector ? { detector: bot.auto_switch.detector } : {}),
    };
    sendingNow(true);
    setError(null);
    try {
      const reply = await setAutoSwitch(bot.id, settings);
      sendingNow(false);
      onSaved(reply, settings);
    } catch (problem) {
      sendingNow(false);
      if (!(problem instanceof ApiError)) {
        setError("Couldn't reach the API.");
      } else {
        setError(problem.status === 422 ? `Refused: ${problem.message}` : problem.message);
      }
    }
  }

  return (
    <form
      onSubmit={submit}
      className="flex flex-col gap-6"
      aria-label={`Auto-switching for ${bot.name}`}
    >
      <AutoSwitchFields
        idPrefix="auto"
        catalog={catalog}
        inputs={inputs}
        onChange={setInputs}
        timeframe={bot.timeframe}
        switcher="the bot"
      />

      <SwitchPolicyFields idPrefix="auto" policy={policy} holding={holding} onChange={setPolicy} />

      {error !== null && (
        <p role="alert" className="text-loss text-sm">
          {error}
        </p>
      )}
      {sending && (
        <p role="status" className="text-sm">
          Saving…
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" disabled={sending} onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" disabled={payload === null || sending}>
          {bot.auto_switch === null ? "Turn on" : "Save"}
        </Button>
      </div>
    </form>
  );
}
