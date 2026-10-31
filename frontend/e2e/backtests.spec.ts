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

/**
 * P6-5c's "done when": an auto-switching backtest from the form is the run
 * `engine.backtest.run --auto-switch` makes with the same arguments, baselines included.
 * The form's starting mapping, each strategy with its defaults, a hold of 3 and `keep`.
 */
test("an auto-switching backtest from the page is the CLI's, baselines and all", async ({
  page,
}) => {
  test.setTimeout(240_000);
  await signIn(page);
  await page.goto("/backtests");

  const form = page.getByRole("form", { name: "New backtest" });
  await form.getByLabel("Timeframe").selectOption("4h");
  await form.getByLabel("Days").fill("60");
  await expect(form.getByLabel("Strategy when ranging")).toBeHidden();
  await form.getByLabel("Switch strategy by regime").check();
  await expect(form.getByLabel("Strategy when trending up")).toHaveValue("ema_crossover");
  await expect(form.getByLabel("Strategy when trending down")).toHaveValue("stay_flat");
  await expect(form.getByLabel("Strategy when ranging")).toHaveValue("rsi_mean_reversion");
  await expect(form.getByLabel("Strategy when high volatility")).toHaveValue("stay_flat");
  await expect(form.getByLabel("Minimum hold, in candles")).toHaveValue("3");
  await expect(form.getByRole("radio", { name: /^Keep/ })).toBeChecked();
  await expect(form.getByTestId("stored-range")).toContainText("199 candles");
  await form.getByLabel("Minimum hold, in candles").fill("0");
  await expect(form.getByRole("button", { name: "Run backtest" })).toBeDisabled();
  await form.getByLabel("Minimum hold, in candles").fill("3");
  await form.getByRole("button", { name: "Run backtest" }).click();

  await expect(page).toHaveURL(/\/backtests\/\d+$/, { timeout: 180_000 });
  const uiRun = Number(new URL(page.url()).pathname.split("/").pop());
  await expect(page.getByRole("heading", { name: /auto-switching on BTC\/USDT 4h/ })).toBeVisible();
  // The run and its baselines: EMA (where it starts, and the uptrend's), stay flat (two
  // regimes'), RSI, and buy-and-hold.
  const table = page.getByRole("table", { name: "Against each config alone" });
  await expect(table.locator("tbody tr")).toHaveCount(5);
  const returns = await table
    .locator('[data-metric="total_return"]')
    .evaluateAll((cells) => cells.map((cell) => cell.getAttribute("data-value") ?? ""));

  const autoSwitch = {
    mapping: {
      trending_up: { strategy: "ema_crossover" },
      trending_down: { strategy: "stay_flat" },
      ranging: { strategy: "rsi_mean_reversion" },
      high_volatility: { strategy: "stay_flat" },
    },
    min_hold: 3,
    policy: "keep",
  };
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
    "--auto-switch",
    JSON.stringify(autoSwitch),
  ]);
  const cliRun = Number(/^run (\d+):/m.exec(printed)?.[1]);
  expect(cliRun).toBeGreaterThan(uiRun);
  // Every return the page ranks, the CLI prints, best first both times.
  expect(printed).toContain("against each config alone, over the same candles:");
  for (const value of returns) {
    expect(printed).toContain(`${fixed(shiftPoint(value, 2), 2, true)}%  max drawdown`);
  }

  // The same mapping (versions filled in), range, candles, fills and baselines.
  const [ui, cli] = psql(
    `SELECT auto_switch::text, range_start, range_end, candles_sha256, md5(result::text),
            md5(baselines::text)
       FROM backtest_runs WHERE id IN (${uiRun}, ${cliRun}) ORDER BY id`,
  ).split("\n");
  expect(ui).toContain('"version": 1');
  expect(cli).toBe(ui);
});
