"use client";

import { Plus } from "lucide-react";
import { useState, type FormEvent } from "react";

import { StrategySetup, initialSetup, setupPayload, type Setup } from "@/components/strategy-setup";
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
import { fieldProblem, type BotReply, type Catalog, type FieldSpec, type NewBot } from "@/lib/bots";

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
  const [name, setName] = useState("");
  const [symbol, setSymbol] = useState("BTC/USDT");
  const [timeframe, setTimeframe] = useState(
    catalog.timeframes.includes("1h") ? "1h" : (catalog.timeframes[0] ?? ""),
  );
  const [cash, setCash] = useState(CASH.default ?? "");
  const [setup, setSetup] = useState<Setup>(() => initialSetup(catalog));
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const cashProblem = fieldProblem(CASH, cash);
  const payload = setupPayload(catalog, setup);
  const valid = NAME.test(name) && symbol.includes("/") && cashProblem === null && payload !== null;

  function sendingNow(value: boolean) {
    setSending(value);
    onSending(value);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid || payload === null) return;
    const bot: NewBot = {
      ...payload,
      name: name.trim(),
      symbol: symbol.trim().toUpperCase(),
      timeframe,
      cash: cash.trim(),
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

      <StrategySetup
        catalog={catalog}
        setup={setup}
        onChange={setSetup}
        sizingNote="How much each buy spends. The risk limits cap it (by default, no coin above 25% of the account)."
      />

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
