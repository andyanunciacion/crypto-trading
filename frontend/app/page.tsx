"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, me, signOut } from "@/lib/api";

export default function HomePage() {
  const router = useRouter();
  const [username, setUsername] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    me()
      .then((user) => {
        if (current) setUsername(user.username);
      })
      .catch((error: unknown) => {
        // The session expired or was ended elsewhere: sign in again.
        if (current && error instanceof ApiError && error.status === 401) {
          router.replace("/login?next=%2F");
        }
      });
    return () => {
      current = false;
    };
  }, [router]);

  async function signOutEverywhere() {
    await signOut().catch(() => undefined); // signed out already, or the API is down
    router.replace("/login");
  }

  return (
    <main className="mx-auto flex max-w-3xl flex-col gap-4 p-6">
      <header className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold">crypto-trading</h1>
        {username && (
          <div className="flex items-center gap-3 text-sm">
            <span className="text-muted-foreground">Signed in as {username}</span>
            <Button variant="outline" size="sm" onClick={signOutEverywhere}>
              Sign out
            </Button>
          </div>
        )}
      </header>
      <p className="text-muted-foreground">
        Dashboard placeholder. The real screens arrive in Phase 5.
      </p>
    </main>
  );
}
