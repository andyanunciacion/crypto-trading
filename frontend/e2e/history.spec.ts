import { readFile } from "node:fs/promises";

import { expect, test, type Page } from "@playwright/test";

import { signIn } from "./auth";
import { psql } from "./stack";

/**
 * History and Logs (P5-6, D49). Done when the CSV export matches the database's rows for
 * a filtered range.
 *
 * Four of e2e-bot's orders are written straight into the database, in September (away
 * from anything a bot does now), around Manila midnights (UTC+8):
 * - A at 23:59:59.5 on 10 Sep, Manila;
 * - B at exactly 00:00 on 11 Sep, Manila;
 * - C later on 11 Sep, filled in two slices, one with a fee of 0.00000001;
 * - D at exactly 00:00 on 12 Sep.
 * Filtering on 11 Sep (Manila) must give B and C, and their fills, nothing else. The
 * expected CSV is built by Postgres itself, with each value as its own exact text.
 */

const REASON = JSON.stringify({ signal: { action: "buy", reason: "e2e" } });
const START = "2026-09-10T16:00:00Z"; // 11 Sep 00:00 in Manila
const END = "2026-09-11T16:00:00Z"; // 12 Sep 00:00 in Manila: excluded

test.beforeAll(() => {
  psql(`
    DELETE FROM fills WHERE order_id IN (SELECT id FROM orders WHERE client_order_id LIKE 'e2e-hist-%');
    DELETE FROM orders WHERE client_order_id LIKE 'e2e-hist-%';
    WITH bot AS (SELECT id FROM bots WHERE name = 'e2e-bot'),
    placed AS (
      INSERT INTO orders (bot_id, mode, exchange, symbol, client_order_id, side, type, price,
                          amount, filled, status, reason, created_at, updated_at)
      SELECT bot.id, 'paper', 'mexc', 'BTC/USDT', 'e2e-hist-' || o.name, o.side, 'limit',
             o.price, o.amount, o.filled, o.status, '${REASON}'::jsonb, o.at, o.at
      FROM bot, (VALUES
        ('a', 'buy', 64000.5, 0.002, 0.002, 'closed', timestamptz '2026-09-10T15:59:59.5Z'),
        ('b', 'sell', 64100.25, 0.002, 0.002, 'closed', timestamptz '2026-09-10T16:00:00Z'),
        ('c', 'buy', 63950, 0.003, 0.003, 'closed', timestamptz '2026-09-11T10:00:00Z'),
        ('d', 'buy', 63000, 0.001, 0, 'canceled', timestamptz '2026-09-11T16:00:00Z')
      ) AS o(name, side, price, amount, filled, status, at)
      RETURNING id, client_order_id, price, created_at
    )
    INSERT INTO fills (order_id, ts, price, amount, fee, fee_currency)
    SELECT placed.id, placed.created_at + f.after, placed.price, f.amount, f.fee, 'USDT'
    FROM placed JOIN (VALUES
      ('e2e-hist-a', interval '0.4 s', 0.002, 0.128001),
      ('e2e-hist-b', interval '1 s', 0.002, 0.1282005),
      ('e2e-hist-c', interval '2 s', 0.001, 0.06395),
      ('e2e-hist-c', interval '3 s', 0.002, 0.00000001)
    ) AS f(coid, after, amount, fee) ON f.coid = placed.client_order_id;
  `);
});

const TS = (column: string) =>
  `to_char(${column} AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"')`;

/** The database's own rows for the range, as CSV text (the same columns as the export). */
function expectedCsv(table: "orders" | "fills"): string {
  const sql =
    table === "orders"
      ? `SELECT o.id AS id, ${TS("o.created_at")} AS created_at, ${TS("o.updated_at")} AS updated_at,
                o.bot_id AS bot_id, b.name AS bot_name, o.mode AS mode, o.exchange AS exchange,
                o.symbol AS symbol, o.client_order_id AS client_order_id, o.side AS side,
                coalesce(o.reason->'exit'->>'kind', 'signal') AS kind, o.status AS status,
                o.price::text AS price, o.amount::text AS amount, o.filled::text AS filled,
                (SELECT trim_scale(sum(f.price * f.amount) / sum(f.amount))::text
                   FROM fills f WHERE f.order_id = o.id) AS avg_fill_price,
                (SELECT CASE WHEN count(DISTINCT f.fee_currency) = 1 THEN sum(f.fee)::text END
                   FROM fills f WHERE f.order_id = o.id) AS fees,
                (SELECT CASE WHEN count(DISTINCT f.fee_currency) = 1 THEN min(f.fee_currency) END
                   FROM fills f WHERE f.order_id = o.id) AS fee_currency,
                o.reject_reason AS reject_reason
           FROM orders o JOIN bots b ON b.id = o.bot_id
          WHERE o.created_at >= '${START}' AND o.created_at < '${END}'
          ORDER BY o.id DESC`
      : `SELECT f.id AS id, ${TS("f.ts")} AS ts, f.order_id AS order_id,
                o.client_order_id AS client_order_id, o.bot_id AS bot_id, b.name AS bot_name,
                o.symbol AS symbol, o.side AS side,
                coalesce(o.reason->'exit'->>'kind', 'signal') AS kind,
                f.price::text AS price, f.amount::text AS amount,
                (f.price * f.amount)::text AS value, f.fee::text AS fee,
                f.fee_currency AS fee_currency
           FROM fills f JOIN orders o ON o.id = f.order_id JOIN bots b ON b.id = o.bot_id
          WHERE f.ts >= '${START}' AND f.ts < '${END}'
          ORDER BY f.id DESC`;
  return psql(`COPY (${sql}) TO STDOUT WITH (FORMAT csv, HEADER);`);
}

/** CSV text as rows of cells. None of these values holds a comma or a quote. */
function cells(csv: string): string[][] {
  return csv
    .trim()
    .split(/\r?\n/)
    .map((line) => line.split(","));
}

async function exportCsv(page: Page): Promise<{ name: string; text: string }> {
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("link", { name: "Export CSV" }).click(),
  ]);
  return {
    name: download.suggestedFilename(),
    text: await readFile(await download.path(), "utf8"),
  };
}

test("the CSV export of a filtered range matches the database's rows", async ({ page }) => {
  await signIn(page);
  await page
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", { name: "History" })
    .click();
  await expect(page).toHaveURL(/\/history/);

  await page.getByLabel("From (Manila day)").fill("2026-09-11");
  await page.getByLabel("To (Manila day, included)").fill("2026-09-11");
  await expect(page).toHaveURL(/from=2026-09-11&to=2026-09-11/);

  // B and C only: A was a moment before Manila's midnight, D exactly at the next one.
  const ids = psql(
    "SELECT id FROM orders WHERE client_order_id IN ('e2e-hist-b', 'e2e-hist-c') ORDER BY id DESC;",
  ).split("\n");
  const rows = page.locator("tr[data-order]");
  await expect(rows).toHaveCount(2);
  expect(await rows.evaluateAll((trs) => trs.map((tr) => tr.getAttribute("data-order")))).toEqual(
    ids,
  );

  const orders = await exportCsv(page);
  expect(orders.name).toBe("orders_2026-09-11_to_2026-09-11.csv");
  expect(cells(orders.text)).toEqual(cells(expectedCsv("orders")));
  expect(cells(orders.text)).toHaveLength(3); // the header, B and C

  // A row's time explains its order (UX9).
  await rows.first().getByRole("button").click();
  await expect(page.getByTestId("trade-explain-panel")).toBeVisible();
  await page.keyboard.press("Escape");

  await page.getByRole("link", { name: "Fills" }).click();
  await expect(page.locator("tr[data-fill]")).toHaveCount(3);
  const fills = await exportCsv(page);
  expect(fills.name).toBe("fills_2026-09-11_to_2026-09-11.csv");
  expect(cells(fills.text)).toEqual(cells(expectedCsv("fills")));
  expect(fills.text).toContain(",0.00000001,USDT"); // exact, never 1E-8
});

test("the Logs page shows the services' lines and the risk events", async ({ page }) => {
  psql(`
    DELETE FROM risk_events WHERE client_order_id = 'e2e-logs-b';
    INSERT INTO risk_events (ts, mode, bot_id, client_order_id, rule, decision, detail, inputs)
    SELECT now(), 'paper', id, 'e2e-logs-b', 'max_open_positions', 'blocked',
           'e2e: 3 positions open, the limit is 3', '{"settings": {"max_open_positions": 3}}'
    FROM bots WHERE name = 'e2e-bot';
  `);
  await signIn(page);
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Logs" }).click();
  await expect(page).toHaveURL(/\/logs/);

  // The engine logged its bots when it started (an info line), mirrored to Redis. Other
  // lines have come since, so ask for that event.
  await expect(page.locator("tr[data-log]").first()).toBeVisible();
  await page.getByLabel("Event starts with").fill("bots.loaded");
  await page.getByLabel("Event starts with").press("Enter");
  await expect(page).toHaveURL(/event=bots.loaded/);
  await expect(page.locator('tr[data-log="bots.loaded"]').first()).toBeVisible();
  await page.getByLabel("Show").selectOption("errors");
  await expect(page.locator('tr[data-log="bots.loaded"]')).toHaveCount(0);

  await page.getByRole("link", { name: "Risk events" }).click();
  const event = page.locator("tr[data-risk-event]", { hasText: "e2e-logs-b" });
  await expect(event).toContainText("e2e: 3 positions open, the limit is 3");
  await expect(event).toContainText("blocked");
});
