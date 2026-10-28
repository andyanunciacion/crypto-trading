import { expect, test } from "@playwright/test";

import { signIn } from "./auth";

/**
 * The Bots page (D47): a paper bot is created, started, switched to another strategy
 * (D51), rolled back to its first from its detail page (D52), has auto-switching turned
 * on and off there (D54) and is stopped from the
 * dashboard, each as a command the engine carries out, with no engine restart. It
 * trades ETH/USDT,
 * which nothing else does, so starting it also starts a new candle series and price
 * stream. It ends stopped, so the kill switch test after it still stops only e2e-bot.
 */
test("a paper bot is created, started, switched, rolled back and stopped", async ({ page }) => {
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

  // Its history (D52): the name opens its detail page, which lists both configs. Rolled
  // back to the first from the keyboard, it runs EMA 9/21 again as a third config, and
  // the first two stay listed.
  await row.getByRole("link", { name: "e2e-eth" }).click();
  await expect(page).toHaveURL(/\/bots\/\d+$/);
  await expect(page.getByRole("heading", { name: "e2e-eth" })).toBeVisible();
  const history = page.getByRole("table", { name: "Strategy history" });
  const first = history.locator('tr[data-config="1"]');
  const second = history.locator('tr[data-config="2"]');
  await expect(second).toContainText("rsi_mean_reversion");
  await expect(second).toContainText("running now");
  await expect(first).toContainText("fast 9, slow 21");
  await expect(second.getByRole("button", { name: /^Roll back/ })).toHaveCount(0);

  await first.getByRole("button", { name: "Roll back e2e-eth to #1" }).focus();
  await page.keyboard.press("Enter");
  const back = page.getByRole("alertdialog", { name: "Roll back e2e-eth to #1?" });
  await expect(back.getByRole("button", { name: "Cancel" })).toBeFocused();
  await expect(back.getByRole("radio", { name: /^Hand over/ })).toBeChecked(); // its policy now
  await back.getByRole("radio", { name: /^Keep/ }).check();
  await back.getByRole("button", { name: "Roll back", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(back).toBeHidden();
  await expect(page.getByText(/^Rolled e2e-eth back to #1, ema_crossover/)).toBeFocused();
  const third = history.locator('tr[data-config="3"]');
  await expect(third).toContainText("ema_crossover");
  await expect(third).toContainText("fast 9, slow 21");
  await expect(third).toContainText("running now");
  await expect(third).toContainText("rollback to #1");
  await expect(second).not.toContainText("running now");
  // #1 is what it runs again, so there's nothing to roll back to there.
  await expect(first).toContainText("same as running now");
  await expect(first.getByRole("button", { name: /^Roll back/ })).toHaveCount(0);
  await expect(history.locator("tbody tr")).toHaveCount(3);

  // The switch log (P6-4) lists both, newest first, with what made them.
  const log = page.getByRole("table", { name: "Switch log" });
  await expect(log.locator("tbody tr")).toHaveCount(2);
  await expect(log.locator('tr[data-switch="3"]')).toContainText("#2 → #3");
  await expect(log.locator('tr[data-switch="3"]')).toContainText("rollback");
  await expect(log.locator('tr[data-switch="2"]')).toContainText("#1 → #2");
  await expect(log.locator('tr[data-switch="2"]')).toContainText("by hand");

  // Auto-switching (P6-4), set up from the keyboard. Every regime maps to EMA 9/21, what
  // it runs now, so a candle closing mid-test can't switch it.
  await page.getByRole("button", { name: "Set up auto-switching" }).focus();
  await page.keyboard.press("Enter");
  const auto = page.getByRole("dialog", { name: "Auto-switching for e2e-eth" });
  await expect(auto.getByLabel("Strategy when ranging")).toHaveValue("rsi_mean_reversion");
  await expect(auto.getByLabel("Strategy when trending up")).toHaveValue("ema_crossover");
  for (const regime of ["trending down", "ranging", "high volatility"]) {
    await auto.getByLabel(`Strategy when ${regime}`).selectOption("ema_crossover");
  }
  await auto.getByLabel("Minimum hold, in candles").fill("0");
  await expect(auto.getByRole("button", { name: "Turn on" })).toBeDisabled();
  await auto.getByLabel("Minimum hold, in candles").fill("5");
  await auto.getByLabel("Minimum hold, in candles").press("Enter");
  await expect(auto).toBeHidden();
  await expect(page.getByText(/^Auto-switching is on for e2e-eth/)).toBeFocused();
  const mapping = page.getByRole("table", { name: "Auto-switching" });
  await expect(mapping.locator('tr[data-regime="ranging"]')).toContainText("fast 9, slow 21");
  await expect(page.getByText(/held for 5 closed candles in a row/)).toBeVisible();
  // A switch by hand would be undone by the regime, so it's off while this is on.
  await expect(page.getByRole("button", { name: "Switch e2e-eth's strategy" })).toBeDisabled();
  await expect(second.getByRole("button", { name: "Roll back e2e-eth to #2" })).toBeDisabled();
  await expect(second).toContainText("turn auto-switching off to roll back");
  await expect(history.locator("tbody tr")).toHaveCount(3); // no config added

  await page.getByRole("button", { name: "Turn off" }).click();
  await expect(page.getByText(/^Auto-switching is off for e2e-eth/)).toBeFocused();
  await expect(page.getByRole("button", { name: "Switch e2e-eth's strategy" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Set up auto-switching" })).toBeVisible();

  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Bots" }).click();
  await expect(row).toContainText("fast 9, slow 21");
  await expect(row).toContainText("on a switch: keep");
  await expect(row.locator('[data-engine="running"]')).toBeVisible(); // still no restart

  await row.getByRole("button", { name: "Stop e2e-eth" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Stop e2e-eth?" });
  // Focus starts on the harmless choice, as in the kill switch (DESIGN §10).
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  await dialog.getByRole("button", { name: "Stop", exact: true }).click();
  await expect(page.getByText(/^Stopped e2e-eth/)).toBeVisible();
  await expect(row).toContainText("stopped from dashboard");
  await expect(row.locator("[data-engine]")).toHaveCount(0);
});
