import { expect, test } from "@playwright/test";

import { totp } from "./totp";

/** The e2e user's password and TOTP secret, made by global-setup.ts. */
function credentials(): { password: string; secret: string } {
  const password = process.env.E2E_PASSWORD;
  const secret = process.env.E2E_TOTP_SECRET;
  if (!password || !secret) throw new Error("run through `pnpm e2e`: global-setup makes the user");
  return { password, secret };
}

test("the API refuses a caller without a session", async ({ request }) => {
  const response = await request.get("/api/auth/me");
  expect(response.status()).toBe(401);
});

test("signs in with a password and a TOTP code, and out again", async ({ page, context }) => {
  const { password, secret } = credentials();

  // Not signed in: any page sends you to sign in, and back afterwards.
  await page.goto("/");
  await expect(page).toHaveURL(/\/login\?next=%2F$/);

  await page.getByLabel("Username").fill("e2e");
  await page.getByLabel("Password").fill(password);
  const code = totp(secret);
  const wrong = `${code.slice(0, 5)}${(Number(code[5]) + 1) % 10}`;
  await page.getByLabel("Code").fill(wrong);
  await page.getByRole("button", { name: "Sign in" }).click();
  // Ours, not Next's route announcer (also role="alert").
  await expect(page.getByRole("alert").filter({ hasText: "Wrong" })).toHaveText(
    "Wrong username, password or code.",
  );
  // Ready for the next code: cleared and focused.
  await expect(page.getByLabel("Code")).toHaveValue("");
  await expect(page.getByLabel("Code")).toBeFocused();

  await page.getByLabel("Code").fill(totp(secret));
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("/");
  await expect(page.getByText("Signed in as e2e")).toBeVisible();

  // The session cookie can't be read by the page's scripts, and is never sent cross-site.
  const session = (await context.cookies()).find((cookie) => cookie.name === "session");
  expect(session).toMatchObject({ httpOnly: true, secure: true, sameSite: "Strict" });
  expect(await page.evaluate(() => document.cookie)).not.toContain("session=");

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
  // Signing out ended the session itself, not just the browser's copy of the cookie.
  const replay = await page.request.get("/api/auth/me", {
    headers: { cookie: `session=${session?.value}` },
  });
  expect(replay.status()).toBe(401);
});
