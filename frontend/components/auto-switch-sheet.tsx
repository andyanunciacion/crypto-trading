"use client";

import { Shuffle } from "lucide-react";
import { useRef, useState, type FormEvent } from "react";

import { allValid } from "@/components/strategy-setup";
import { StrategyParamsForm } from "@/components/strategy-params-form";
import { SwitchPolicyFields } from "@/components/switch-policy-fields";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
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
  REGIMES,
  fieldProblem,
  initialValues,
  toSettings,
  type AutoSwitchSettings,
  type BotReply,
  type BotSetup,
  type Catalog,
  type Choice,
  type FieldSpec,
  type MappedRegime,
  type SwitchPolicy,
} from "@/lib/bots";

/** What each regime starts as when auto-switching is first set up. */
const STARTING: Record<MappedRegime, string> = {
  trending_up: "ema_crossover",
  trending_down: "stay_flat",
  ranging: "rsi_mean_reversion",
  high_volatility: "stay_flat",
};

const MIN_HOLD: FieldSpec = {
  name: "min_hold",
  type: "integer",
  default: "3",
  minimum: "1",
  exclusive_minimum: false,
  maximum: "100",
  exclusive_maximum: false,
  description: null,
};

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

type Row = { strategy: string; values: Record<string, string> };

/** The inputs for `choice`'s params: `params`' values where it has them. */
function valuesFor(choice: Choice | undefined, params: Record<string, unknown> | undefined) {
  const fields = choice?.fields ?? [];
  const values = initialValues(fields);
  for (const field of fields) {
    const value = params?.[field.name];
    if (typeof value === "number" || typeof value === "string") values[field.name] = String(value);
  }
  return values;
}

function startingRows(bot: BotSetup, catalog: Catalog): Record<MappedRegime, Row> {
  const choose = (name: string) =>
    catalog.strategies.find((s) => s.name === name) ?? catalog.strategies[0];
  return Object.fromEntries(
    REGIMES.map(({ value: regime }) => {
      const stored = bot.auto_switch?.mapping[regime];
      const choice = choose(stored?.strategy ?? STARTING[regime]);
      return [regime, { strategy: choice?.name ?? "", values: valuesFor(choice, stored?.params) }];
    }),
  ) as Record<MappedRegime, Row>;
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
  const [rows, setRows] = useState(() => startingRows(bot, catalog));
  const [minHold, setMinHold] = useState(String(bot.auto_switch?.min_hold ?? MIN_HOLD.default));
  const [policy, setPolicy] = useState<SwitchPolicy>(bot.auto_switch?.policy ?? bot.switch_policy);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fieldsOf = (row: Row) =>
    catalog.strategies.find((s) => s.name === row.strategy)?.fields ?? [];
  const holdProblem = fieldProblem(MIN_HOLD, minHold);
  const valid =
    holdProblem === null &&
    REGIMES.every(({ value }) => {
      const row = rows[value];
      return row.strategy !== "" && allValid(fieldsOf(row), row.values);
    });

  function setRow(regime: MappedRegime, row: Row) {
    setRows((current) => ({ ...current, [regime]: row }));
  }

  function sendingNow(value: boolean) {
    setSending(value);
    onSending(value);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid) return;
    const settings: AutoSwitchSettings = {
      mapping: Object.fromEntries(
        REGIMES.map(({ value }) => {
          const row = rows[value];
          return [value, { strategy: row.strategy, params: toSettings(fieldsOf(row), row.values) }];
        }),
      ) as AutoSwitchSettings["mapping"],
      min_hold: Number(minHold.trim()),
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
      {REGIMES.map(({ value: regime, label, hint }) => {
        const row = rows[regime];
        const hintId = `auto-${regime}-hint`;
        return (
          <fieldset key={regime} className="flex flex-col gap-3" aria-describedby={hintId}>
            <legend className="mb-2 text-sm font-semibold">{label}</legend>
            <p id={hintId} className="text-muted-foreground -mt-2 text-xs">
              {hint}
            </p>
            <NativeSelect
              aria-label={`Strategy when ${label.toLowerCase()}`}
              value={row.strategy}
              onChange={(event) => {
                const next = event.target.value;
                const choice = catalog.strategies.find((s) => s.name === next);
                setRow(regime, { strategy: next, values: valuesFor(choice, undefined) });
              }}
            >
              {catalog.strategies.map((s) => (
                <option key={s.name} value={s.name}>
                  {s.title}
                </option>
              ))}
            </NativeSelect>
            <StrategyParamsForm
              idPrefix={`auto-${regime}`}
              fields={fieldsOf(row)}
              values={row.values}
              onChange={(values) => setRow(regime, { ...row, values })}
            />
          </fieldset>
        );
      })}

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="auto-min-hold">Minimum hold, in candles</Label>
        <Input
          id="auto-min-hold"
          inputMode="numeric"
          value={minHold}
          aria-invalid={holdProblem !== null}
          aria-describedby="auto-min-hold-hint"
          onChange={(event) => setMinHold(event.target.value)}
          className="w-32 font-mono"
        />
        <p id="auto-min-hold-hint" className="text-muted-foreground text-xs">
          {holdProblem !== null && <span className="text-loss">{holdProblem} · </span>}
          How many closed {bot.timeframe} candles in a row a new regime must read before the bot
          switches (1–100). Longer is steadier, but later: the detector already sees a turn a few
          candles late.
        </p>
      </div>

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
        <Button type="submit" disabled={!valid || sending}>
          {bot.auto_switch === null ? "Turn on" : "Save"}
        </Button>
      </div>
    </form>
  );
}
