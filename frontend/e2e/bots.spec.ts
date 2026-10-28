import { expect, test } from "@playwright/test";

import { signIn } from "./auth";

/**
 * The Bots page (D47): a paper bot is created, started, switched to another strategy
 * (D51) and stopped from the dashboard, each as a command the engine carries out, with
 * no engine restart. It trades ETH/USDT,
 * which nothing else does, so starting it also starts a new candle series and price
 * stream. It ends stopped, so the kill switch test after it still stops only e2e-bot.
 */
test("a paper bot is created, started, switched and stopped from the Bots page", async ({
  page,
}) => {
  await signIn(page);
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Bots" }).click();
  await expect(page).toHaveURL("/bots");

  await page.getByRole("button", { name: "New bot" }).click();
  const sheet = page.getByRole("dialog", { name: "New bot" });
  // The params are drawn from the strategy's pydantic model, with its defaults.
  await expect(sheet.getByLabel("fast", { exact: true })).toHaveValue("9");
  await expect(sheet.getByLabel("slow", { exact: true })).toHaveValue("21");
  await sheet.getByLabel("Name").fill("e2e-eth");
  await sheet.getByLabel("Symbol").fill("ETH/USDT");

  // The engine checks what the form can't: fast must be shorter than slow.
  await sheet.getByLabel("fast", { exact: true }).fill("30");
  await sheet.getByRole("button", { name: "Create bot" }).click();
  await expect(sheet.getByRole("alert")).toContainText("fast (30) must be shorter than slow (21)");

  await sheet.getByLabel("fast", { exact: true }).fill("9");
  await sheet.getByLabel("Stop-loss").selectOption("percent");
  await sheet.getByLabel("percent", { exact: true }).fill("3");
  await sheet.getByRole("button", { name: "Create bot" }).click();
  await expect(sheet).toBeHidden();
  await expect(page.getByText(/^Created e2e-eth, stopped/)).toBeVisible();

  const row = page.locator('tr[data-bot="e2e-eth"]');
  await expect(row).toContainText("stopped");
  await expect(row).toContainText("fast 9, slow 21");
  await expect(row).toContainText("stop-loss 3%");
  await expect(row.getByRole("cell", { name: "1,000.00USDT" })).toBeVisible(); // its paper cash

  // Started: running at once in the database; in the engine once warmed up, as its
  // next heartbeat (every 10 s) says.
  await row.getByRole("button", { name: "Start e2e-eth" }).click();
  await expect(page.getByText(/^Started e2e-eth/)).toBeVisible();
  await expect(row).toContainText("running");
  await expect(row.locator('[data-engine="running"]')).toBeVisible({ timeout: 30_000 });
  // A new symbol: the engine streams its prices now (the heartbeat lists them).
  const overview = (await (await page.request.get("/api/overview")).json()) as {
    engine: { symbols: string[] };
  };
  expect(overview.engine.symbols).toContain("ETH/USDT");

  // Switched while it runs (D51), from the keyboard: the sheet starts from what it runs
  // now, and nothing changed means nothing to send.
  await row.getByRole("button", { name: "Switch e2e-eth's strategy" }).focus();
  await page.keyboard.press("Enter");
  const edit = page.getByRole("dialog", { name: "Switch e2e-eth's strategy" });
  await expect(edit.getByLabel("fast", { exact: true })).toHaveValue("9");
  await expect(edit.getByRole("radio", { name: /^Keep/ })).toBeChecked(); // the default policy
  await expect(edit.getByRole("button", { name: "Save policy" })).toBeDisabled();
  await edit.getByLabel("Strategy", { exact: true }).selectOption("rsi_mean_reversion");
  await edit.getByLabel("period", { exact: true }).fill("7");
  await edit.getByRole("radio", { name: /^Hand over/ }).check();
  await edit.getByLabel("period", { exact: true }).press("Enter");
  await expect(edit).toBeHidden();
  await expect(page.getByText(/^Switched e2e-eth to rsi_mean_reversion/)).toBeVisible();
  await expect(row).toContainText("period 7, oversold 30, overbought 70");
  await expect(row).toContainText("on a switch: hand over");
  await expect(row.locator('[data-engine="running"]')).toBeVisible(); // no restart

  await row.getByRole("button", { name: "Stop e2e-eth" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Stop e2e-eth?" });
  // Focus starts on the harmless choice, as in the kill switch (DESIGN §10).
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  await dialog.getByRole("button", { name: "Stop", exact: true }).click();
  await expect(page.getByText(/^Stopped e2e-eth/)).toBeVisible();
  await expect(row).toContainText("stopped from dashboard");
  await expect(row.locator("[data-engine]")).toHaveCount(0);
});
