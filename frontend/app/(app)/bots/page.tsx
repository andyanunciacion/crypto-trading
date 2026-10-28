"use client";

import { Pause, Play, Square } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { BotWhy, StatusBadge } from "@/components/bot-status";
import { EditStrategySheet } from "@/components/edit-strategy-sheet";
import { Money } from "@/components/money";
import { ModeBadge } from "@/components/mode-badge";
import { NewBotSheet } from "@/components/new-bot-sheet";
import { ageSeconds, useFeed, useNow } from "@/components/overview-provider";
import { StopBotDialog } from "@/components/stop-bot-dialog";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError, botCommand, bots as fetchBots } from "@/lib/api";
import {
  describeParams,
  describeSizing,
  describeStops,
  policyLabel,
  switchedText,
  type BotReply,
  type BotSetup,
} from "@/lib/bots";
import { ENGINE_STALE_SECONDS, type Bot, type BotStatus, type Position } from "@/lib/overview";
import { cn } from "@/lib/utils";

type Message = { kind: "status" | "alert"; text: string };

/** How long a command's answer outranks the feed: a few of its one-second snapshots. */
const ANSWER_WINS_MS = 3000;

function problemText(error: unknown): string {
  if (!(error instanceof ApiError)) return "Couldn't reach the API.";
  return error.status === 422 ? `Refused: ${error.message}` : error.message;
}

/**
 * Bots (DESIGN §4, D47): every bot with its setup, live status from the Overview feed,
 * and its controls. Each control is a command the engine carries out (the API never
 * writes a bot), so the reply says what really happened. Pausing is one click (UX
 * principle 3); stopping asks, because a stopped bot's position is left unwatched.
 * Edit switches its strategy or params while it runs (D51); a bot's name opens its
 * detail page, with its strategy history and rollback (D52).
 */
export default function BotsPage() {
  const feed = useFeed();
  const now = useNow();
  const [setups, setSetups] = useState<BotSetup[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [message, setMessage] = useState<Message | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [reloads, setReloads] = useState(0); // bumped after each command
  const refresh = () => setReloads((n) => n + 1);
  // The engine's answer to a command, by bot: shown over the feed's status for a few
  // seconds, as the feed's snapshot (a second apart) can be older than the answer.
  const [answered, setAnswered] = useState<Map<number, { status: BotStatus; at: number }>>(
    new Map(),
  );
  // A command's result takes the focus: the button that sent it is often gone (Pause
  // becomes Start), and the keyboard would otherwise land on the page itself.
  const messageRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (message !== null) messageRef.current?.focus();
  }, [message]);

  // Loaded again after a command, and whenever a bot appears or goes (made by the CLI, say).
  const ids = feed.overview?.bots.map((bot) => bot.id).join(",") ?? "";
  useEffect(() => {
    let current = true;
    fetchBots()
      .then((loaded) => {
        if (!current) return;
        setSetups(loaded);
        setLoadError(null);
      })
      .catch((error: unknown) => {
        if (current) setLoadError(problemText(error));
      });
    return () => {
      current = false;
    };
  }, [ids, reloads]);

  const overview = feed.overview;
  const live = new Map((overview?.bots ?? []).map((bot) => [bot.id, bot]));
  const positions = new Map<number, Position>(
    (overview?.positions ?? []).map((p) => [p.bot_id, p]),
  );
  const heartbeat = overview?.engine ?? null;
  const engineFresh =
    heartbeat !== null && ageSeconds(heartbeat.ts, feed, now) <= ENGINE_STALE_SECONDS;
  const inEngine = new Set(heartbeat?.bots ?? []);

  async function run(bot: BotSetup, action: "start" | "pause" | "stop", close = false) {
    setBusy(bot.id);
    setMessage(null);
    try {
      const reply = await botCommand(bot.id, action, close);
      const status = reply.status;
      if (status !== null) {
        setAnswered((old) => new Map(old).set(bot.id, { status, at: Date.now() }));
      }
      setMessage({ kind: "status", text: said(bot.name, action, reply) });
    } catch (error) {
      setMessage({ kind: "alert", text: `${bot.name}: ${problemText(error)}` });
    } finally {
      setBusy(null);
      refresh();
    }
  }

  return (
    <section aria-labelledby="bots" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <h1 id="bots" className="text-xl font-semibold">
          Bots
        </h1>
        <span className="text-muted-foreground text-sm">paper only until P9</span>
        <div className="ml-auto">
          <NewBotSheet
            onCreated={(reply, name) => {
              setMessage({
                kind: "status",
                text: `Created ${name}, stopped, in ${reply.took_ms} ms. Start it when you're ready.`,
              });
              refresh();
            }}
          />
        </div>
      </div>

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
      {loadError && (
        <p role="alert" className="text-loss text-sm">
          {loadError}
        </p>
      )}

      {setups === null ? (
        <p className="text-muted-foreground text-sm">Loading…</p>
      ) : setups.length === 0 ? (
        <p className="text-muted-foreground text-sm">No bots yet: New bot adds one.</p>
      ) : (
        <Card className="py-2">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead>Market</TableHead>
                <TableHead>Strategy</TableHead>
                <TableHead>Sizing · stops</TableHead>
                <TableHead className="text-right">Cash</TableHead>
                <TableHead>Why</TableHead>
                <TableHead>
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {setups.map((setup) => {
                // The feed's status is a second old at most; the list's, as of its last load.
                // Just after a command, the engine's answer beats both.
                const answer = answered.get(setup.id);
                const bot =
                  answer !== undefined && now - answer.at < ANSWER_WINS_MS
                    ? { ...setup, status: answer.status }
                    : { ...setup, ...pickLive(live.get(setup.id)) };
                const position = positions.get(bot.id);
                return (
                  <TableRow key={bot.id} data-bot={bot.name}>
                    <TableCell className="font-medium">
                      {/* Its detail page: setup and strategy history, with rollback (D52). */}
                      <Link
                        href={`/bots/${bot.id}`}
                        className="decoration-muted-foreground hover:decoration-foreground underline underline-offset-4"
                      >
                        {bot.name}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-col items-start gap-1">
                        <StatusBadge status={bot.status} />
                        <EngineNote
                          status={bot.status}
                          inEngine={inEngine.has(bot.id)}
                          engineFresh={engineFresh}
                        />
                      </div>
                    </TableCell>
                    <TableCell>
                      <ModeBadge mode={bot.mode} />
                    </TableCell>
                    <TableCell>
                      {bot.symbol} · {bot.timeframe}
                    </TableCell>
                    {/* The long text wraps, so the actions stay on screen without scrolling. */}
                    <TableCell className="min-w-40 whitespace-normal">
                      <div className="flex flex-col">
                        <span>{bot.strategy ?? "—"}</span>
                        <span className="text-muted-foreground font-mono text-xs">
                          {describeParams(bot.params)}
                        </span>
                        <span className="text-muted-foreground text-xs">
                          on a switch: {policyLabel(bot.switch_policy).toLowerCase()}
                        </span>
                        {bot.auto_switch !== null && (
                          <span className="text-xs font-medium">
                            auto-switching by regime (Edit is off)
                          </span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="text-muted-foreground min-w-40 text-xs whitespace-normal">
                      <div>{describeSizing(bot.sizing)}</div>
                      <div>stops: {describeStops(bot.stops)}</div>
                    </TableCell>
                    <TableCell className="text-right">
                      <Money value={bot.cash} currency={bot.currency} />
                    </TableCell>
                    <TableCell className="text-muted-foreground min-w-40 text-xs break-words whitespace-normal">
                      <BotWhy bot={bot} />
                    </TableCell>
                    <TableCell>
                      <div className="flex justify-end gap-1">
                        <EditStrategySheet
                          bot={bot}
                          holding={position !== undefined}
                          // While the regime switches it, a switch by hand would be undone.
                          disabled={busy !== null || bot.auto_switch !== null}
                          onSwitched={(reply, change) => {
                            setMessage({
                              kind: "status",
                              text: switchedText(bot, change, reply, position !== undefined),
                            });
                            refresh();
                          }}
                        />
                        {bot.status !== "running" && (
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busy !== null}
                            onClick={() => run(bot, "start")}
                            aria-label={`Start ${bot.name}`}
                          >
                            <Play aria-hidden />
                            Start
                          </Button>
                        )}
                        {bot.status === "running" && (
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busy !== null}
                            onClick={() => run(bot, "pause")}
                            aria-label={`Pause ${bot.name}`}
                          >
                            <Pause aria-hidden />
                            Pause
                          </Button>
                        )}
                        {(bot.status !== "stopped" || position !== undefined) && (
                          <StopBotDialog
                            name={bot.name}
                            status={bot.status}
                            position={position}
                            disabled={busy !== null}
                            onStop={(close) => run(bot, "stop", close)}
                          >
                            <Square aria-hidden />
                            {bot.status === "stopped" ? "Close" : "Stop"}
                          </StopBotDialog>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </Card>
      )}
    </section>
  );
}

/** What the feed knows newer than the list: the status, and why. */
function pickLive(bot: Bot | undefined) {
  if (bot === undefined) return {};
  const { status, paused_reason, paused_at, stopped_reason, stopped_at } = bot;
  return { status, paused_reason, paused_at, stopped_reason, stopped_at };
}

/** Whether the engine is really running it yet: a started bot warms up for a few seconds. */
function EngineNote({
  status,
  inEngine,
  engineFresh,
}: {
  status: BotStatus;
  inEngine: boolean;
  engineFresh: boolean;
}) {
  if (status === "stopped") return null;
  if (!engineFresh) return <span className="text-stale text-xs">engine down</span>;
  return (
    <span className="text-muted-foreground text-xs" data-engine={inEngine ? "running" : "starting"}>
      {inEngine ? "in the engine" : "warming up…"}
    </span>
  );
}

function said(name: string, action: "start" | "pause" | "stop", reply: BotReply): string {
  const took = `(${reply.took_ms} ms)`;
  if (action === "start") return `Started ${name} ${took}: it trades from its next candle.`;
  if (action === "pause") {
    return `Paused ${name} ${took}: no buys; its sells and stops carry on.`;
  }
  const parts = [`Stopped ${name} ${took}`];
  if (reply.canceled.length > 0) parts.push(`cancelled ${reply.canceled.length} order(s)`);
  if (reply.closing.length > 0) parts.push("selling its position until it's gone");
  for (const left of reply.not_closed) parts.push(`couldn't close ${left.symbol}: ${left.reason}`);
  return `${parts.join("; ")}.`;
}
