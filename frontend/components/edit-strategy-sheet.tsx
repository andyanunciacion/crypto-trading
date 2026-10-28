"use client";

import { Pencil } from "lucide-react";
import { useState, type FormEvent } from "react";

import { allValid } from "@/components/strategy-setup";
import { StrategyParamsForm } from "@/components/strategy-params-form";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { ApiError, catalog as fetchCatalog, switchStrategy } from "@/lib/api";
import {
  SWITCH_POLICIES,
  initialValues,
  toSettings,
  type BotReply,
  type BotSetup,
  type Catalog,
  type Choice,
  type StrategySwitch,
  type SwitchPolicy,
} from "@/lib/bots";

/**
 * Switching a bot's strategy or params (P6-1, D51), from the Bots page: a side sheet
 * like "New bot", with the engine's own strategies, starting from what the bot runs
 * now. The policy says what happens to an open position, and becomes the bot's. The
 * engine checks everything, keeps the old config, and switches at the bot's next
 * candle, with no restart.
 */
export function EditStrategySheet({
  bot,
  holding,
  disabled,
  onSwitched,
}: {
  bot: BotSetup;
  holding: boolean;
  disabled: boolean;
  onSwitched: (reply: BotReply, change: StrategySwitch) => void;
}) {
  const [open, setOpen] = useState(false);
  const [opened, setOpened] = useState(0); // a fresh form each time
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

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
          disabled={disabled}
          aria-label={`Switch ${bot.name}'s strategy`}
        >
          <Pencil aria-hidden />
          Edit
        </Button>
      </SheetTrigger>
      <SheetContent className="sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>Switch {bot.name}&apos;s strategy</SheetTitle>
          <SheetDescription>
            {bot.status === "stopped"
              ? "It's stopped: it starts with the new strategy."
              : "It trades with the new strategy from its next candle, with no restart."}{" "}
            The old settings are kept in its history.
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
          <SwitchForm
            key={opened}
            bot={bot}
            holding={holding}
            catalog={catalog}
            onSending={setSending}
            onCancel={() => setOpen(false)}
            onSwitched={(reply, change) => {
              setOpen(false);
              onSwitched(reply, change);
            }}
          />
        )}
      </SheetContent>
    </Sheet>
  );
}

/** The inputs for `choice`'s params: the bot's own values where it has them. */
function valuesFor(choice: Choice | undefined, params: Record<string, unknown> | null) {
  const fields = choice?.fields ?? [];
  const values = initialValues(fields);
  for (const field of fields) {
    const value = params?.[field.name];
    if (typeof value === "number" || typeof value === "string") values[field.name] = String(value);
  }
  return values;
}

function SwitchForm({
  bot,
  holding,
  catalog,
  onSending,
  onCancel,
  onSwitched,
}: {
  bot: BotSetup;
  holding: boolean;
  catalog: Catalog;
  onSending: (sending: boolean) => void;
  onCancel: () => void;
  onSwitched: (reply: BotReply, change: StrategySwitch) => void;
}) {
  const current = catalog.strategies.find((s) => s.name === bot.strategy);
  const [strategy, setStrategy] = useState(current?.name ?? catalog.strategies[0]?.name ?? "");
  const [values, setValues] = useState(() => valuesFor(current, bot.params));
  const [policy, setPolicy] = useState<SwitchPolicy>(bot.switch_policy);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const chosen = catalog.strategies.find((s) => s.name === strategy);
  const fields = chosen?.fields ?? [];
  const valid = chosen !== undefined && allValid(fields, values);
  // What it runs now, as the inputs would show it: then only the policy can change.
  const same =
    strategy === bot.strategy &&
    fields.every((f) => values[f.name]?.trim() === valuesFor(current, bot.params)[f.name]);
  const unchanged = same && policy === bot.switch_policy;

  function sendingNow(value: boolean) {
    setSending(value);
    onSending(value);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid || unchanged) return;
    const change: StrategySwitch = { strategy, params: toSettings(fields, values), policy };
    sendingNow(true);
    setError(null);
    try {
      const reply = await switchStrategy(bot.id, change);
      sendingNow(false);
      onSwitched(reply, change);
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
    <form onSubmit={submit} className="flex flex-col gap-6" aria-label={`Switch ${bot.name}`}>
      <fieldset className="flex flex-col gap-3">
        <legend className="mb-2 text-sm font-semibold">Strategy</legend>
        <NativeSelect
          aria-label="Strategy"
          value={strategy}
          onChange={(event) => {
            const next = event.target.value;
            setStrategy(next);
            const choice = catalog.strategies.find((s) => s.name === next);
            setValues(
              next === bot.strategy ? valuesFor(choice, bot.params) : valuesFor(choice, null),
            );
          }}
        >
          {catalog.strategies.map((s) => (
            <option key={s.name} value={s.name}>
              {s.title}
              {s.name === bot.strategy ? " (now)" : ""}
            </option>
          ))}
        </NativeSelect>
        {chosen && (
          <StrategyParamsForm
            idPrefix="switch"
            fields={fields}
            values={values}
            onChange={setValues}
          />
        )}
      </fieldset>

      <fieldset className="flex flex-col gap-2" aria-describedby="switch-policy-note">
        <legend className="mb-2 text-sm font-semibold">Its open position</legend>
        <p id="switch-policy-note" className="text-muted-foreground -mt-2 text-xs">
          {holding
            ? "It holds a position now: this decides what happens to it."
            : "It holds no position now. The choice is kept for its next switch."}
        </p>
        {SWITCH_POLICIES.map((p) => (
          <label key={p.value} className="flex items-start gap-2 text-sm">
            <input
              type="radio"
              name="policy"
              value={p.value}
              checked={policy === p.value}
              onChange={() => setPolicy(p.value)}
              className="accent-primary mt-0.5 size-4 shrink-0"
            />
            <span>
              <span className="font-medium">{p.label}</span>
              <span className="text-muted-foreground">: {p.hint}</span>
            </span>
          </label>
        ))}
      </fieldset>

      {error !== null && (
        <p role="alert" className="text-loss text-sm">
          {error}
        </p>
      )}
      {sending && (
        <p role="status" className="text-sm">
          Switching…
        </p>
      )}
      {unchanged && (
        <p className="text-muted-foreground text-sm">
          That&apos;s what it runs now: change the strategy, a param or the policy.
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" disabled={sending} onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" disabled={!valid || unchanged || sending}>
          {same ? "Save policy" : "Switch"}
        </Button>
      </div>
    </form>
  );
}
