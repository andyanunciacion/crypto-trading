import { expect, test } from "@playwright/test";

import { credentials, freshCode, signIn } from "./auth";
import { psql } from "./stack";
import { totp } from "./totp";

/**
 * Settings (P5-7, D50). Done when keys are stored encrypted (checked in the database),
 * and raising a limit without a TOTP code is rejected.
 *
 * Starts from the seeded paper limits (ARCHITECTURE §6) and puts them back afterwards,
 * so the other specs see them unchanged. No exchange keys are stored at first or after.
 */

const SEED = `
  UPDATE risk_settings SET max_open_positions = 3, max_coin_exposure_pct = 25,
    max_daily_loss_pct = 3, max_drawdown_pct = 10, stale_after_candles = 2,
    max_failed_orders = 3, failed_orders_minutes = 10, spike_pct = 3, spike_minutes = 5
  WHERE mode = 'paper';
  TRUNCATE exchange_accounts RESTART IDENTITY;
`;
// Made up, never real keys; gitleaks:allow tells the secret scan so.
const KEY = "mx0vglE2ETESTKEY0123456789abQ7r5"; // gitleaks:allow
const SECRET = "e2eSECRETdoNotUse9876543210zyxwvu"; // gitleaks:allow

test.beforeAll(() => psql(SEED));
test.afterAll(() => psql(SEED));

function drawdown(): string {
  return psql("SELECT max_drawdown_pct FROM risk_settings WHERE mode = 'paper';");
}

test("lowering a limit saves; raising it needs a code", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: "Settings" }).click();
  await expect(page).toHaveURL("/settings");
  const field = page.getByRole("textbox", { name: "Max drawdown" });
  await expect(field).toHaveValue("10");

  // Lowering: no code asked.
  await field.fill("8");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved. It applies from the next buy.")).toBeVisible();
  expect(drawdown()).toBe("8");

  // Raising: the API refuses without a code, and the dialog says what it loosens.
  // Each save's code (none for the first), to check that one submit sends one save.
  const sent: (string | undefined)[] = [];
  page.on("request", (request) => {
    if (request.method() === "PUT" && request.url().endsWith("/api/risk-settings/paper")) {
      sent.push((request.postDataJSON() as { code?: string }).code);
    }
  });
  await field.fill("15");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const dialog = page.getByRole("alertdialog", { name: "Loosen the risk limits?" });
  await expect(dialog).toContainText("Max drawdown: 8% below the peak → 15% below the peak");
  expect(drawdown()).toBe("8");
  const code = dialog.getByLabel("Code from your authenticator app");
  await expect(code).toBeFocused();

  // A code from five minutes ago is refused, and nothing is saved. Enter submits it.
  const { secret } = credentials();
  const stale = totp(secret, new Date(Date.now() - 5 * 60_000));
  await code.fill(stale);
  await code.press("Enter");
  await expect(dialog.getByRole("alert")).toContainText("wrong or already used code");
  expect(drawdown()).toBe("8");

  // The right one saves it.
  const fresh = await freshCode(secret);
  await code.fill(fresh);
  await code.press("Enter");
  await expect(dialog).toBeHidden();
  await expect(page.getByText("Saved. It applies from the next buy.")).toBeVisible();
  expect(drawdown()).toBe("15");
  // One save per submit: the dialog's Enter mustn't also submit the form behind it
  // (React events bubble through portals; found by hand, 2026-10-28).
  expect(sent).toEqual([undefined, stale, fresh]);
});

test("exchange keys are stored encrypted and never shown again", async ({ page }) => {
  await signIn(page);
  await page.goto("/settings?tab=keys");
  await expect(page.getByText("No exchange keys stored.")).toBeVisible();

  const form = page.getByRole("form", { name: "Add exchange keys" });
  await form.getByLabel("Label").fill("e2e");
  await form.getByLabel("API key").fill(KEY);
  await form.getByLabel("Secret key").fill(SECRET);
  await form.getByRole("button", { name: "Save keys" }).click();

  const dialog = page.getByRole("alertdialog", { name: "Save the exchange keys?" });
  await dialog
    .getByLabel("Code from your authenticator app")
    .fill(await freshCode(credentials().secret));
  await dialog.getByRole("button", { name: "Save keys" }).click();
  await expect(page.getByText("Saved e2e's keys (key …Q7r5), encrypted.")).toBeVisible();

  // Listed by its hint; the typed keys are gone from the page.
  const row = page.getByRole("row", { name: /e2e/ });
  await expect(row).toContainText("MEXC");
  await expect(row).toContainText("…Q7r5");
  await expect(form.getByLabel("API key")).toHaveValue("");
  const html = await page.content();
  expect(html).not.toContain(KEY);
  expect(html).not.toContain(SECRET);

  // In the database: Fernet tokens, not the keys.
  const stored = psql(
    "SELECT label, key_hint, left(api_key_enc, 5), left(api_secret_enc, 5), " +
      `api_key_enc LIKE '%${KEY}%' OR api_secret_enc LIKE '%${SECRET}%' FROM exchange_accounts;`,
  );
  expect(stored).toBe("e2e|Q7r5|gAAAA|gAAAA|f");
});
