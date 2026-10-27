import { expect, test } from "@playwright/test";

import { shiftPoint } from "../lib/format";
import { signIn } from "./auth";
import { composeOutput, psql } from "./stack";

/**
 * The Backtests page (D48), and P5-5's "done when": a backtest run from the dashboard
 * shows the same metrics as the CLI's run with the same inputs. It runs EMA crossover
 * (its defaults) over the last 60 days of BTC/USDT 4h, which global setup backfilled and
 * nothing else writes, from the page and then with `python -m engine.backtest.run`;
 * every number the CLI prints must be what the page shows, and both stored runs must be
 * the same run.
 */

/** A decimal string as Python's f"{value:.{places}f}" writes it (half-even, like Decimal). */
function fixed(value: string, places: number, sign = false): string {
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: places,
    maximumFractionDigits: places,
    roundingMode: "halfEven",
    signDisplay: sign ? "always" : "auto",
    useGrouping: false,
  }).format(value as Intl.StringNumericLiteral);
}

test("a backtest from the page shows the CLI's metrics for the same inputs", async ({ page }) => {
  test.setTimeout(180_000);
  await signIn(page);
  await page
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", { name: "Backtests" })
    .click();
  await expect(page).toHaveURL("/backtests");

  const form = page.getByRole("form", { name: "New backtest" });
  // The strategy's params come from the catalog, with its defaults; the costs are the CLI's.
  await expect(form.getByLabel("fast", { exact: true })).toHaveValue("9");
  await expect(form.getByLabel("Fee rate")).toHaveValue("0.001");
  await form.getByLabel("Timeframe").selectOption("4h");
  await form.getByLabel("Days").fill("60");
  await expect(form.getByTestId("stored-range")).toContainText("Stored:");
  await form.getByRole("button", { name: "Run backtest" }).click();

  await expect(page).toHaveURL(/\/backtests\/\d+$/, { timeout: 120_000 });
  const uiRun = Number(new URL(page.url()).pathname.split("/").pop());
  const value = async (name: string) =>
    (await page.locator(`[data-metric="${name}"]`).getAttribute("data-value")) ?? "";
  await expect(page.locator('[data-metric="total_return"]')).toBeVisible();
  await expect(page.getByTestId("equity-chart")).toBeVisible();
  const shown = {
    totalReturn: await value("total_return"),
    maxDrawdown: await value("max_drawdown"),
    sharpe: await value("sharpe"),
    trades: await value("trade_count"),
    winRate: await value("win_rate"),
    profitFactor: await value("profit_factor"),
  };

  // The CLI, with the same arguments (its defaults are the form's).
  const printed = composeOutput([
    "run",
    "--rm",
    "-T",
    "engine",
    "python",
    "-m",
    "engine.backtest.run",
    "ema_crossover",
    "BTC/USDT",
    "4h",
    "--days",
    "60",
  ]);
  const cliRun = Number(/^run (\d+):/m.exec(printed)?.[1]);
  expect(cliRun).toBeGreaterThan(uiRun);

  expect(printed).toContain(`(${fixed(shiftPoint(shown.totalReturn, 2), 2, true)}%)`);
  const sharpe = shown.sharpe === "" ? "n/a" : fixed(shown.sharpe, 2);
  expect(printed).toContain(
    `max drawdown ${fixed(shiftPoint(shown.maxDrawdown, 2), 2)}%, Sharpe ${sharpe} (annualized)`,
  );
  if (shown.winRate === "") {
    expect(printed).toContain("no closed trades");
  } else {
    const noun = shown.trades === "1" ? "trade" : "trades";
    const factor =
      shown.profitFactor === "" ? "n/a (no losing trades)" : fixed(shown.profitFactor, 2);
    expect(printed).toContain(
      `${shown.trades} ${noun}, ${fixed(shiftPoint(shown.winRate, 2), 1)}% won, profit factor ${factor}`,
    );
  }

  // And they're the same run: the same range and candles, the same fills to the digit.
  const [ui, cli] = psql(
    `SELECT range_start, range_end, candles_sha256, final_equity, md5(result::text)
       FROM backtest_runs WHERE id IN (${uiRun}, ${cliRun}) ORDER BY id`,
  ).split("\n");
  expect(cli).toBe(ui);
});
