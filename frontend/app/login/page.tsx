"use client";

import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, signIn } from "@/lib/api";

/** Where to go after signing in: the page that sent us here, if it's one of ours. */
function nextPath(): string {
  const next = new URLSearchParams(window.location.search).get("next");
  // Only a path on this site: "//evil.example" or "https://..." would be an open redirect.
  return next !== null && next.startsWith("/") && !next.startsWith("//") ? next : "/";
}

function message(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Wrong username, password or code.";
    if (error.status === 429) return `Locked: ${error.message}.`;
    return `Couldn't sign in (${error.message}).`;
  }
  return "Couldn't reach the API. Is it running?";
}

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const codeInput = useRef<HTMLInputElement>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(username, password, code);
      router.replace(nextPath());
    } catch (caught) {
      setError(message(caught));
      setCode(""); // a code only works once, so a new one is needed anyway
      setBusy(false);
      codeInput.current?.focus(); // ready for the next code, without reaching for the mouse
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>
            <h1 className="text-xl">crypto-trading</h1>
          </CardTitle>
          <CardDescription>
            Sign in with your password and a code from your authenticator app.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
            <div className="flex flex-col gap-2">
              <Label htmlFor="username">Username</Label>
              <Input
                id="username"
                autoComplete="username"
                autoFocus
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="code">Code</Label>
              <Input
                id="code"
                ref={codeInput}
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                maxLength={6}
                required
                className="font-mono tracking-widest tabular-nums"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                aria-describedby={error ? "sign-in-error" : undefined}
                aria-invalid={error ? true : undefined}
              />
            </div>
            {error && (
              <p id="sign-in-error" role="alert" className="text-destructive text-sm">
                {error}
              </p>
            )}
            <Button type="submit" disabled={busy || !username || !password || code.length !== 6}>
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
