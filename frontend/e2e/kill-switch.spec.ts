import { expect, test } from "@playwright/test";

import { signIn } from "./auth";

/**
 * The kill switch from the dashboard (UX1, D43, D45). global-setup.ts made one running
 * paper bot, e2e-bot, before starting the engine, so the engine is running it.
 */
test("the kill switch in the header stops every bot", async ({ page }) => {
  await signIn(page);

  // The header: the mode, a live engine, and the kill switch.
  const header = page.getByRole("banner");
  await expect(header.getByText("PAPER", { exact: true })).toBeVisible();
  await expect(header.getByRole("button", { name: /^Engine: \d+ s ago$/ })).toBeVisible();
  const bot = page.locator('tr[data-bot="e2e-bot"]');
  await expect(bot).toContainText("running");

  await header.getByRole("button", { name: "Kill switch" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Kill switch" });
  // Focus moves into the dialog, onto the harmless choice (DESIGN §10).
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();

  const clicked = Date.now();
  await dialog.getByRole("button", { name: "Stop all bots", exact: true }).click();
  await expect(dialog.getByText("Stopped 1 bot: e2e-bot")).toBeVisible();
  // DESIGN §9: the engine acknowledges within 2 s of the click.
  expect(Date.now() - clicked).toBeLessThan(2000);
  await expect(dialog.getByText(/^Done in \d+ ms\.$/)).toBeVisible();

  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toBeHidden();
  // The live table reads the database: the engine stopped the bot, and says why.
  await expect(bot).toContainText("stopped");
  await expect(bot).toContainText("kill switch from dashboard");
});
