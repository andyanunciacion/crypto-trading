# crypto-trading — Architecture

> **How** crypto-trading is built. For product scope see [PRD.md](PRD.md); for build order see [ROADMAP.md](ROADMAP.md).
> Decisions and the reasons behind them are in §12 (decision log). When a decision changes, update this file so it always describes the current plan.

## 1. Guiding constraints

- **Solo builder + Claude, learning as we go.** Prefer well-documented, widely used tools with good docs and examples. Fewer moving parts beats clever designs.
- **Money is at stake.** Correctness and safety come before speed and features. Money code is tested first and reviewed by hand.
- **The engine must survive the UI.** The trading engine is its own process. The API and dashboard can crash or restart without affecting trading or risk checks (D3).
- **Philippine exchange access.** Coins.ph is the first exchange; everything exchange-specific stays behind one adapter (D4).
- **Low running cost.** Under about USD 30/month, self-hosted on one small VPS (D10).
- **Same setup on PC and server.** Everything runs in Docker Compose (Docker Desktop + WSL 2 locally).

## 2. System overview

Five services in Docker Compose: dashboard, API, alerts bot, trading engine, and storage (PostgreSQL + Redis). **The engine is the only part that talks to the exchange.**

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

Prices flow from the exchange through market data to the strategies. Signals pass the RiskManager before the broker sends any order. The dashboard and Telegram never trade directly: they send **commands** to the engine through Redis, and the engine acts on them.

## 3. Tech stack

### 3.1 Tooling and repo
| Concern | Choice | Why |
|---|---|---|
| Repo | **Monorepo**, private GitHub | One place for engine, API, frontend and docs |
| Python packaging | **uv** (proposed, D13) | Fast; lockfile; installs Python versions |
| Node packaging | **pnpm**, Node 20+ LTS | Fast, strict dependency resolution |
| Lint / types (Python) | **ruff**, **mypy** (strict) | Catch bugs early; keep Claude's output consistent |
| Lint / format (TS) | **ESLint**, **Prettier** | Standard for Next.js |
| Hooks | **pre-commit** with ruff, mypy, gitleaks | Secret scanning before anything reaches Git |
| CI | **GitHub Actions** | Lint, type check and tests on every push |
| Containers | **Docker + Docker Compose** | Same setup on the PC and the server |

### 3.2 Engine and API (Python 3.12+)
| Concern | Choice | Why |
|---|---|---|
| Exchange access | **CCXT** (async), wrapped in `ExchangeAdapter` | One API for 100+ exchanges; supports `coinsph` |
| API | **FastAPI + Uvicorn** | Async, typed, auto-generated docs, built-in WebSockets |
| Engine | **Separate asyncio worker** | Survives API/UI restarts (D3) |
| Indicators | **pandas, numpy** + pandas-ta or TA-Lib (open question) | RSI, EMA, MACD, Bollinger Bands, ATR, ADX |
| Backtesting | **Custom event-driven backtester** (D15) | Runs the same strategy code as live trading |
| Validation and config | **pydantic**, **pydantic-settings** | Typed settings, strategy params and API schemas |
| ORM and migrations | **SQLAlchemy 2.0 + Alembic** | Typed models; versioned schema changes |
| Logging | **structlog** (JSON) | Every decision logged with its inputs |
| Errors | **Sentry** (free tier) | Stack traces and alerts for crashes |
| Alerts | **python-telegram-bot** | Phone alerts and remote kill switch (D16) |
| Tests | **pytest, pytest-asyncio** | Engine, risk and strategy code must be tested |

### 3.3 Dashboard
| Concern | Choice | Why |
|---|---|---|
| Framework | **Next.js** (React, TypeScript) | Pages and routing; well supported by Claude |
| UI | **Tailwind CSS + shadcn/ui** | Clean components without heavy design work |
| Charts | **TradingView Lightweight Charts** (candles), **Recharts** (P&L, equity) | Fast candlestick charts with markers |
| Data | **TanStack Query** + native WebSocket | Cached REST calls plus live updates |
| E2E tests | **Playwright** | Login, kill switch and bot control flows |

### 3.4 Infrastructure (self-hosted)
| Concern | Choice |
|---|---|
| Database | **PostgreSQL 16 + TimescaleDB** (D5) |
| Cache / messaging | **Redis** (D6) |
| Server | **VPS in Singapore**, Ubuntu 24.04 LTS, 2–4 GB RAM (D10) |
| HTTPS / proxy | **Caddy** (automatic HTTPS) |
| Private access | **Tailscale** or firewall IP limits (open question) |
| Backups | Nightly `pg_dump` to off-server object storage |

## 4. Repository layout

```text
backend/     FastAPI app: REST + WebSocket, auth, reads DB, sends commands to the engine via Redis
engine/      Trading worker: market data, strategies, risk manager, brokers, bot runner, regime switcher
shared/      Code used by both: SQLAlchemy models, pydantic schemas, config, Decimal/money helpers
frontend/    Next.js dashboard
infra/       Docker Compose files, Caddy config, deploy and backup scripts
AI_DOCS/     Planning docs (this folder)
```

Each Python package has its own `tests/` folder. Alembic migrations live in `shared/`.

## 5. Data model

Conventions:
- **Money and quantities are `Decimal`** in Python and `NUMERIC` in Postgres. Indicator math may use float inside pandas; anything that becomes a price, amount, fee, balance or P&L is converted to Decimal at the strategy → risk boundary (D9).
- **Timestamps are UTC** (`timestamptz`). The dashboard shows Asia/Manila time.
- **IDs:** `bigint` identity for internal rows; orders also carry a unique `client_order_id` (D18).
- **Symbols** use CCXT's unified format (`BTC/PHP`, `BTC/USDT`).
- **Schema changes go through Alembic migrations only.**
- Every row that records a trading decision stores the **mode** (`backtest`, `paper`, `live`).

### 5.1 Entities by phase

| Entity | Key fields | Phase | Notes |
|---|---|---|---|
| `candles` | exchange, symbol, timeframe, ts, open, high, low, close, volume | P1 | TimescaleDB hypertable; unique (exchange, symbol, timeframe, ts) |
| `backtest_runs` | strategy, version, params, symbol, timeframe, range, fee/slippage model, metrics, equity curve | P2 | Results are reproducible from stored inputs |
| `bots` | name, exchange_account_id, symbol, timeframe, mode, status, active_strategy_config_id, switch_policy | P3 | `mode` defaults to `paper` |
| `strategy_configs` | bot_id, strategy, version, params, created_at, created_by | P3 | Append-only: a change adds a row (history and rollback in P6) |
| `orders` | bot_id, client_order_id, exchange_order_id, side, type, price, amount, status, mode, reason | P3 | `client_order_id` unique; `reason` links to the signal and risk check |
| `fills` | order_id, price, amount, fee, fee_currency, ts | P3 | Partial fills are separate rows |
| `positions` | bot_id, symbol, side, amount, avg_entry, stop, take_profit, opened_at, closed_at, realized_pnl | P3 | Rebuilt and checked on restart |
| `paper_balances` | bot_id, currency, amount | P3 | Virtual balances for paper mode |
| `risk_settings` | the limits in §6, scope (global or per bot) | P4 | Editable from Settings in P5 |
| `risk_events` | bot_id, rule, decision (allowed/blocked/resized), inputs, ts | P4 | Every block or resize is recorded |
| `users` | username, password_hash, totp_secret (encrypted) | P5 | One row; see §9 |
| `exchange_accounts` | exchange, label, api_key_enc, api_secret_enc, sandbox | P5 | Keys encrypted with Fernet (D12) |
| `strategy_switches` | bot_id, from_config, to_config, trigger (manual/regime), regime, reason, ts | P6 | Shown on the chart and sent to Telegram |
| `alert_settings` | event type, enabled | P7 | |

## 6. Risk management

Every order goes **strategy signal → RiskManager → Broker**. Nothing else can reach a broker. From Phase 3 the RiskManager is in the path, at first as a pass-through stub, so the wiring never has to change (D8).

**Default limits** (starting values; change to suit):

| Limit | Starting value |
|---|---|
| Risk per trade | 1% of account equity |
| Max open positions | 3 |
| Max exposure per coin | 25% of equity |
| Max daily loss | 3% of equity, then pause all bots until the next day (Asia/Manila) |
| Max drawdown | 10% from peak, then stop and alert |
| Stale data cutoff | No orders if the last price is older than 2 candles |
| Error breaker | Pause a bot after 3 failed orders in 10 minutes |

**Kill switch:** cancels open orders, stops all bots, and closes positions if asked to. It can be triggered from the dashboard, Telegram, or the engine itself (drawdown breach), and it must work even when the API is down: the engine listens on a Redis command channel.

## 7. Strategy system

Every strategy is a plug-in with the same interface. The backtester, paper broker and live broker all run it unchanged (D7).

- `name`, `version`, and a typed `params` schema (pydantic; the dashboard renders it as a form)
- `warmup_candles`: how much history it needs before it gives signals
- `on_candle(market_data, position)` → BUY, SELL or HOLD, with an optional size hint and stop level
- Optional `on_fill()`, `on_start()`, `on_stop()` hooks
- **No exchange access.** Strategies return signals only.

| Strategy | Works best in | Core idea | Phase |
|---|---|---|---|
| EMA crossover | Trending markets | Buy when the fast EMA crosses above the slow one; sell on the cross below | P2 |
| RSI mean reversion | Ranging markets | Buy when RSI is oversold; sell when it recovers | P2 |
| Bollinger breakout | Volatility expansion | Enter on a close outside the band with rising volume | P2 |
| Grid trading | Sideways markets | Buy and sell orders at fixed steps inside a price range | P6 |
| DCA | Long-term accumulation | Buy a fixed amount on a schedule or on dips | P6 |

**Regime switching (P6):** a `RegimeDetector` labels each candle (trending up/down, ranging, high volatility) using ADX, ATR and EMA slope. For example, ADX > 25 means trending; ADX < 20 with narrow Bollinger Bands means ranging. A mapping table links each regime to a strategy or to "stay flat". A regime must hold for a minimum number of candles before a switch. Each bot's `switch_policy` decides what happens to open positions. Every switch is logged and alerted. Auto-switching is backtested as one combined strategy before it's turned on.

- **When to revisit:** if strategies need multiple symbols or timeframes at once, `on_candle` needs a richer market-data input.

## 8. Exchange integration

- `ExchangeAdapter` wraps CCXT: balances, candles, tickers, place/cancel order, plus a sandbox flag. Nothing else imports CCXT.
- First exchange: **Coins.ph / Coins Pro** (`coinsph` in CCXT). Whether it has a testnet and WebSocket support in CCXT is an open question (PRD §9). The fallback is REST polling, with paper mode as the testing environment.
- PHP pairs can have thin order books: model higher slippage in backtests and prefer limit orders.
- Rate limits are respected (CCXT `enableRateLimit`), with retries and backoff on network errors only, never on rejected orders.
- Status of exchanges for Philippine users as of October 2026 is in `archive/main-file.md`. Recheck the BSP and SEC lists before switching.

## 9. Auth and authorization

- **One user.** Password (argon2 hash) + TOTP (pyotp). JWT in an httpOnly, secure cookie (D11).
- The API checks auth on every REST and WebSocket route; nothing relies on the UI to hide things.
- The dashboard is reachable only via Tailscale or IP limits, never openly on the internet.
- Telegram commands are accepted only from the configured chat ID.

## 10. Security and quality

- **Secrets and config:** `.env` (never committed; `.env.example` has no real values). Exchange keys are encrypted at rest with Fernet; the encryption key lives only in the server's environment (D12). gitleaks runs in pre-commit.
- **Exchange keys:** trade-only, IP-locked to the server, never with withdrawal permission. A separate sub-account holds only the money the bot may trade.
- **Live mode:** `paper` is the default. Live needs an explicit config flag **and** a confirmation (D17).
- **Orders:** unique `client_order_id` so retries never duplicate; on startup the engine reconciles with the exchange before trading (D18).
- **Logs:** structlog JSON with the inputs behind each signal and risk decision. Never log keys, secrets or TOTP codes.
- **Testing:** unit tests for every risk rule, position math and fee calculation, written first. Strategy tests use known price series. Backtest-vs-replay parity test. Integration tests against Postgres/Redis in Docker. Playwright for login, bot control and kill switch.
- **Environments:** local (Docker Desktop), server (paper first, then live). There's no separate staging server: paper mode on the server plays that role.

## 11. Cost estimate

Rough figures as of October 2026. **Verify current pricing before committing.**

| Item | Est. monthly |
|---|---|
| VPS, 2–4 GB RAM, Singapore | USD 6–24 |
| Off-server backups (object storage) | USD 1–5 |
| Domain (optional with Tailscale) | ~USD 1 (USD 10–15/year) |
| Sentry, Telegram, Tailscale, GitHub | Free tiers |
| PostgreSQL, TimescaleDB, Redis | Free, self-hosted |
| **Total** | **≈ USD 8–30**, plus exchange fees (0.1–0.5% per trade) |

## 12. Decision log

| # | Decision | Why | Revisit when |
|---|---|---|---|
| D1 | **Custom app on CCXT** instead of extending Freqtrade | Learn and own the whole design; tailor the risk layer and switching | The goal shifts to "a working bot soon", or a phase badly overruns |
| D2 | **Python for engine/API, TypeScript/Next.js for the dashboard** | Python has the trading and data libraries; Next.js is well supported | — |
| D3 | **Engine is a separate worker**; API/UI talk to it only via Redis commands and the DB | Trading and risk checks keep running if the UI or API crashes | — |
| D4 | **Coins.ph (`coinsph`) first, behind a generic `ExchangeAdapter`** | BSP-licensed with an API; most global exchanges are blocked for PH users | Binance or another exchange becomes legal and offers better liquidity/fees |
| D5 | **PostgreSQL + TimescaleDB** instead of a separate time-series DB | One database for candles and trading records | Candle queries get slow at the data volume we keep |
| D6 | **Redis** for live prices, pub/sub commands and locks | Simple and fast; one extra container | Commands must survive a Redis restart → move them to Redis Streams or a DB queue |
| D7 | **Same strategy code in backtest, paper and live; strategies return signals only** | Comparable results; safe switching; no exchange access from strategies | — (core rule) |
| D8 | **RiskManager on every order path, as a stub from Phase 3** | No order can ever bypass risk; the wiring never changes | — (core rule) |
| D9 | **Decimal for money; floats only inside indicator math** | Avoid rounding errors in prices, amounts, fees and P&L | — |
| D10 | **Self-hosted on one Singapore VPS with Docker Compose** instead of managed services | Low cost; low latency from Manila and to the exchange; TimescaleDB is costly as a managed service | Server upkeep takes more time than building |
| D11 | **Single user, JWT cookie + TOTP**, dashboard behind Tailscale/IP limits | The app controls money; nobody else uses it | More users are needed |
| D12 | **Exchange keys encrypted with Fernet**, master key in server env | Keys never stored in plain text | Moving to a managed secrets store |
| D13 | **uv** instead of Poetry (proposed) | Faster; lockfile; installs Python itself | Tooling friction in Docker or CI |
| D14 | **pnpm** for the frontend | Fast, strict installs | — |
| D15 | **Custom event-driven backtester** as the source of truth; vectorbt only if sweeps are too slow (proposed) | Realistic fills/fees; reuses live strategy code (D7) | Parameter sweeps take too long → add vectorbt for coarse sweeps only |
| D16 | **Telegram** for alerts and remote commands | Free, instant on the phone, has a bot API | — |
| D17 | **Paper is the default mode**; live needs a config flag + confirmation | Stops accidental real-money trading | — |
| D18 | **Unique client order IDs + reconcile with the exchange on startup** | Retries never duplicate orders; state stays correct after a crash | — |
| D19 | **Phases with exit criteria, not dates** | Part-time solo work; timing is unpredictable | — |
| D20 | **Decision log lives here** instead of a separate `docs/decisions.md` | One source of truth for the "why" | — |
