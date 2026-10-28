import { expect, test, type Page } from "@playwright/test";

import { psql } from "./stack";
import { totp } from "./totp";

/** The e2e user's password and TOTP secret, made by global-setup.ts. */
export function credentials(): { password: string; secret: string } {
  const password = process.env.E2E_PASSWORD;
  const secret = process.env.E2E_TOTP_SECRET;
  if (!password || !secret) throw new Error("run through `pnpm e2e`: global-setup makes the user");
  return { password, secret };
}

const STEP_MS = 30_000;
// The last 30 s step whose code this worker handed out (it may not be submitted yet).
let lastStep = -1;

/** The step of the last code the API accepted for the e2e user, from the database.
 * Each Playwright project runs in its own worker, so memory alone can't know it. */
function acceptedStep(): number {
  const step = psql("SELECT coalesce(totp_last_step, -1) FROM users WHERE username = 'e2e';");
  return Number(step);
}

/**
 * A TOTP code nobody has used yet. The API accepts each step's code once, so a sign-in
 * in a step whose code was already used waits for the next one. That wait (up to 30 s)
 * is added to the test's time limit: it's the clock's, not the app's, and counting it
 * timed out a test that signed in just after another (the kill switch, after P5-6's).
 */
export async function freshCode(secret: string): Promise<string> {
  const used = Math.max(lastStep, acceptedStep());
  let now = Date.now();
  if (Math.floor(now / STEP_MS) <= used) {
    const wait = (used + 1) * STEP_MS - now + 500;
    test.info().setTimeout(test.info().timeout + wait);
    await new Promise((resolve) => setTimeout(resolve, wait));
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
