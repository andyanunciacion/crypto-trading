"use client";

import { KeyRound, Trash2 } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { CodeDialog } from "@/components/code-dialog";
import { Filter } from "@/components/page-tabs";
import { Timestamp } from "@/components/timestamp";
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
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  ApiError,
  addExchangeAccount,
  deleteExchangeAccount,
  exchangeAccounts,
  replaceExchangeKeys,
} from "@/lib/api";
import { EXCHANGE_NAMES, codeNeeded, type Exchange, type ExchangeAccount } from "@/lib/settings";

type Message = { kind: "status" | "alert"; text: string };
/** What the code dialog is confirming: a new account, or new keys for one. */
type Pending = { kind: "add" } | { kind: "replace"; account: ExchangeAccount };

const EMPTY_FORM = { exchange: "mexc" as Exchange, label: "", apiKey: "", apiSecret: "" };

function problemText(error: unknown): string {
  if (!(error instanceof ApiError)) return "Couldn't reach the API.";
  return error.status === 422 ? `Refused: ${error.message}` : error.message;
}

/**
 * The exchange keys (D50): stored encrypted by the API, and never shown again, only the
 * API key's last 4 characters. Adding or replacing keys asks for a TOTP code; deleting
 * doesn't. The typed keys are cleared once saved. Nothing trades with them until P9.
 */
export function ExchangeKeys() {
  const [accounts, setAccounts] = useState<ExchangeAccount[] | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [sandbox, setSandbox] = useState(false);
  const [replacing, setReplacing] = useState<ExchangeAccount | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [codeError, setCodeError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message | null>(null);
  const [reloads, setReloads] = useState(0);
  const messageRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    let current = true;
    exchangeAccounts()
      .then((loaded) => {
        if (current) setAccounts(loaded);
      })
      .catch((error: unknown) => {
        if (current) setMessage({ kind: "alert", text: problemText(error) });
      });
    return () => {
      current = false;
    };
  }, [reloads]);

  useEffect(() => {
    if (message !== null) messageRef.current?.focus();
  }, [message]);

  const keysValid = form.apiKey.length >= 16 && form.apiSecret.length >= 16;
  const addValid = keysValid && form.label.trim() !== "";

  function edit(change: Partial<typeof EMPTY_FORM>) {
    setMessage(null);
    setForm((old) => ({ ...old, ...change }));
  }

  function clear() {
    setForm(EMPTY_FORM);
    setSandbox(false);
    setReplacing(null);
  }

  async function confirm(code: string) {
    if (pending === null) return;
    setBusy(true);
    const keys = { api_key: form.apiKey, api_secret: form.apiSecret, code };
    try {
      const saved =
        pending.kind === "add"
          ? await addExchangeAccount({
              ...keys,
              exchange: form.exchange,
              label: form.label.trim(),
              sandbox: form.exchange === "binance" && sandbox,
            })
          : await replaceExchangeKeys(pending.account.id, keys);
      clear();
      setPending(null);
      setCodeError(null);
      setMessage({
        kind: "status",
        text: `${pending.kind === "add" ? "Saved" : "Replaced"} ${saved.label}'s keys (key …${saved.key_hint}), encrypted.`,
      });
    } catch (error) {
      const needed = codeNeeded(error);
      if (needed !== null) {
        setCodeError(needed.detail);
      } else {
        setPending(null);
        setMessage({ kind: "alert", text: problemText(error) });
      }
    } finally {
      setBusy(false);
      setReloads((n) => n + 1);
    }
  }

  async function remove(account: ExchangeAccount) {
    try {
      await deleteExchangeAccount(account.id);
      setMessage({ kind: "status", text: `Deleted ${account.label}'s keys.` });
    } catch (error) {
      setMessage({ kind: "alert", text: problemText(error) });
    } finally {
      setReloads((n) => n + 1);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (replacing !== null ? keysValid : addValid) {
      setCodeError(null);
      setPending(replacing !== null ? { kind: "replace", account: replacing } : { kind: "add" });
    }
  }

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      {accounts === null ? (
        !message && <p className="text-muted-foreground text-sm">Loading…</p>
      ) : accounts.length === 0 ? (
        <p className="text-muted-foreground text-sm">No exchange keys stored.</p>
      ) : (
        <Card className="py-2">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Label</TableHead>
                <TableHead>Exchange</TableHead>
                <TableHead>API key</TableHead>
                <TableHead>Saved</TableHead>
                <TableHead>
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {accounts.map((account) => (
                <TableRow key={account.id} data-account={account.id}>
                  <TableCell className="font-medium">{account.label}</TableCell>
                  <TableCell>
                    {EXCHANGE_NAMES[account.exchange]}
                    {account.sandbox && " (testnet)"}
                  </TableCell>
                  <TableCell className="font-mono">…{account.key_hint}</TableCell>
                  <TableCell>
                    <Timestamp iso={account.updated_at} />
                  </TableCell>
                  <TableCell className="flex justify-end gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      aria-label={`Replace ${account.label}'s keys`}
                      onClick={() => {
                        setMessage(null);
                        setForm({ ...EMPTY_FORM, exchange: account.exchange });
                        setReplacing(account);
                      }}
                    >
                      <KeyRound aria-hidden />
                      Replace
                    </Button>
                    <DeleteDialog account={account} onDelete={() => void remove(account)} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}

      <Card className="gap-3 py-4">
        <CardHeader className="px-4">
          <CardTitle className="text-base">
            {replacing === null ? "Add exchange keys" : `Replace ${replacing.label}'s keys`}
          </CardTitle>
        </CardHeader>
        <CardContent className="px-4">
          <form
            onSubmit={submit}
            autoComplete="off"
            className="flex flex-col gap-3"
            aria-label={replacing === null ? "Add exchange keys" : "Replace exchange keys"}
          >
            <p className="text-muted-foreground text-xs">
              Use a key with spot read and trade only, <strong>never withdrawal</strong>, locked to
              the server&apos;s IP. The keys are encrypted before they&apos;re stored and are never
              shown again. Nothing trades with them until live trading (P9).
            </p>
            {replacing === null && (
              <div className="flex flex-wrap items-end gap-3">
                <Filter label="Exchange">
                  <NativeSelect
                    value={form.exchange}
                    onChange={(e) => edit({ exchange: e.target.value as Exchange })}
                  >
                    <option value="mexc">MEXC</option>
                    <option value="binance">Binance</option>
                  </NativeSelect>
                </Filter>
                <Filter label="Label">
                  <Input
                    value={form.label}
                    maxLength={64}
                    placeholder="main"
                    onChange={(e) => edit({ label: e.target.value })}
                  />
                </Filter>
                {form.exchange === "binance" && (
                  <label className="flex items-center gap-1.5 pb-2 text-sm">
                    <input
                      type="checkbox"
                      checked={sandbox}
                      onChange={(e) => setSandbox(e.target.checked)}
                      className="accent-primary size-4"
                    />
                    Spot testnet
                  </label>
                )}
              </div>
            )}
            <div className="grid gap-3 sm:grid-cols-2">
              <Filter label="API key">
                <Input
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={form.apiKey}
                  onChange={(e) => edit({ apiKey: e.target.value.trim() })}
                />
              </Filter>
              <Filter label="Secret key">
                <Input
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={form.apiSecret}
                  onChange={(e) => edit({ apiSecret: e.target.value.trim() })}
                />
              </Filter>
            </div>
            <div className="flex items-center gap-3">
              <Button type="submit" disabled={replacing !== null ? !keysValid : !addValid}>
                {replacing === null ? "Save keys" : "Replace keys"}
              </Button>
              {replacing !== null && (
                <Button type="button" variant="outline" onClick={clear}>
                  Cancel
                </Button>
              )}
            </div>
          </form>
        </CardContent>
      </Card>

      {message && (
        <p
          ref={messageRef}
          tabIndex={-1}
          role={message.kind}
          className={message.kind === "alert" ? "text-loss text-sm" : "text-sm"}
        >
          {message.text}
        </p>
      )}

      <CodeDialog
        open={pending !== null}
        title={
          pending?.kind === "replace" ? "Replace the exchange keys?" : "Save the exchange keys?"
        }
        confirm="Save keys"
        busy={busy}
        error={codeError}
        onSubmit={(code) => void confirm(code)}
        onCancel={() => {
          setPending(null);
          setCodeError(null);
        }}
      >
        <p>
          {pending?.kind === "replace"
            ? `${pending.account.label}'s keys (${EXCHANGE_NAMES[pending.account.exchange]}) will be replaced.`
            : `Keys for ${EXCHANGE_NAMES[form.exchange]}, as "${form.label.trim()}".`}{" "}
          From P9, keys like these trade real money.
        </p>
      </CodeDialog>
    </div>
  );
}

/** Deleting needs no code (it only lowers risk), just this confirmation; focus on Cancel. */
function DeleteDialog({ account, onDelete }: { account: ExchangeAccount; onDelete: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>
        <Button size="sm" variant="outline" aria-label={`Delete ${account.label}'s keys`}>
          <Trash2 aria-hidden />
          Delete
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete {account.label}&apos;s keys?</AlertDialogTitle>
          <AlertDialogDescription>
            The app forgets them. They stay valid on {EXCHANGE_NAMES[account.exchange]} until you
            delete the key there too.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button
            variant="destructive"
            onClick={() => {
              setOpen(false);
              onDelete();
            }}
          >
            Delete
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
