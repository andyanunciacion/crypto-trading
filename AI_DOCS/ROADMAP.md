# crypto-trading — Roadmap

> **Order and progress.** Scope details are in [PRD.md](PRD.md); technical design is in [ARCHITECTURE.md](ARCHITECTURE.md).
> Work is split into **vertical slices**: each slice goes end to end (data → logic → UI where there is one → tests) and works on its own.
> Tick the checkboxes as slices ship. Each phase has **exit criteria** instead of dates. Don't start a phase until the previous one meets them.
> Rough guide only: ~10–12 weeks part-time (10–15 h/week) to the first small live trade.

**Rules for every slice**
- Tests come with the slice; money code (risk, sizing, fees, P&L) is tested first.
- ruff, mypy and the tests pass before the slice is ticked.
- Any diff that touches orders, risk or keys is reviewed by hand.

## Phase 0 — Project setup

1. [x] **Repo:** `git init`, private GitHub repo, `.gitignore` (with `.env`) and folder layout (`backend/`, `engine/`, `shared/`, `frontend/`, `infra/`).
2. [x] **Compose stack:** Docker Compose with Postgres + TimescaleDB, Redis, API, engine and frontend. Each service starts and has a trivial health check.
3. [x] **Config:** typed settings with pydantic-settings; `.env.example` with no real secrets; `MODE=paper` by default.
4. [x] **Quality gates:** uv + pnpm set up; pre-commit with ruff, mypy, ESLint, Prettier, gitleaks; pytest runs one empty test.
5. [x] **CI:** GitHub Actions runs lint, type check and tests on every push.

**Exit criteria:** `docker compose up` starts every service; CI passes on an empty test; gitleaks blocks a commit containing a fake key.

## Phase 1 — Exchange connection and market data

1. [x] **ExchangeAdapter (mexc):** CCXT async wrapper for balances, candles, tickers, place/cancel order, with a sandbox flag. Tested against recorded responses. Answers the open questions on testnet and WebSocket support.
2. [x] **Candle store + backfill:** TimescaleDB hypertable, Alembic migration, downloader for 1m–1d with gap detection and filling.
3. [x] **Live prices:** stream (WebSocket, or REST polling if CCXT's `watch*` for `mexc` isn't reliable) → latest price in Redis + published on a channel; auto-reconnect with backoff.
4. [x] **Resilience:** rate limits respected; retries with backoff on network errors; structured logs for every reconnect/retry.

**Exit criteria:** a year of 1h candles for the first pair is stored with no gaps; live prices update in Redis for an hour without crashing.

## Phase 2 — Strategy engine and backtesting

1. [x] **Strategy interface + EMA crossover:** base class (params schema, warm-up, `on_candle` → signal), first strategy, tests on known price series.
2. [x] **Event-driven backtester:** replays candles through the same strategy code; models fees, slippage (higher for small-cap pairs) and minimum order sizes; stores `backtest_runs`.
3. [x] **Report:** total return, max drawdown, Sharpe, win rate, profit factor, number of trades, equity curve.
4. [x] **More strategies:** RSI mean reversion and Bollinger breakout.
5. [x] **Overfitting checks:** parameter sweeps and walk-forward testing.
6. [x] **Parity test:** the same strategy gives the same signals in a backtest and in a replay of recorded live data.

**Exit criteria:** the parity test passes; every report includes fees and slippage; walk-forward runs for at least one strategy.

## Phase 3 — Paper trading

1. [x] **Broker interface + PaperBroker:** fills at live bid/ask from Redis with fees and partial fills; orders, fills, positions and paper balances stored in Postgres; unique `client_order_id` on every order.
2. [x] **RiskManager stub in the path:** strategy → RiskManager (pass-through for now, logs every order) → Broker, so the wiring never changes later (D8).
3. [x] **Bot runner:** loads active bots and strategy configs from the DB; runs them on each closed candle.
   - [x] **Paper fills for crossing orders** (P3-3b, before the 24-hour run): an order's remainder fills at the best price, not its limit, and an unchanged quote doesn't fill twice (D34, D36).
4. [x] **Restart recovery:** state rebuilt from the DB on startup; no lost or duplicated positions (tested by killing the engine mid-run). After a gap, bots act only on the newest candle, never on the ones they missed.

**Exit criteria:** a fast (1m) paper bot runs for 24 hours on the PC with no crashes, and its trades check out against prices by hand. Running for days is proven on the server in Phase 8 (D37).

## Phase 4 — Risk management

1. [x] **Position sizing:** fixed amount, percent of equity, ATR-based.
2. [x] **Stops:** stop-loss, take-profit, trailing stops. (Rules and backtests in P4-2a; paper bots in P4-2b, whose paper run matched its backtests trade for trade.)
3. [x] **Limits:** max open positions, max exposure per coin, max daily loss, max drawdown (defaults in ARCHITECTURE §6), stored in `risk_settings`; every block recorded in `risk_events`. Account-wide; a buy over the coin cap is shrunk; sells never checked (D41).
4. [ ] **Circuit breakers:** stale data (> 2 candles), repeated errors (3 in 10 min), price spikes.
5. [ ] **Kill switch (engine side):** Redis command → cancel orders, stop all bots, optionally close positions.

**Exit criteria:** a test proves each limit blocks orders, including edge cases; the kill switch stops everything within seconds.

## Phase 5 — API and dashboard

1. [ ] **Auth:** single user, password + TOTP, JWT in an httpOnly cookie; Next.js login page.
2. [ ] **Shell + Overview + kill switch:** header with mode badge, freshness and kill switch (UX1, UX2, UX5); Overview with equity, today's P&L, positions and bot status over WebSocket.
3. [ ] **Market:** Lightweight Charts candles with indicators and trade markers; trade explanation panel (UX9).
4. [ ] **Bots:** create, start, pause, stop; edit strategy params via a form generated from the schema; commands go to the engine via Redis.
5. [ ] **Backtests:** run a backtest from the UI; view metrics, equity curve and trades.
6. [ ] **History + Logs:** trades and orders with filters and CSV export; logs of decisions, risk events and errors.
7. [ ] **Settings:** encrypted exchange keys, risk limits (raising one needs TOTP, UX4), alert preferences.

**Exit criteria:** you can start a paper bot, watch it trade live on the chart, and stop it from the UI; Playwright covers login, bot control and the kill switch.

## Phase 6 — Strategy switching

1. [ ] **Manual hot-swap:** change a bot's strategy or params without restarting the engine; per-bot open-position policy (keep / close first / hand over).
2. [ ] **Config history:** append-only `strategy_configs` with rollback from the UI.
3. [ ] **Regime detector:** trending up/down, ranging, high volatility from ADX, ATR, EMA slope; tested on labeled history.
4. [ ] **Auto-switcher:** regime → strategy mapping in settings, minimum hold time; switches logged, shown on the chart and in a switch log.
5. [ ] **Combined backtest:** auto-switching backtested end to end as one strategy.
6. [ ] **Grid and DCA strategies.**

**Exit criteria:** a backtest with auto-switching runs end to end, and a manual switch in paper mode keeps positions correct.

## Phase 7 — Alerts and monitoring

1. [ ] **Telegram alerts:** trades, stop-loss hits, switches, errors, limit breaches, daily summary; on/off per event type.
2. [ ] **Telegram commands:** /status, /pnl, /pause, /kill, only from your chat ID.
3. [ ] **Heartbeat:** alert if the engine stops sending updates.
4. [ ] **Health + errors:** health endpoint per service; Sentry wired into API and engine.

**Exit criteria:** stopping the engine container triggers an alert within 2 minutes, and /kill works from your phone.

## Phase 8 — Deployment

1. [ ] **Server:** Singapore VPS, Ubuntu 24.04; SSH keys only, firewall, automatic security updates.
2. [ ] **Access:** Caddy HTTPS; dashboard reachable only via Tailscale or IP limits.
3. [ ] **Backups:** nightly off-server Postgres backups, **restored once** to prove they work.
4. [ ] **Deploys:** one-command deploy script or GitHub Action; Docker restart policies.
5. [ ] **Soak:** paper bot running on the server.

**Exit criteria:** the paper bot runs on the server for 2 weeks with no manual fixes.

## Phase 9 — Live trading, small

> **Go-live gate (before slice 2).** Every box must be ticked:
> - [ ] Strategy backtested over at least 1–2 years, including fees and slippage
> - [ ] Walk-forward results close to in-sample results (not overfitted)
> - [ ] Paper trading for 2–4 weeks with results close to the backtest
> - [ ] Every risk rule covered by tests and checked by hand once
> - [ ] Kill switch tested from the dashboard and from Telegram
> - [ ] Restart test passed: positions and orders recovered correctly
> - [ ] API key is trade-only, IP-locked, with no withdrawal permission; separate sub-account
> - [ ] Dashboard behind two-factor login and Tailscale or IP limits
> - [ ] Nightly backups tested by restoring once
> - [ ] Alerts arrive on your phone for trades, errors and downtime
> - [ ] Starting capital is an amount you can afford to lose fully
> - [ ] Weekly review time set in your calendar

1. [ ] **LiveBroker + reconciliation:** live broker on the Broker interface; startup reconciliation with the exchange. MEXC has no testnet, so test in two steps: first the full order path on **Binance's spot testnet** (fake money, D23), then a tiny real order on MEXC.
2. [ ] **First live bot:** switch one bot from PaperBroker to LiveBroker with a small amount (UX3 flow).
3. [ ] **Weekly review:** compare live vs. paper vs. backtest every week; turn bots off when they stop matching.
4. [ ] **Scale-up rule:** scale only after 1–2 months of results close to expectations.

**Exit criteria:** this phase doesn't really end. Keep reviewing weekly.
