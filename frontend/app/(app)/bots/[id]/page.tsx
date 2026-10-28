"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { BotWhy, StatusBadge } from "@/components/bot-status";
import { EditStrategySheet } from "@/components/edit-strategy-sheet";
import { Money } from "@/components/money";
import { ModeBadge } from "@/components/mode-badge";
import { useFeed } from "@/components/overview-provider";
import { RollbackDialog } from "@/components/rollback-dialog";
import { Timestamp } from "@/components/timestamp";
import { Card } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError, bot as fetchBot, catalog as fetchCatalog } from "@/lib/api";
import {
  describeParams,
  describeSizing,
  describeStops,
  policyLabel,
  switchedText,
  type BotDetail,
  type Catalog,
  type ConfigEntry,
} from "@/lib/bots";
import { cn } from "@/lib/utils";

type Message = { kind: "status" | "alert"; text: string };

/**
 * One bot (P6-2, D52): its setup, and every strategy config it has had, newest first.
 * Configs are append-only, so the history is complete: each row is when that config
 * became the bot's, and Roll back on an earlier one makes a copy of it the bot's config
 * (a switch, as Edit makes, with a policy for the open position).
 */
export default function BotPage() {
  const { id } = useParams<{ id: string }>();
  const feed = useFeed();
  const [detail, setDetail] = useState<BotDetail | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [message, setMessage] = useState<Message | null>(null);
  const [reloads, setReloads] = useState(0); // bumped after each command
  const botId = Number(id);
  const validId = Number.isInteger(botId) && botId >= 1;
  const error = validId ? loadError : `No bot ${id}.`;
  // A command's result takes the focus, as on the Bots page: the button that sent it
  // is gone once the history reloads.
  const messageRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (message !== null) messageRef.current?.focus();
  }, [message]);

  useEffect(() => {
    if (!validId) return;
    let current = true;
    fetchBot(botId)
      .then((loaded) => {
        if (!current) return;
        setDetail(loaded);
        setLoadError(null);
      })
      .catch((problem: unknown) => {
        if (current) {
          setLoadError(problem instanceof ApiError ? problem.message : "Couldn't reach the API.");
        }
      });
    return () => {
      current = false;
    };
  }, [botId, validId, reloads]);

  // Only to say which configs the engine can no longer build; it checks again anyway.
  useEffect(() => {
    let current = true;
    fetchCatalog()
      .then((loaded) => current && setCatalog(loaded))
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, []);

  const live = feed.overview?.bots.find((b) => b.id === botId);
  const holding = (feed.overview?.positions ?? []).some((p) => p.bot_id === botId);

  return (
    <section aria-labelledby="bot" className="flex flex-col gap-4">
      <Link
        href="/bots"
        className="text-muted-foreground flex items-center gap-1 text-sm hover:underline"
      >
        <ArrowLeft aria-hidden className="size-4" />
        Bots
      </Link>
      {message && (
        <p
          ref={messageRef}
          tabIndex={-1}
          role={message.kind}
          className={cn("text-sm outline-none", message.kind === "alert" && "text-loss")}
        >
          {message.text}
        </p>
      )}
      {error !== null ? (
        <p role="alert" className="text-loss text-sm">
          {error}
        </p>
      ) : detail === null ? (
        <p role="status" className="text-muted-foreground text-sm">
          Loading…
        </p>
      ) : (
        <Detail
          // The feed's status is a second old at most; the page's, as of its last load.
          detail={
            live === undefined
              ? detail
              : {
                  ...detail,
                  bot: {
                    ...detail.bot,
                    status: live.status,
                    paused_reason: live.paused_reason,
                    paused_at: live.paused_at,
                    stopped_reason: live.stopped_reason,
                    stopped_at: live.stopped_at,
                  },
                }
          }
          catalog={catalog}
          holding={holding}
          onDone={(text) => {
            setMessage({ kind: "status", text });
            setReloads((n) => n + 1);
          }}
        />
      )}
    </section>
  );
}

function Detail({
  detail,
  catalog,
  holding,
  onDone,
}: {
  detail: BotDetail;
  catalog: Catalog | null;
  holding: boolean;
  onDone: (text: string) => void;
}) {
  const { bot, configs } = detail;
  const numbers = new Map(configs.map((c) => [c.id, c.number]));
  const running = configs.find((c) => c.active);
  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <h1 id="bot" className="text-xl font-semibold">
          {bot.name}
        </h1>
        <StatusBadge status={bot.status} />
        <ModeBadge mode={bot.mode} />
        <span className="text-muted-foreground text-sm">
          {bot.symbol} · {bot.timeframe}
        </span>
        <div className="ml-auto">
          <EditStrategySheet
            bot={bot}
            holding={holding}
            disabled={false}
            onSwitched={(reply, change) => onDone(switchedText(bot, change, reply, holding))}
          />
        </div>
      </div>

      <Card className="px-6 py-4">
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
          <Setting term="Strategy">
            {bot.strategy ?? "—"}
            {bot.strategy_version !== null && ` v${bot.strategy_version}`}:{" "}
            <span className="font-mono">{describeParams(bot.params)}</span>
          </Setting>
          <Setting term="On a switch">{policyLabel(bot.switch_policy)}</Setting>
          <Setting term="Sizing">{describeSizing(bot.sizing)}</Setting>
          <Setting term="Stops">{describeStops(bot.stops)}</Setting>
          <Setting term="Cash">
            <Money value={bot.cash} currency={bot.currency} />
          </Setting>
          {bot.status !== "running" && (
            <Setting term="Why">
              <BotWhy bot={bot} />
            </Setting>
          )}
        </dl>
      </Card>

      <h2 id="history" className="text-lg font-semibold">
        Strategy history
      </h2>
      <p className="text-muted-foreground -mt-2 text-sm">
        Every strategy and params it has run, newest first. Nothing here is ever changed or deleted:
        a switch or a rollback adds a row.
      </p>
      <Card className="py-2">
        <Table aria-labelledby="history">
          <TableHeader>
            <TableRow>
              <TableHead className="text-right">#</TableHead>
              <TableHead>Strategy</TableHead>
              <TableHead>Params</TableHead>
              <TableHead>Made</TableHead>
              <TableHead>By</TableHead>
              <TableHead>Note</TableHead>
              <TableHead>
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {configs.map((config) => {
              const blocked = cantBuild(config, catalog);
              // Rolling back to what it runs now would change nothing (the engine refuses it).
              const same = !config.active && running !== undefined && sameSetup(config, running);
              const restored =
                config.restored_from === null ? undefined : numbers.get(config.restored_from);
              return (
                <TableRow key={config.id} data-config={config.number}>
                  <TableCell className="text-right font-mono tabular-nums">
                    {config.number}
                  </TableCell>
                  <TableCell>
                    {config.strategy}{" "}
                    <span className="text-muted-foreground text-xs">v{config.version}</span>
                  </TableCell>
                  <TableCell className="font-mono text-xs whitespace-normal">
                    {describeParams(config.params)}
                  </TableCell>
                  <TableCell>
                    <Timestamp iso={config.created_at} />
                  </TableCell>
                  <TableCell className="text-muted-foreground text-xs">
                    {config.created_by}
                  </TableCell>
                  <TableCell className="text-xs whitespace-normal">
                    <div className="flex flex-col">
                      {config.active && <span className="font-medium">running now</span>}
                      {restored !== undefined && (
                        <span className="text-muted-foreground">rollback to #{restored}</span>
                      )}
                      {same && <span className="text-muted-foreground">same as running now</span>}
                      {!config.active && !same && blocked !== null && (
                        <span className="text-muted-foreground">{blocked}</span>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    {!config.active && !same && (
                      <div className="flex justify-end">
                        <RollbackDialog
                          bot={bot}
                          config={config}
                          holding={holding}
                          disabled={blocked}
                          onRolledBack={(reply, policy) =>
                            onDone(
                              switchedText(
                                bot,
                                { strategy: config.strategy, policy },
                                reply,
                                holding,
                                config.number,
                              ),
                            )
                          }
                        />
                      </div>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </Card>
    </>
  );
}

/**
 * Whether two configs are the same strategy, version and params. Both come from the
 * same JSONB column, which stores an object's keys in one order, so their JSON compares.
 */
function sameSetup(a: ConfigEntry, b: ConfigEntry): boolean {
  return (
    a.strategy === b.strategy &&
    a.version === b.version &&
    JSON.stringify(a.params) === JSON.stringify(b.params)
  );
}

/** Why the engine can't run `config` now (its strategy is gone or has a new version), or null. */
function cantBuild(config: ConfigEntry, catalog: Catalog | null): string | null {
  if (catalog === null) return null;
  const strategy = catalog.strategies.find((s) => s.name === config.strategy);
  if (strategy === undefined) return `the engine has no ${config.strategy} now`;
  if (strategy.version !== null && strategy.version !== config.version) {
    return `${config.strategy} is version ${strategy.version} now: can't roll back`;
  }
  return null;
}

function Setting({ term, children }: { term: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{term}</dt>
      <dd>{children}</dd>
    </>
  );
}
