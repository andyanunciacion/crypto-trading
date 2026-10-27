"use client";

import { Plus } from "lucide-react";
import { useState, type FormEvent } from "react";

import { StrategyParamsForm } from "@/components/strategy-params-form";
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
import { ApiError, catalog as fetchCatalog, createBot } from "@/lib/api";
import {
  STOP_SLOTS,
  fieldProblem,
  initialValues,
  toSettings,
  type BotReply,
  type Catalog,
  type Choice,
  type FieldSpec,
  type NewBot,
  type Settings,
} from "@/lib/bots";

type Values = Record<string, string>;
type SlotKey = (typeof STOP_SLOTS)[number]["key"];

const CASH: FieldSpec = {
  name: "cash",
  type: "decimal",
  default: "1000",
  minimum: "0",
  exclusive_minimum: true,
  maximum: null,
  exclusive_maximum: false,
  description: null,
};
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const NO_STOPS: Record<SlotKey, string> = { stop_loss: "", take_profit: "", trailing: "" };

function choice(choices: Choice[], name: string): Choice | undefined {
  return choices.find((c) => c.name === name);
}

/** Every value shown is valid (the hints under each input say what's wrong). */
function allValid(fields: FieldSpec[], values: Values): boolean {
  return fields.every((f) => fieldProblem(f, values[f.name] ?? "") === null);
}

/**
 * "New bot" (D47): a side sheet with the engine's own choices, each strategy's params
 * drawn from its pydantic model. The bot is created stopped, with its paper cash;
 * starting it is a separate click in the list. The engine checks everything (the
 * params, the symbol on the exchange, the name) and its refusal is shown here.
 */
export function NewBotSheet({ onCreated }: { onCreated: (reply: BotReply, name: string) => void }) {
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
        <Button>
          <Plus aria-hidden />
          New bot
        </Button>
      </SheetTrigger>
      <SheetContent className="sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>New bot</SheetTitle>
          <SheetDescription>
            A paper bot, created stopped: start it from the list when you&apos;re ready. The engine
            checks everything before it&apos;s saved.
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
          <NewBotForm
            key={opened}
            catalog={catalog}
            onSending={setSending}
            onCancel={() => setOpen(false)}
            onCreated={(reply, name) => {
              setOpen(false);
              onCreated(reply, name);
            }}
          />
        )}
      </SheetContent>
    </Sheet>
  );
}

function NewBotForm({
  catalog,
  onSending,
  onCancel,
  onCreated,
}: {
  catalog: Catalog;
  onSending: (sending: boolean) => void;
  onCancel: () => void;
  onCreated: (reply: BotReply, name: string) => void;
}) {
  const first = catalog.strategies[0];
  const [name, setName] = useState("");
  const [strategy, setStrategy] = useState(first?.name ?? "");
  const [params, setParams] = useState<Values>(first ? initialValues(first.fields) : {});
  const [symbol, setSymbol] = useState("BTC/USDT");
  const [timeframe, setTimeframe] = useState(
    catalog.timeframes.includes("1h") ? "1h" : (catalog.timeframes[0] ?? ""),
  );
  const [cash, setCash] = useState(CASH.default ?? "");
  const [sizingMode, setSizingMode] = useState("all_in");
  const [sizing, setSizing] = useState<Values>(
    initialValues(choice(catalog.sizing, "all_in")?.fields ?? []),
  );
  const [stopKinds, setStopKinds] = useState<Record<SlotKey, string>>(NO_STOPS);
  const [stopValues, setStopValues] = useState<Record<SlotKey, Values>>({
    stop_loss: {},
    take_profit: {},
    trailing: {},
  });
  const [stopsShared, setStopsShared] = useState<Values>(initialValues(catalog.stops_fields));
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const strategyChoice = choice(catalog.strategies, strategy);
  const sizingChoice = choice(catalog.sizing, sizingMode);
  const usesAtr = Object.values(stopKinds).includes("atr");
  const stopChoices = STOP_SLOTS.map(({ key }) => ({
    key,
    chosen: stopKinds[key] === "" ? undefined : choice(catalog.stops[key] ?? [], stopKinds[key]),
  }));
  const cashProblem = fieldProblem(CASH, cash);
  const valid =
    NAME.test(name) &&
    symbol.includes("/") &&
    cashProblem === null &&
    strategyChoice !== undefined &&
    allValid(strategyChoice.fields, params) &&
    sizingChoice !== undefined &&
    allValid(sizingChoice.fields, sizing) &&
    stopChoices.every(({ key, chosen }) => !chosen || allValid(chosen.fields, stopValues[key])) &&
    (!usesAtr || allValid(catalog.stops_fields, stopsShared));

  function sendingNow(value: boolean) {
    setSending(value);
    onSending(value);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid || !strategyChoice || !sizingChoice) return;
    const stops: Record<string, Settings | number> = {};
    for (const { key, chosen } of stopChoices) {
      if (chosen) stops[key] = { kind: chosen.name, ...toSettings(chosen.fields, stopValues[key]) };
    }
    if (usesAtr) Object.assign(stops, toSettings(catalog.stops_fields, stopsShared));
    const bot: NewBot = {
      name: name.trim(),
      strategy,
      params: toSettings(strategyChoice.fields, params),
      symbol: symbol.trim().toUpperCase(),
      timeframe,
      cash: cash.trim(),
      sizing: { mode: sizingMode, ...toSettings(sizingChoice.fields, sizing) },
      stops,
    };
    sendingNow(true);
    setError(null);
    try {
      const reply = await createBot(bot);
      sendingNow(false);
      onCreated(reply, bot.name);
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
    <form onSubmit={submit} className="flex flex-col gap-6" aria-label="New bot">
      <fieldset className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <legend className="mb-2 text-sm font-semibold">Bot</legend>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="bot-name">Name</Label>
          <Input
            id="bot-name"
            value={name}
            autoComplete="off"
            aria-invalid={name !== "" && !NAME.test(name)}
            aria-describedby="bot-name-hint"
            onChange={(event) => setName(event.target.value)}
          />
          <p id="bot-name-hint" className="text-muted-foreground text-xs">
            Unique; letters, digits, - _ and .
          </p>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="bot-cash">Paper cash</Label>
          <Input
            id="bot-cash"
            value={cash}
            inputMode="decimal"
            className="font-mono"
            aria-invalid={cashProblem !== null}
            aria-describedby="bot-cash-hint"
            onChange={(event) => setCash(event.target.value)}
          />
          <p id="bot-cash-hint" className="text-muted-foreground text-xs">
            {cashProblem === null ? (
              `in ${symbol.split("/")[1] || "the quote currency"}`
            ) : (
              <span className="text-loss">{cashProblem}</span>
            )}
          </p>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="bot-symbol">Symbol</Label>
          <Input
            id="bot-symbol"
            value={symbol}
            autoComplete="off"
            aria-describedby="bot-symbol-hint"
            onChange={(event) => setSymbol(event.target.value)}
          />
          <p id="bot-symbol-hint" className="text-muted-foreground text-xs">
            e.g. BTC/USDT; the exchange must list it
          </p>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="bot-timeframe">Timeframe</Label>
          <NativeSelect
            id="bot-timeframe"
            value={timeframe}
            onChange={(event) => setTimeframe(event.target.value)}
          >
            {catalog.timeframes.map((tf) => (
              <option key={tf} value={tf}>
                {tf}
              </option>
            ))}
          </NativeSelect>
        </div>
      </fieldset>

      <fieldset className="flex flex-col gap-3">
        <legend className="mb-2 text-sm font-semibold">Strategy</legend>
        <NativeSelect
          aria-label="Strategy"
          value={strategy}
          onChange={(event) => {
            setStrategy(event.target.value);
            setParams(initialValues(choice(catalog.strategies, event.target.value)?.fields ?? []));
          }}
        >
          {catalog.strategies.map((s) => (
            <option key={s.name} value={s.name}>
              {s.title}
            </option>
          ))}
        </NativeSelect>
        {strategyChoice && (
          <StrategyParamsForm
            idPrefix="param"
            fields={strategyChoice.fields}
            values={params}
            onChange={setParams}
          />
        )}
      </fieldset>

      <fieldset className="flex flex-col gap-3">
        <legend className="mb-2 text-sm font-semibold">Sizing</legend>
        <p className="text-muted-foreground -mt-2 text-xs">
          How much each buy spends. The risk limits cap it (by default, no coin above 25% of the
          account).
        </p>
        <NativeSelect
          aria-label="Sizing"
          value={sizingMode}
          onChange={(event) => {
            setSizingMode(event.target.value);
            setSizing(initialValues(choice(catalog.sizing, event.target.value)?.fields ?? []));
          }}
        >
          {catalog.sizing.map((m) => (
            <option key={m.name} value={m.name}>
              {m.title}
            </option>
          ))}
        </NativeSelect>
        {sizingChoice && sizingChoice.fields.length > 0 && (
          <StrategyParamsForm
            idPrefix="sizing"
            fields={sizingChoice.fields}
            values={sizing}
            onChange={setSizing}
          />
        )}
      </fieldset>

      <fieldset className="flex flex-col gap-4">
        <legend className="mb-2 text-sm font-semibold">Stops</legend>
        {STOP_SLOTS.map(({ key, label, hint }) => {
          const chosen = stopChoices.find((s) => s.key === key)?.chosen;
          return (
            <div key={key} className="flex flex-col gap-2">
              <Label htmlFor={`stop-${key}`}>{label}</Label>
              <NativeSelect
                id={`stop-${key}`}
                value={stopKinds[key]}
                aria-describedby={`stop-${key}-hint`}
                onChange={(event) => {
                  const kind = event.target.value;
                  setStopKinds({ ...stopKinds, [key]: kind });
                  const fields = choice(catalog.stops[key] ?? [], kind)?.fields ?? [];
                  setStopValues({ ...stopValues, [key]: initialValues(fields) });
                }}
              >
                <option value="">None</option>
                {(catalog.stops[key] ?? []).map((k) => (
                  <option key={k.name} value={k.name}>
                    {k.title}
                  </option>
                ))}
              </NativeSelect>
              <p id={`stop-${key}-hint`} className="text-muted-foreground text-xs">
                {hint}
              </p>
              {chosen && (
                <StrategyParamsForm
                  idPrefix={`stop-${key}`}
                  fields={chosen.fields}
                  values={stopValues[key]}
                  onChange={(values) => setStopValues({ ...stopValues, [key]: values })}
                />
              )}
            </div>
          );
        })}
        {usesAtr && (
          <StrategyParamsForm
            idPrefix="stops"
            fields={catalog.stops_fields}
            values={stopsShared}
            onChange={setStopsShared}
          />
        )}
      </fieldset>

      {error !== null && (
        <p role="alert" className="text-loss text-sm">
          {error}
        </p>
      )}
      {sending && (
        <p role="status" className="text-sm">
          Creating…
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" disabled={sending} onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" disabled={!valid || sending}>
          Create bot
        </Button>
      </div>
    </form>
  );
}
