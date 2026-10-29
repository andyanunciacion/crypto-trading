import { randomBytes } from "node:crypto";

import { compose } from "./stack";
import { newSecret } from "./totp";

/**
 * Starts the e2e stack from scratch and creates its one user, `e2e`, with a random
 * password and TOTP secret, and one running paper bot, `e2e-bot`, for the market chart
 * and the kill switch (bots.spec.ts makes its own, from the dashboard). It backfills
 * 120 days of BTC/USDT 4h candles for the backtests, and starts the backtester.
 * Environment variables set here reach the tests.
 *
 * The engine connects to MEXC for prices and candles, so this needs the internet.
 */
export default function globalSetup(): void {
  compose(["down", "-v", "--remove-orphans"]); // whatever a failed run left behind
  // --wait: until the API and the dashboard report healthy (their dependencies start first).
  compose(["up", "-d", "--build", "--wait", "api", "frontend"]);
  // The bot before the engine: the engine loads bots when it starts. The backtester has
  // its own image of the engine's code; built here too, or a stale one runs the page's
  // backtests while the CLI runs the new code.
  compose(["build", "engine", "backtester"]);
  compose([
    "run",
    "--rm",
    "-T",
    "engine",
    "python",
    "-m",
    "engine.bots.create",
    "e2e-bot",
    "ema_crossover",
    "BTC/USDT",
    "1h",
    "--cash",
    "1000",
  ]);
  // For backtests.spec.ts: 4h candles, which no bot stores, so the range it runs over
  // can't move between the dashboard's run and the CLI's.
  compose([
    "run",
    "--rm",
    "-T",
    "engine",
    "python",
    "-m",
    "engine.marketdata.backfill",
    "BTC/USDT",
    "4h",
    "--days",
    "120",
  ]);
  compose(["up", "-d", "--wait", "engine", "backtester"]);

  const password = randomBytes(18).toString("base64url");
  const secret = newSecret();
  compose(
    [
      "exec",
      "-T",
      "api",
      "python",
      "-m",
      "backend.users",
      "create",
      "e2e",
      "--password-stdin",
      "--totp-secret",
      secret,
    ],
    `${password}\n`,
  );
  process.env.E2E_PASSWORD = password;
  process.env.E2E_TOTP_SECRET = secret;
}
