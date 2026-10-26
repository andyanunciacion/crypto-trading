import { expect, test } from "@playwright/test";

import { signIn } from "./auth";
import { psql } from "./stack";

/**
 * The Market chart (D46). The engine runs e2e-bot (1h EMA crossover on BTC/USDT) and
 * has stored its warm-up candles; it also streams the candle forming on MEXC, so the
 * newest bar moves. This runs before the kill switch test stops the bot
 * (playwright.config.ts).
 *
 * The bot hasn't traded, so one filled buy is written straight into the e2e database,
 * in the newest stored candle at its close, with the reason a bot's buy records.
 */

const REASON = {
  signal: {
    action: "buy",
    ts: "2026-10-26T08:00:00Z",
    reason: "fast EMA crossed above slow EMA",
    indicators: { prev_fast_ema: 99.5, prev_slow_ema: 100, fast_ema: 100.5, slow_ema: 100.25 },
    size_hint: null,
    stop: null,
  },
  bot: { name: "e2e-bot" },
  risk: { decision: "allowed", rule: "limits", detail: "within every limit" },
};

let orderId: number;

test.beforeAll(async () => {
  // The bot's warm-up candles are stored once the engine has fetched them.
  await expect
    .poll(() => Number(psql("SELECT count(*) FROM candles WHERE timeframe = '1h';")), {
      timeout: 60_000,
    })
    .toBeGreaterThan(0);
  orderId = Number(
    psql(`
      WITH last AS (
        SELECT ts, close FROM candles WHERE symbol = 'BTC/USDT' AND timeframe = '1h'
        ORDER BY ts DESC LIMIT 1
      ), placed AS (
        INSERT INTO orders (bot_id, mode, exchange, symbol, client_order_id, side, type,
                            price, amount, filled, status, reason)
        SELECT b.id, 'paper', 'mexc', 'BTC/USDT', 'e2e-chart-b', 'buy', 'limit',
               last.close, 0.001, 0.001, 'closed', '${JSON.stringify(REASON)}'::jsonb
        FROM bots b, last WHERE b.name = 'e2e-bot'
        RETURNING id, price
      )
      INSERT INTO fills (order_id, ts, price, amount, fee, fee_currency)
      SELECT placed.id, last.ts + interval '5 minutes', placed.price, 0.001, 0, 'USDT'
      FROM placed, last
      RETURNING order_id;
    `)
      .split("\n")
      .find((line) => /^\d+$/.test(line)), // not the command tag psql may print after it
  );
  expect(orderId).toBeGreaterThan(0);
});

test("live candles move, and a trade marker explains its trade", async ({ page }) => {
  await signIn(page);
  await page
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", { name: "Market" })
    .click();
  await expect(page).toHaveURL(/\/market/);

  const chart = page.getByTestId("candle-chart");
  await expect(chart).toHaveAttribute("data-last-bar", /.+/);
  await expect(page.getByText(/^Live: forming candle/)).toBeVisible();
  // The forming candle moves with the market (MEXC sends it several times a second).
  const first = await chart.getAttribute("data-last-bar");
  await expect.poll(() => chart.getAttribute("data-last-bar"), { timeout: 90_000 }).not.toBe(first);

  // A canvas can't be searched: the chart says where it drew the trade's marker.
  const point = await chart.evaluate(
    (element, id) =>
      (
        element as HTMLElement & {
          markerPoint?: (orderId: number) => { x: number; y: number } | null;
        }
      ).markerPoint?.(id) ?? null,
    orderId,
  );
  expect(point).not.toBeNull();
  await chart.click({ position: point! });

  const panel = page.getByTestId("trade-explain-panel");
  await expect(panel.getByRole("heading", { name: "Buy BTC/USDT" })).toBeVisible();
  const signal = panel.getByRole("region", { name: "Signal" });
  await expect(signal.getByText("fast EMA crossed above slow EMA")).toBeVisible();
  const risk = panel.getByRole("region", { name: "Risk check" });
  await expect(risk.getByText("allowed", { exact: true })).toBeVisible();
  await expect(risk.getByText("within every limit")).toBeVisible();

  // The trades table opens the same panel from the keyboard.
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  const explainButton = page.getByRole("button", { name: `Explain the buy of order ${orderId}` });
  await explainButton.focus();
  await page.keyboard.press("Enter");
  await expect(signal.getByText("fast EMA crossed above slow EMA")).toBeVisible();
  // Closing it returns focus to that row, so the keyboard doesn't lose its place.
  await page.keyboard.press("Escape");
  await expect(explainButton).toBeFocused();
});
