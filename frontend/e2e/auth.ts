import { expect, type Page } from "@playwright/test";

import { totp } from "./totp";

/** The e2e user's password and TOTP secret, made by global-setup.ts. */
export function credentials(): { password: string; secret: string } {
  const password = process.env.E2E_PASSWORD;
  const secret = process.env.E2E_TOTP_SECRET;
  if (!password || !secret) throw new Error("run through `pnpm e2e`: global-setup makes the user");
  return { password, secret };
}

const STEP_MS = 30_000;
// The last 30 s step whose code was used. The tests run one at a time in one worker
// (playwright.config.ts), so this module, and this value, is shared by every spec.
let lastStep = -1;

/**
 * A TOTP code nobody has used yet. The API accepts each step's code once, so a second
 * sign-in in the same 30 s step waits for the next one.
 */
export async function freshCode(secret: string): Promise<string> {
  let now = Date.now();
  if (Math.floor(now / STEP_MS) <= lastStep) {
    await new Promise((resolve) => setTimeout(resolve, (lastStep + 1) * STEP_MS - now + 500));
    now = Date.now();
  }
  lastStep = Math.floor(now / STEP_MS);
  return totp(secret, new Date(now));
}

/** Sign in as the e2e user, landing on the Overview. */
export async function signIn(page: Page): Promise<void> {
  const { password, secret } = credentials();
  await page.goto("/login");
  await page.getByLabel("Username").fill("e2e");
  await page.getByLabel("Password").fill(password);
  await page.getByLabel("Code").fill(await freshCode(secret));
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
}
