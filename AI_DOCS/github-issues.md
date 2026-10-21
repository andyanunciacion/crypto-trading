# GitHub issues — reference list

> A one-time reference for creating issues. **Once they exist, GitHub is the source of truth.** This file can be deleted, or kept as templates.
> IDs like `P0-T1` are only for cross-referencing dependencies here. GitHub assigns the real numbers.
> Order follows [ROADMAP.md](ROADMAP.md): one issue per slice. Branch naming: `<issue#>-<short-slug>`. Commits: `#<issue#> <title>`.

**Suggested labels:** `infra` · `engine` · `strategy` · `risk` · `backend` · `frontend` · `security` · `ops` · `money` (touches orders, risk, fees or keys — review by hand) · `blocked` (waiting on a gate or open question)
**Suggested milestones:** `Phase 0 — Setup` · `Phase 1 — Market data` · `Phase 2 — Backtesting` · `Phase 3 — Paper trading` · `Phase 4 — Risk` · `Phase 5 — Dashboard` · `Phase 6 — Switching` · `Phase 7 — Alerts` · `Phase 8 — Deployment` · `Phase 9 — Live`

---

## Phase 0 — Project setup

### P0-T1 · Repo and folder layout
`infra` · Depends on: —
**Goal:** a versioned monorepo with the agreed layout.
- [x] `git init`; private GitHub repo; push
- [x] `.gitignore` covering `.env`, Python, Node and Docker artifacts
- [x] Folders `backend/`, `engine/`, `shared/`, `frontend/`, `infra/` with placeholder READMEs

**Done when:** a fresh clone has the layout and `git check-ignore .env` confirms `.env` is ignored.

### P0-T2 · Docker Compose stack
`infra` · Depends on: P0-T1
**Goal:** every service starts with one command.
- [x] Postgres 16 + TimescaleDB, Redis
- [x] Minimal FastAPI app (`/health`), minimal engine loop (logs a heartbeat), Next.js hello page
- [x] Health checks and named volumes

**Done when:** `docker compose up` starts all five services and each health check is green.

### P0-T3 · Typed config
`infra` `security` · Depends on: P0-T2
**Goal:** one typed config used by API and engine.
- [x] `shared/config.py` with pydantic-settings
- [x] `.env.example` with every key and no real values; `MODE=paper` default
- [x] Startup fails with a clear error if required settings are missing

**Done when:** API and engine read config from `shared/`, and starting without a required var gives a readable error.

### P0-T4 · Quality gates
`infra` · Depends on: P0-T1
**Goal:** consistent code from the first commit.
- [ ] uv project(s) for Python; pnpm for frontend
- [ ] ruff, mypy (strict), ESLint, Prettier configured
- [ ] pre-commit with ruff, mypy, gitleaks
- [ ] pytest with one passing test per Python package

**Done when:** `pre-commit run --all-files` passes, and committing a fake API key is blocked by gitleaks.

### P0-T5 · CI
`infra` · Depends on: P0-T4
**Goal:** checks run on every push.
- [ ] GitHub Actions: ruff, mypy, pytest, ESLint, frontend type check

**Done when:** a push runs the workflow green, and a deliberate lint error turns it red.

---

## Phase 1 — Exchange connection and market data

### P1-1 · ExchangeAdapter for mexc
`engine` `money` · Depends on: P0-T3
**Goal:** the only gateway to the exchange.
- [ ] Async CCXT wrapper: balances, candles, tickers, place/cancel order; sandbox flag
- [ ] Decimal conversion at the adapter boundary
- [ ] Tests with recorded responses (no network in tests)
- [ ] Find out: spot testnet? `watch*` WebSocket support? API key rules (IP lock, trade-only, expiry)? Record answers in PRD §9

**Done when:** tests pass offline, nothing outside the adapter imports `ccxt`, and the three open questions are ticked in the PRD.

### P1-2 · Candle store and backfill
`engine` · Depends on: P1-1
**Goal:** complete price history in the database.
- [ ] `candles` hypertable via Alembic
- [ ] Downloader for 1m, 5m, 15m, 1h, 4h, 1d; idempotent upserts
- [ ] Gap detection and filling

**Done when:** a year of 1h candles for the first pair is stored, and a gap-check query returns no gaps.

### P1-3 · Live price stream
`engine` · Depends on: P1-1
**Goal:** current prices always available in Redis.
- [ ] WebSocket stream, or REST polling fallback
- [ ] Latest price per symbol in Redis + published on a channel
- [ ] Auto-reconnect with exponential backoff

**Done when:** prices update in Redis for one hour without a crash, including after pulling the network for 30 seconds.

### P1-4 · Rate limits and retries
`engine` · Depends on: P1-1
**Goal:** the engine is polite to the exchange and resilient to network errors.
- [ ] `enableRateLimit`; retry with backoff on network errors only (never on rejected orders)
- [ ] structlog entries for every retry and reconnect

**Done when:** tests show network errors retry and rejected orders don't.

---

## Phase 2 — Strategy engine and backtesting

### P2-1 · Strategy interface + EMA crossover
`strategy` · Depends on: P1-2
**Goal:** a plug-in contract every strategy follows.
- [ ] Base class: name, version, pydantic params, `warmup_candles`, `on_candle` → signal
- [ ] EMA crossover strategy
- [ ] Tests on hand-built price series with known crossovers

**Done when:** EMA crossover emits BUY/SELL exactly at the expected candles in tests.

### P2-2 · Event-driven backtester
`strategy` `money` · Depends on: P2-1
**Goal:** honest backtests using live strategy code.
- [ ] Candle replay through the strategy
- [ ] Fees, slippage (configurable, higher default for small-cap pairs), minimum order sizes
- [ ] `backtest_runs` table storing inputs and results

**Done when:** a backtest's results can be reproduced from its stored inputs, and fees match a hand calculation in a test.

### P2-3 · Backtest report
`strategy` · Depends on: P2-2
**Goal:** the metrics needed to judge a strategy.
- [ ] Total return, max drawdown, Sharpe, win rate, profit factor, trade count, equity curve

**Done when:** each metric matches a hand-computed value on a small fixture.

### P2-4 · RSI mean reversion and Bollinger breakout
`strategy` · Depends on: P2-1
**Goal:** strategies for ranging and breakout markets.
- [ ] Both strategies with typed params and tests

**Done when:** both pass tests on known series and run through the backtester.

### P2-5 · Parameter sweeps and walk-forward
`strategy` · Depends on: P2-3
**Goal:** catch overfitting before paper trading.
- [ ] Grid sweep over params
- [ ] Walk-forward: optimize on window N, test on N+1, roll forward

**Done when:** a walk-forward report for one strategy shows in-sample vs. out-of-sample metrics side by side.

### P2-6 · Backtest/live parity test
`strategy` · Depends on: P2-2, P1-3
**Goal:** prove backtest signals equal live signals.
- [ ] Record a live candle stream; replay it through the live path and the backtester

**Done when:** both paths produce identical signal lists in CI.

---

## Phase 3 — Paper trading

### P3-1 · Broker interface + PaperBroker
`engine` `money` · Depends on: P2-2
**Goal:** simulated trading with realistic fills.
- [ ] `Broker` interface; `PaperBroker` filling at live bid/ask with fees and partial fills
- [ ] `orders`, `fills`, `positions`, `paper_balances` tables
- [ ] Unique `client_order_id` on every order

**Done when:** tests show fills, fees and balances are correct, and a retried order never creates a duplicate.

### P3-2 · RiskManager stub in the order path
`risk` `money` · Depends on: P3-1
**Goal:** every order passes the RiskManager from day one.
- [ ] Pass-through RiskManager that logs every order with inputs
- [ ] Brokers can only be reached through it

**Done when:** a test shows that no code path reaches a Broker without the RiskManager.

### P3-3 · Bot runner
`engine` · Depends on: P3-2
**Goal:** active bots run automatically.
- [ ] `bots` and `strategy_configs` tables
- [ ] Load active bots; run on each closed candle

**Done when:** a seeded paper bot trades on new candles without manual steps.

### P3-4 · Restart recovery
`engine` `money` · Depends on: P3-3
**Goal:** no lost or duplicated positions after a restart.
- [ ] Rebuild state from DB on startup
- [ ] Test: kill the engine mid-run, restart, compare state

**Done when:** the kill/restart test passes, and a 72-hour paper run completes with trades that check out by hand.

---

## Phase 4 — Risk management

### P4-1 · Position sizing
`risk` `money` · Depends on: P3-2
- [ ] Fixed amount, percent of equity, ATR-based; respect minimum order size

**Done when:** tests cover each mode, including rounding and minimum-size edge cases.

### P4-2 · Stops
`risk` `money` · Depends on: P4-1
- [ ] Stop-loss, take-profit, trailing stop

**Done when:** tests trigger each stop on known price paths.

### P4-3 · Limits
`risk` `money` · Depends on: P4-1
- [ ] Max open positions, max exposure per coin, max daily loss (resets at Asia/Manila midnight), max drawdown
- [ ] `risk_settings` and `risk_events` tables

**Done when:** a test per limit shows it blocks orders, and every block is written to `risk_events`.

### P4-4 · Circuit breakers
`risk` · Depends on: P4-3
- [ ] Stale data (> 2 candles), 3 failed orders in 10 min, price spike

**Done when:** tests show each breaker pauses trading and logs why.

### P4-5 · Kill switch (engine)
`risk` `money` · Depends on: P4-3
- [ ] Redis command channel; cancel orders, stop bots, optional close positions
- [ ] Works with the API container stopped

**Done when:** an integration test sends the command and every bot is stopped within seconds.

---

## Phase 5 — API and dashboard

### P5-1 · Auth
`backend` `frontend` `security` · Depends on: P0-T2
- [ ] `users` table; argon2 password; TOTP enrolment and check
- [ ] JWT in httpOnly secure cookie; auth on every REST/WS route
- [ ] Login page

**Done when:** a Playwright test logs in with password + TOTP, and API tests show every route rejects unauthenticated calls.

### P5-2 · App shell, Overview, kill switch
`frontend` `backend` · Depends on: P5-1, P4-5
- [ ] WebSocket feed for equity, P&L, positions, bot status, heartbeat
- [ ] Header with mode badge, freshness, kill switch (UX1, UX2, UX5)

**Done when:** a Playwright test triggers the kill switch from the UI and the engine stops all bots.

### P5-3 · Market chart
`frontend` · Depends on: P5-2
- [ ] Lightweight Charts candles, indicators, trade markers
- [ ] Trade explanation panel (UX9)

**Done when:** live candles update on screen and clicking a trade marker shows its signal and risk decision.

### P5-4 · Bots page
`frontend` `backend` · Depends on: P5-2, P3-3
- [ ] Create, start, pause, stop via engine commands
- [ ] Params form generated from the strategy schema

**Done when:** a Playwright test creates and starts a paper bot, then stops it.

### P5-5 · Backtests page
`frontend` `backend` · Depends on: P5-1, P2-3
- [ ] Run form; results with metrics, equity curve and trades

**Done when:** a backtest run from the UI shows the same metrics as the CLI run with the same inputs.

### P5-6 · History and Logs
`frontend` `backend` · Depends on: P5-2
- [ ] Trades/orders with filters and CSV export; logs page

**Done when:** CSV export matches the DB rows for a filtered range.

### P5-7 · Settings
`frontend` `backend` `security` `money` · Depends on: P5-1
- [ ] Exchange keys encrypted with Fernet (`exchange_accounts`); never sent back to the browser
- [ ] Risk limits editor (raising needs TOTP, UX4); alert preferences

**Done when:** keys are stored encrypted (checked in the DB), and raising a limit without TOTP is rejected by the API.

---

## Phase 6 — Strategy switching

### P6-1 · Manual hot-swap
`engine` `money` · Depends on: P5-4
- [ ] Change strategy/params live; open-position policy keep / close first / hand over

**Done when:** a paper-mode test switches strategy under each policy and positions stay correct.

### P6-2 · Config history and rollback
`engine` `frontend` · Depends on: P6-1
- [ ] Append-only `strategy_configs`; rollback from bot detail

**Done when:** rolling back restores the previous params, and both versions stay in history.

### P6-3 · Regime detector
`strategy` · Depends on: P2-1
- [ ] ADX, ATR, EMA slope → trending up/down, ranging, high volatility

**Done when:** tests on labeled historical windows give the expected regimes.

### P6-4 · Auto-switcher
`engine` `frontend` · Depends on: P6-1, P6-3
- [ ] Regime → strategy mapping; minimum hold time
- [ ] `strategy_switches` table; switch markers on chart and switch log

**Done when:** a replay shows switches only after the minimum hold, each logged with a reason.

### P6-5 · Combined auto-switching backtest
`strategy` · Depends on: P6-4
- [ ] Backtester supports the switcher as one combined strategy

**Done when:** an auto-switching backtest runs end to end and produces a full report.

### P6-6 · Grid and DCA strategies
`strategy` · Depends on: P2-2
- [ ] Grid (range, number of grids); DCA (amount, interval, dip trigger)

**Done when:** both pass tests on known series and run in the backtester.

---

## Phase 7 — Alerts and monitoring

### P7-1 · Telegram alerts
`ops` · Depends on: P3-3
- [ ] Alerts bot service consuming engine events; per-event toggles (`alert_settings`); daily summary

**Done when:** a paper trade, a risk block and an error each produce a Telegram message.

### P7-2 · Telegram commands
`ops` `security` · Depends on: P7-1, P4-5
- [ ] /status, /pnl, /pause, /kill; ignore all other chat IDs

**Done when:** /kill stops all bots, and a message from another chat ID is ignored and logged.

### P7-3 · Heartbeat alert
`ops` · Depends on: P7-1
- [ ] Engine heartbeat; alert when missing

**Done when:** stopping the engine container triggers an alert within 2 minutes.

### P7-4 · Health and Sentry
`ops` · Depends on: P0-T2
- [ ] `/health` per service; Sentry in API and engine

**Done when:** a deliberate exception shows up in Sentry with a stack trace.

---

## Phase 8 — Deployment

### P8-1 · Server hardening
`infra` `security` · Depends on: P7-3
- [ ] Singapore VPS, Ubuntu 24.04; SSH keys only; firewall; unattended upgrades

**Done when:** password SSH login is refused, and only the required ports are open.

### P8-2 · HTTPS and private access
`infra` `security` · Depends on: P8-1
- [ ] Caddy; Tailscale or IP-limited access to the dashboard

**Done when:** the dashboard loads over HTTPS from an allowed device and is unreachable from elsewhere.

### P8-3 · Backups
`infra` `ops` · Depends on: P8-1
- [ ] Nightly `pg_dump` to off-server storage; restore procedure written down

**Done when:** a backup has been restored into a fresh database once and the data checked.

### P8-4 · Deploys and restart policies
`infra` · Depends on: P8-1
- [ ] One-command deploy (script or GitHub Action); `restart: unless-stopped`

**Done when:** a deploy updates the server in one command, and a reboot brings every service back.

### P8-5 · Two-week server soak
`ops` · Depends on: P8-2, P8-3, P8-4
- [ ] Paper bot running on the server; log any manual fix

**Done when:** 2 weeks pass with no manual fixes.

---

## Phase 9 — Live trading, small

### P9-1 · LiveBroker and reconciliation
`engine` `money` `security` · Depends on: P8-5
- [ ] `LiveBroker` on the Broker interface
- [ ] Startup reconciliation with the exchange before trading
- [ ] Tiny real order test (or testnet)

**Done when:** a tiny real order is placed and cancelled through the full path, and reconciliation fixes a seeded mismatch in a test.

### P9-2 · Go-live gate and first live bot
`money` `blocked` · Depends on: P9-1
- [ ] Every box in the ROADMAP Phase 9 go-live gate ticked
- [ ] Switch one bot to live via the UX3 flow, with a small amount

**Done when:** the go-live gate is fully ticked and one live bot has made its first trade.

### P9-3 · Weekly review routine
`ops` · Depends on: P9-2
- [ ] Weekly live vs. paper vs. backtest comparison (report or dashboard view)

**Done when:** the first weekly comparison has been produced and reviewed.
