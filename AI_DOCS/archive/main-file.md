# Crypto Auto-Trading Web App — Development Plan

Oct 19, 2026

## Overview

You'll build a self-hosted web app that watches crypto markets, runs pluggable trading strategies, and places orders automatically. You switch strategies from a dashboard or let rules switch them. The plan runs in 10 phases over about 10–12 weeks part-time. Real money is used only in the last phase.

**What the finished app does**

- Streams live prices from one or more exchanges and stores price history
- Runs strategies (trend-following, mean reversion, grid, DCA) as swappable modules
- Backtests any strategy on historical data before it touches money
- Paper trades with live prices and fake balances
- Enforces risk limits: position size, stop-loss, max daily loss, kill switch
- Shows a dashboard with live P&L, positions, trades, charts, and logs
- Switches strategies manually or automatically by market regime
- Sends alerts to Telegram on trades, errors, and limit breaches

**Guiding principles**

1. Safety before features. Every order passes through the risk layer, no exceptions.
2. Backtest, then paper trade, then go live small. Never skip a step.
3. The trading engine runs separately from the web UI. If the UI crashes, trading and risk checks keep running.
4. Build one exchange and one strategy end to end first, then widen.

**Build vs. extend.** This plan builds a custom app on CCXT, so you control every part. A faster alternative is to extend [Freqtrade](https://www.freqtrade.io), an open-source bot that already has backtesting, a web UI, and Telegram control. Choose custom if you want to learn and own the design. Choose Freqtrade if you mainly want a working bot quickly.

## Tech stack

Use Python for the backend and trading engine, where the trading libraries are. Use TypeScript and Next.js for the dashboard. Docker Compose ties everything together, both on your PC and later on a server.

| Layer | Technology | Why |
| --- | --- | --- |
| Language (backend) | Python 3.12+ | Best ecosystem for trading, data, and indicators |
| Exchange access | CCXT (with async support) | One API for 100+ exchanges; REST and WebSocket |
| Backend API | FastAPI + Uvicorn | Fast, async, auto-generated API docs, built-in WebSockets |
| Trading engine | Separate Python asyncio worker | Keeps trading alive if the API or UI restarts |
| Indicators | pandas, numpy, pandas-ta or TA-Lib | RSI, EMA, MACD, Bollinger Bands, ATR |
| Backtesting | vectorbt or backtesting.py, plus a custom event-driven backtester | Fast parameter sweeps, plus realistic fills and fees |
| Database | PostgreSQL 16 + TimescaleDB extension | Trades, orders, and settings, plus fast time-series candles |
| ORM and migrations | SQLAlchemy 2.0 + Alembic | Typed models, versioned schema changes |
| Cache and messaging | Redis | Live prices, pub/sub between engine and API, locks |
| Frontend | Next.js (React, TypeScript) | Dashboard pages, routing, good Claude Code support |
| UI kit | Tailwind CSS + shadcn/ui | Clean components without heavy design work |
| Charts | TradingView Lightweight Charts, Recharts | Candlestick charts; P&L and equity curves |
| Frontend data | TanStack Query + native WebSocket | Caching REST calls, live updates |
| Auth | JWT + TOTP two-factor (pyotp) | The app controls money, so 2FA is required |
| Secrets | .env files + encrypted API keys (cryptography / Fernet) | Exchange keys never stored in plain text |
| Alerts | Telegram Bot API (python-telegram-bot) | Instant phone alerts and remote kill switch |
| Testing | pytest, pytest-asyncio; Playwright for UI | Engine logic must be tested before money moves |
| Code quality | ruff, mypy, ESLint, Prettier, pre-commit | Catches bugs early; keeps Claude Code output consistent |
| Logging and errors | structlog (JSON logs), Sentry (free tier) | Know exactly why a trade happened or failed |
| Containers | Docker + Docker Compose | Same setup on your PC and the server |
| Hosting (later) | A VPS in Singapore (DigitalOcean, Vultr, AWS Lightsail, or Hetzner) | Low latency from Manila and to exchange servers; runs 24/7 |
| CI | GitHub Actions | Runs tests on every push |

On your PC, install Git, Docker Desktop (with WSL 2 on Windows), Python 3.12+, Node.js 20+ LTS, and VS Code. You also need Claude Code.

## Architecture

The app is five services in Docker Compose: dashboard, API, alerts bot, trading engine, and storage (PostgreSQL and Redis). The engine is the only part that talks to the exchange.

```text
Every order passes the risk manager before it reaches the exchange

+-----------------------------+          +-----------------------------+
| Dashboard (Next.js)         |          | Your phone (Telegram)       |
| Charts, bots, P&L, kill sw. |          | Alerts and remote commands  |
+-----------------------------+          +-----------------------------+
          ^  | HTTPS + WebSocket                ^  | alerts, /status, /kill
          |  v                                  |  v
+-----------------------------+          +-----------------------------+
| API (FastAPI)               |          | Alerts bot                  |
| REST + WebSocket, 2FA login |          | Trade, error, downtime      |
+-----------------------------+          +-----------------------------+
          ^  | history, settings                ^ events
          |  v                                  |
+-----------------------------+          +-----------------------------+
| PostgreSQL + TimescaleDB    |          | Redis                       |
| Candles, orders, trades     |          | Live prices, events, cmds   |
+-----------------------------+          +-----------------------------+
          ^ orders, trades, P&L                 ^  | prices, commands
          |                                     |  v
+--------------------------------------------------------------------+
| Trading engine (separate worker) — keeps running if the UI stops   |
|                                                                    |
|  [Market data] -> [Strategies] -> [RISK MANAGER] -> [Broker]       |
|   live+history    signals only    checks every     paper or live   |
|        ^                ^          order               |           |
|        |                | switches strategy            |           |
|        |         [Regime switcher]                     |           |
|        |          picks the strategy                   |           |
+--------|-----------------------------------------------|-----------+
         | prices                                 orders |
         |                                               v
+--------------------------------------------------------------------+
| Exchange API (Coins.ph, via CCXT)                                  |
| Trade-only key, IP-locked, no withdrawals                          |
+--------------------------------------------------------------------+
```

Prices flow up from the exchange through market data to the strategies. Signals then pass the risk manager before the broker sends any order. The dashboard never trades directly; it sends commands to the engine through Redis.

## Phase-by-phase plan

Each phase ends with something that works and has tests. Don't start a phase until the previous one meets its "done when" check. The prompts are starting points to paste into Claude Code. Use plan mode first, review the plan, then let it build.

### Phase 0 — Project setup (week 1)

**Goal:** a clean monorepo that runs with one command.

**Tech:** Git, GitHub, Docker Compose, Poetry or uv (Python), pnpm (Node), pre-commit, GitHub Actions.

**Features**

- Folders: `backend/` (API), `engine/` (trading worker), `shared/` (models, config), `frontend/`, `infra/`
- Docker Compose with Postgres + TimescaleDB, Redis, API, engine, and frontend
- Typed config with pydantic-settings; `.env.example` with no real secrets
- Linting, type checks, and a test run in CI
- A `CLAUDE.md` file describing the project rules (see "Working with Claude Code")

**Claude Code prompt:** "Set up a monorepo for a crypto trading app: a FastAPI backend, a separate asyncio trading engine, shared SQLAlchemy models, and a Next.js frontend. Add Docker Compose with TimescaleDB and Redis, pre-commit with ruff and mypy, and a GitHub Actions workflow that runs the tests."

**Done when:** `docker compose up` starts every service, and CI passes on an empty test.

### Phase 1 — Exchange connection and market data (weeks 1–2)

**Goal:** reliable live and historical prices in your database.

**Tech:** CCXT (async), WebSockets, TimescaleDB hypertables, Redis.

**Features**

- An `ExchangeAdapter` wrapper around CCXT so the rest of the app never calls CCXT directly
- Testnet or sandbox mode switch per exchange
- Historical candle downloader (1m, 5m, 15m, 1h, 4h, 1d) with gap filling
- Live ticker and candle stream over WebSocket, with auto-reconnect and backoff
- Latest prices cached in Redis and published on a channel
- Respect for rate limits; retries on network errors

**Claude Code prompt:** "Build an ExchangeAdapter class using ccxt async with methods for fetching balances, candles, and tickers and for placing and cancelling orders. Add a sandbox flag. Then write a candle downloader that backfills a TimescaleDB hypertable, and a live stream service that reconnects automatically and publishes prices to Redis."

**Done when:** a year of BTC/USDT 1h candles is stored with no gaps, and live prices update in Redis for an hour without crashing.

### Phase 2 — Strategy engine and backtesting (weeks 2–4)

**Goal:** test strategies on history and get honest results.

**Tech:** pandas, pandas-ta, vectorbt or backtesting.py, pytest.

**Features**

- A `Strategy` base class: settings, `on_candle()` returning a signal, warm-up period (see "Strategy system")
- Three or four starter strategies
- An event-driven backtester that uses the same strategy code as live trading
- Realistic fees, slippage, and minimum order sizes
- Reports: total return, max drawdown, Sharpe ratio, win rate, profit factor, number of trades, equity curve
- Parameter sweeps and walk-forward testing to avoid overfitting

**Claude Code prompt:** "Create a Strategy base class with on_candle returning BUY, SELL, or HOLD and typed parameters. Implement EMA crossover and RSI mean-reversion strategies. Build an event-driven backtester that reuses these classes, models 0.1% fees and slippage, and outputs a metrics report and an equity curve. Add tests with known price series."

**Done when:** the same strategy produces the same signals in backtests and in a replay of live data, and reports include fees.

### Phase 3 — Paper trading (weeks 4–5)

**Goal:** run strategies on live prices with fake money.

**Tech:** asyncio worker, Postgres, Redis.

**Features**

- A `PaperBroker` with the same interface as the live broker
- Simulated fills at live bid/ask, with fees and partial fills
- Virtual balances, open positions, and trade history saved to the database
- A bot runner that loads active strategy configs and runs them on each new candle
- State recovery after a restart: no lost or duplicated positions

**Claude Code prompt:** "Add a Broker interface with PaperBroker and LiveBroker implementations. The paper broker fills market orders at the live bid or ask from Redis, charges fees, and stores orders, fills, and positions in Postgres. Build a bot runner that runs the active strategies on each closed candle and recovers state on restart."

**Done when:** a paper bot runs for 72 hours with no crashes, and its trades can be checked against prices by hand.

### Phase 4 — Risk management (week 5)

**Goal:** no order goes out without passing risk checks.

**Tech:** pure Python module with heavy unit tests.

**Features**

- A `RiskManager` that every order must pass through
- Position sizing: fixed amount, percent of equity, or ATR-based
- Stop-loss, take-profit, and trailing stops
- Limits: max open positions, max exposure per coin, max daily loss, max drawdown
- Circuit breakers: pause on repeated errors, stale data, or price spikes
- Global kill switch: cancel orders, optionally close positions, stop all bots

**Claude Code prompt:** "Build a RiskManager that sits between strategies and the Broker. It enforces position sizing modes, stop-loss and take-profit, max open positions, max daily loss, and max drawdown. It blocks orders when data is older than 2 candles, and it has a kill switch. Write tests for every rule, including edge cases."

**Done when:** tests prove each limit blocks orders, and the kill switch stops everything within seconds.

### Phase 5 — API and dashboard (weeks 6–7)

**Goal:** see and control everything from the browser.

**Tech:** FastAPI, WebSockets, Next.js, Tailwind, shadcn/ui, TradingView Lightweight Charts, TanStack Query.

**Features**

- Login with password and two-factor code
- Overview page: total equity, today's P&L, open positions, bot status
- Market page: live candlestick chart with trade markers and indicators
- Bots page: create, start, pause, and stop bots; edit strategy settings
- Backtest page: run a backtest from the UI and view results
- Trades and orders history with filters and CSV export
- Logs page and a large, always-visible kill switch button
- Settings: exchange API keys (encrypted), risk limits, alert preferences

**Claude Code prompt:** "Add REST endpoints and a WebSocket feed to the FastAPI backend for bots, positions, trades, backtests, and settings. Then build a Next.js dashboard with shadcn/ui pages for Overview, Market (Lightweight Charts with trade markers), Bots, Backtests, History, Logs, and Settings. Add JWT login with TOTP two-factor authentication."

**Done when:** you can start a paper bot, watch it trade live on the chart, and stop it from the UI.

### Phase 6 — Strategy switching (week 8)

**Goal:** change strategies manually or automatically, safely.

**Tech:** regime detection with ADX, ATR, and moving averages; a rules engine; database-stored configs.

**Features**

- Manual hot-swap: change a bot's strategy or settings without restarting the engine
- Switching modes for open positions: keep, close first, or hand over to the new strategy
- A market regime detector (trending up, trending down, ranging, high volatility)
- Rule-based auto-switching, for example "ranging → grid; trending → EMA crossover"
- Cooldown so it doesn't flip strategies every few candles
- Version history of every strategy config, with rollback

**Claude Code prompt:** "Add a RegimeDetector that labels the market as trending up, trending down, ranging, or volatile using ADX, ATR, and EMA slope. Add a StrategySwitcher that maps regimes to strategies with a minimum hold time, and handles open positions according to a per-bot setting. Log every switch and show switches on the dashboard."

**Done when:** a backtest with auto-switching runs end to end, and a manual switch in paper mode keeps positions correct.

### Phase 7 — Alerts and monitoring (week 9)

**Goal:** know when anything happens, even away from your PC.

**Tech:** python-telegram-bot, structlog, Sentry, optional Prometheus + Grafana.

**Features**

- Telegram alerts: trades, stop-loss hits, strategy switches, errors, daily summary
- Telegram commands: /status, /pnl, /pause, /kill (limited to your chat ID)
- Heartbeat check: alert if the engine stops sending updates
- Health endpoint and an error-tracking dashboard

**Done when:** stopping the engine container triggers an alert within 2 minutes, and /kill works from your phone.

### Phase 8 — Deployment (week 10)

**Goal:** run 24/7 on a server, not your PC.

**Tech:** VPS (Ubuntu 24.04 LTS), Docker Compose, Caddy (automatic HTTPS), Tailscale or a firewall, automated backups.

**Features**

- Server hardening: SSH keys only, firewall, automatic security updates
- HTTPS with a domain; the dashboard reachable only via Tailscale or with IP limits
- Database backups every night, stored off the server
- A deploy script or GitHub Action to update with one command
- Docker restart policies so services come back after a reboot

**Done when:** the paper bot runs on the server for 2 weeks with no manual fixes.

### Phase 9 — Live trading, small (weeks 11–12 and on)

**Goal:** trade real money carefully.

**Features**

- Switch one bot from PaperBroker to LiveBroker
- Start with a small amount you can afford to lose completely
- Compare live results to paper and backtest results every week
- Scale up only after at least 1–2 months of results close to expectations

**Done when:** this phase never really ends. Keep reviewing, and turn bots off when they stop matching their backtests.

## Strategy system

Every strategy is a plug-in with the same interface. The backtester, paper broker, and live broker all run the same strategy code unchanged. That is what makes switching safe.

**The interface (in plain terms)**

- `name`, `version`, and a typed `params` schema (validated with pydantic, rendered as a form in the dashboard)
- `warmup_candles`: how much history it needs before giving signals
- `on_candle(market_data, position)` → a signal: BUY, SELL, or HOLD, with an optional size hint and stop level
- Optional `on_fill()` and `on_start()` / `on_stop()` hooks
- No direct calls to the exchange. Strategies only return signals; the risk layer and broker do the rest.

**Starter strategies**

| Strategy | Works best in | Core idea | Key settings |
| --- | --- | --- | --- |
| EMA crossover | Trending markets | Buy when fast EMA crosses above slow EMA; sell on cross below | Fast period, slow period, timeframe |
| RSI mean reversion | Ranging markets | Buy when RSI is oversold, sell when it recovers | RSI period, oversold and overbought levels |
| Bollinger breakout | Volatility expansion | Enter when price closes outside the band with rising volume | Band period, width, volume filter |
| Grid trading | Sideways markets | Place buy and sell orders at fixed steps inside a price range | Upper and lower price, number of grids |
| DCA | Long-term accumulation | Buy a fixed amount on a schedule or on dips | Amount, interval, dip trigger |

**Switching rules**

1. A regime detector labels each candle: trending up, trending down, ranging, or high volatility. For example, ADX above 25 means trending; ADX below 20 with narrow Bollinger Bands means ranging.
2. A mapping table in settings links each regime to a strategy, or to "stay flat."
3. A regime must hold for a minimum number of candles before a switch, to avoid flip-flopping.
4. Each bot has an open-position policy for switches: keep the position with its stop, close first, or let the new strategy manage it.
5. Every switch is logged with the reason, shown on the chart, and sent to Telegram.
6. Auto-switching is itself backtested as one combined strategy before it is turned on.

## Risk management and security

The main risks are bugs and leaked API keys, not market moves alone. These rules go into `CLAUDE.md` so Claude Code follows them in every session.

**Default risk limits (change to suit you)**

| Limit | Starting value |
| --- | --- |
| Risk per trade | 1% of account equity |
| Max open positions | 3 |
| Max exposure per coin | 25% of equity |
| Max daily loss | 3% of equity, then pause all bots until the next day |
| Max drawdown | 10% from peak, then stop and alert |
| Stale data cutoff | No orders if the last price is older than 2 candles |
| Error breaker | Pause a bot after 3 failed orders in 10 minutes |

**Security rules**

- Create exchange API keys with trading only. Never turn on withdrawal permission.
- Use the exchange's IP whitelist and lock keys to your server's IP.
- Encrypt keys at rest. The encryption key lives only in the server's environment, never in Git.
- Add `.env` to `.gitignore` from day one. Run a secret scanner (gitleaks) in pre-commit.
- Require two-factor login for the dashboard, and don't expose it to the open internet without Tailscale or IP limits.
- Limit Telegram commands to your own chat ID.
- Keep a separate exchange sub-account for the bot, holding only the money it is allowed to trade.

**Engineering safeguards**

- Every order gets a unique client order ID so retries never place duplicate orders.
- On startup, the engine checks its records against the exchange and fixes any mismatch before trading.
- Run the live broker against the exchange testnet in CI-style tests before real use.
- Log every decision with its inputs, so you can always answer "why did it buy?"

I'm not a financial advisor. Treat every strategy as an experiment, and only trade money you can afford to lose.

## Exchange choice in the Philippines

Start with Coins.ph (Coins Pro). It is licensed by the BSP, has a trading API, and is already supported by CCXT as `coinsph`. Most big global exchanges are currently blocked or flagged for Philippine users.

| Exchange | Status for PH users | API for bots | Notes |
| --- | --- | --- | --- |
| [Coins.ph / Coins Pro](https://docs.ccxt.com/docs/exchanges/coinsph) | BSP-licensed VASP | Yes, REST API, in CCXT as `coinsph` | Spot fees reported from about 0.10%; PHP pairs; [API docs](https://coins-docs.github.io) |
| [PDAX](https://pdax.ph/) | BSP-licensed VASP | Check with PDAX | Powers GCash's GCrypto; fees reported around 0.40–0.50% |
| Binance | Blocked since 2024 | — | Pursuing a return through an SEC sandbox partner, starting in the second half of 2026 ([Cointelegraph](https://cointelegraph.com/news/binance-philippines-return-sec-sandbox-blockshoals)) |
| OKX, Bybit, KuCoin, Kraken, MEXC, Bitget and others | Named in an SEC advisory, August 2025 | — | Being restricted in phases ([IDN Financials](https://www.idnfinancials.com/news/56446/philippines-blocks-10-crypto-exchanges-bybit-and-bitget-blacklisted)) |
| Coinbase, Gemini | ISP-blocked since December 2025 | — | ([Live Bitcoin News via MEXC](https://www.mexc.com/news/341634)) |

**What this means for the build**

- Build the `ExchangeAdapter` for `coinsph` first. Keep it generic so you can add Binance or others later if they become legal for you.
- If Coins Pro has no testnet, rely on your own paper-trading mode before going live.
- PHP-quoted pairs can have thinner order books than USDT pairs on global exchanges. Model higher slippage in backtests, and favor limit orders.
- Avoid using a VPN to reach blocked exchanges. It can break the exchange's terms, lead to frozen accounts, and leave you without local legal protection.
- Rules are changing quickly. Check the [BSP list of licensed VASPs](https://www.bsp.gov.ph) and SEC advisories before you pick or switch exchanges.

Fees and status above are from news and review sites as of October 2026. Confirm them on each exchange's own site.

## Working with Claude Code

Work one phase at a time, in small tasks. Plan first, review the plan, then let Claude Code build and test. Commit after every working step.

**Setup**

1. Install Claude Code on your PC and sign in.
2. Create an empty folder, run `git init`, and start Claude Code inside it.
3. Ask it to create `CLAUDE.md` from the template below, then build Phase 0.
4. Push to a private GitHub repository.

**CLAUDE.md template**

```markdown
# Crypto trading app — project rules

## Stack
Python 3.12, FastAPI, SQLAlchemy 2, Alembic, ccxt (async), pandas, Redis,
PostgreSQL + TimescaleDB. Frontend: Next.js, TypeScript, Tailwind, shadcn/ui.

## Layout
backend/ API · engine/ trading worker · shared/ models and config · frontend/ · infra/

## Hard rules
- Every order goes through RiskManager. Never call the exchange from a strategy.
- Strategies return signals only; they never place orders.
- Default mode is PAPER. LIVE needs an explicit config flag and a confirmation.
- Never commit secrets. Read keys only from environment variables.
- Never enable or use withdrawal endpoints.
- Every order uses a unique client order ID.
- Use Decimal, not float, for prices, amounts, and balances.

## Quality
- Write tests for all engine, risk, and strategy code. Run pytest before finishing.
- Run ruff and mypy; fix all errors.
- Log decisions with structlog, including the inputs behind each signal.

## Commands
- Start everything: docker compose up
- Tests: docker compose run engine pytest
```

**Workflow tips**

- Use plan mode for each phase. Ask for the plan, edit it, then approve.
- Give one feature per request, with a clear "done when" check.
- Ask for tests first on money-related code: risk rules, position math, fees.
- After each task, ask Claude Code to run tests and explain any failures before fixing them.
- Review every diff that touches orders, risk, or keys yourself. Don't approve those blindly.
- Keep a `docs/decisions.md` log of design choices, so later sessions stay consistent.
- If a session drifts, start a new one and point it to `CLAUDE.md` and the current phase.

## Timeline, costs, and go-live checklist

Expect about 12 weeks part-time (10–15 hours a week) from empty folder to first small live trade. Running costs stay under about USD 30 a month, not counting your Claude plan.

**Running costs (approximate)**

| Item | Cost |
| --- | --- |
| VPS, 2–4 GB RAM, Singapore region | About USD 6–24 a month |
| Domain name (optional with Tailscale) | About USD 10–15 a year |
| Off-server backups (object storage) | About USD 1–5 a month |
| Sentry, Telegram, Tailscale, GitHub | Free tiers are enough |
| PostgreSQL, TimescaleDB, Redis | Free, self-hosted |
| Exchange trading fees | Depends on volume; 0.1–0.5% per trade |

**Go-live checklist**

- [ ] Strategy backtested over at least 1–2 years, including fees and slippage
- [ ] Walk-forward results close to in-sample results (not overfitted)
- [ ] Paper trading for 2–4 weeks with results close to the backtest
- [ ] Every risk rule covered by tests and checked by hand once
- [ ] Kill switch tested from the dashboard and from Telegram
- [ ] Restart test passed: positions and orders recovered correctly
- [ ] API key is trade-only, IP-locked, with no withdrawal permission
- [ ] Dashboard behind two-factor login and Tailscale or IP limits
- [ ] Nightly backups tested by restoring once
- [ ] Alerts arrive on your phone for trades, errors, and downtime
- [ ] Starting capital is an amount you can afford to lose fully
- [ ] Weekly review time set in your calendar
