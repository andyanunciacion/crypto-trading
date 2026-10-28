"use client";

import { useRef, useState, type FormEvent, type ReactNode } from "react";

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * Asks for a TOTP code before a change that adds risk (UX4, D50): loosening a risk
 * limit, saving exchange keys (and from P9, going live, UX3). Focus starts in the code
 * field. A wrong code, or a lock after too many, is shown here, and the code is cleared
 * for the next one. The code just used to sign in won't do: each is good once.
 */
export function CodeDialog({
  open,
  title,
  children,
  confirm,
  busy,
  error,
  onSubmit,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode; // what the code is for
  confirm: string; // the button's label
  busy: boolean;
  error: string | null;
  onSubmit: (code: string) => void;
  onCancel: () => void;
}) {
  const [code, setCode] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const valid = /^\d{6}$/.test(code);

  function submit(event: FormEvent) {
    event.preventDefault();
    // React events bubble through the portal to whatever renders this: never past it,
    // or a form around it would be submitted too.
    event.stopPropagation();
    if (!valid || busy) return;
    onSubmit(code);
    setCode("");
  }

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !busy) {
          setCode("");
          onCancel();
        }
      }}
    >
      <AlertDialogContent
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          input.current?.focus();
        }}
      >
        <form onSubmit={submit} className="flex flex-col gap-4">
          <AlertDialogHeader>
            <AlertDialogTitle>{title}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div>{children}</div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="flex flex-col gap-1">
            <Label htmlFor="totp-code">Code from your authenticator app</Label>
            <Input
              ref={input}
              id="totp-code"
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="\d{6}"
              maxLength={6}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              aria-invalid={error !== null}
              aria-describedby={error === null ? undefined : "totp-error"}
              className="w-32 font-mono tracking-widest"
            />
          </div>
          {error !== null && (
            <p id="totp-error" role="alert" className="text-loss text-sm">
              {error}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel type="button" disabled={busy}>
              Cancel
            </AlertDialogCancel>
            <Button type="submit" disabled={!valid || busy}>
              {busy ? "Checking…" : confirm}
            </Button>
          </AlertDialogFooter>
        </form>
      </AlertDialogContent>
    </AlertDialog>
  );
}
