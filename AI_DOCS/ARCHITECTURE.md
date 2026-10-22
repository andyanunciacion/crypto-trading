# crypto-trading — Architecture

> **How** crypto-trading is built. For product scope see [PRD.md](PRD.md); for build order see [ROADMAP.md](ROADMAP.md).
> Decisions and the reasons behind them are in §12 (decision log). When a decision changes, update this file so it always describes the current plan.

## 1. Guiding constraints

- **Solo builder + Claude, learning as we go.** Prefer well-documented, widely used tools with good docs and examples. Fewer moving parts beats clever designs.
- **Money is at stake.** Correctness and safety come before speed and features. Money code is tested first and reviewed by hand.
- **The engine must survive the UI.** The trading engine is its own process. The API and dashboard can crash or restart without affecting trading or risk checks (D3).
- **One exchange to start, easy to leave.** MEXC spot is the first exchange; everything exchange-specific stays behind one adapter, so moving means writing a new adapter, not a rewrite (D21).
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
| Exchange API (MEXC, via CCXT)                                      |
| Trade-only key, IP-locked, no withdrawals                          |
+--------------------------------------------------------------------+
```

Prices flow from the exchange through market data to the strategies. Signals pass the RiskManager before the broker sends any order. The dashboard and Telegram never trade directly: they send **commands** to the engine through Redis, and the engine acts on them.

## 3. Tech stack

### 3.1 Tooling and repo
| Concern | Choice | Why |
|---|---|---|
| Repo | **Monorepo**, private GitHub | One place for engine, API, frontend and docs |
| Python packaging | **uv** workspace, one `uv.lock` at the root (D13) | Fast; lockfile; installs Python versions; API and engine share library versions |
| Node packaging | **pnpm**, Node 24 LTS | Fast, strict dependency resolution |
| Lint / types (Python) | **ruff**, **mypy** (strict) | Catch bugs early; keep Claude's output consistent |
| Lint / format (TS) | **ESLint**, **Prettier** | Standard for Next.js |
| Hooks | **pre-commit** with ruff, mypy, gitleaks | Secret scanning before anything reaches Git |
| CI | **GitHub Actions** | Lint, type check and tests on every push |
| Containers | **Docker + Docker Compose** | Same setup on the PC and the server |

### 3.2 Engine and API (Python 3.12+)
| Concern | Choice | Why |
|---|---|---|
| Exchange access | **CCXT** (async), wrapped in `ExchangeAdapter` | One API for 100+ exchanges; supports `mexc` |
| API | **FastAPI + Uvicorn** | Async, typed, auto-generated docs, built-in WebSockets |
| Engine | **Separate asyncio worker** | Survives API/UI restarts (D3) |
| Indicators | **Hand-rolled** in `engine/src/engine/strategies/indicators.py` (D28); pandas-ta or TA-Lib only if that stops scaling | RSI, EMA, MACD, Bollinger Bands, ATR, ADX |
| Backtesting | **Custom event-driven backtester** (D15) | Runs the same strategy code as live trading |
| Validation and config | **pydantic**, **pydantic-settings** | Typed settings, strategy params and API schemas |
| ORM and migrations | **SQLAlchemy 2.x (async) + Alembic**, **asyncpg** driver (D24) | Typed models; versioned schema changes |
| Redis client | **redis-py** (`redis.asyncio`) | Official client, asyncio built in; latest prices and pub/sub (D26) |
| Logging | **structlog** (JSON) | Every decision logged with its inputs |
| Errors | **Sentry** (free tier) | Stack traces and alerts for crashes |
| Alerts | **python-telegram-bot** | Phone alerts and remote kill switch (D16) |
| Tests | **pytest, pytest-asyncio**, **testcontainers** (throwaway TimescaleDB for DB tests) | Engine, risk and strategy code must be tested |

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
infra/       Caddy config, deploy and backup scripts
AI_DOCS/     Planning docs (this folder)
compose.yaml       Docker Compose stack (at the root, so `docker compose up` works from a fresh clone)
pyproject.toml     uv workspace root; members: backend, engine, shared
uv.lock            the single Python lockfile for the workspace
```

Each Python package has its own `tests/` folder. Alembic migrations live in `shared/src/shared/db/migrations/` and ship inside the package; the one-shot `migrate` Compose service applies them before the API and engine start (D24).

## 5. Data model

Conventions:
- **Money and quantities are `Decimal`** in Python and `NUMERIC` in Postgres. Indicator math may use float inside pandas; anything that becomes a price, amount, fee, balance or P&L is converted to Decimal at the strategy → risk boundary (D9).
- **Timestamps are UTC** (`timestamptz`). The dashboard shows Asia/Manila time.
- **IDs:** `bigint` identity for internal rows; orders also carry a unique `client_order_id` (D18).
- **Symbols** use CCXT's unified format (`BTC/USDT`, `ETH/USDT`).
- **Schema changes go through Alembic migrations only.**
- Every row that records a trading decision stores the **mode** (`backtest`, `paper`, `live`).

### 5.1 Entities by phase

| Entity | Key fields | Phase | Notes |
|---|---|---|---|
| `candles` | exchange, symbol, timeframe, ts, open, high, low, close, volume | P1 | TimescaleDB hypertable on `ts` (7-day chunks); primary key (exchange, symbol, timeframe, ts); `ts` is the open time; closed candles only; never synthesized (D25) |
| `backtest_runs` | strategy, strategy_version, params, exchange, symbol, timeframe, range_start/range_end, warmup_candles, initial_cash, fee_rate, slippage_rate, market_rules, candle_count, candles_sha256, backtester_version, final_equity, total_fees, result (JSONB: signals, fills, skipped orders, equity curve) | P2 | Plain table. Reproducible from stored inputs: a re-run refuses if the candles' hash, the strategy version or the backtester version changed (D29). Metrics come in P2-3 |
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

Every strategy is a plug-in with the same interface (`engine/src/engine/strategies/base.py`). The backtester, paper broker and live broker all run it unchanged (D7).

- `name`, `version` (an int, bumped when the logic changes), and a typed `params_model` (pydantic, `extra="forbid"`; the dashboard renders its JSON schema as a form)
- `warmup_candles`: how much history it needs before it gives signals
- `on_candle(candles, position)` → `Signal`. `candles` are closed candles, oldest first, ending with the one that just closed; `position` is `None` when flat. Until there are `warmup_candles` of them it returns HOLD (`"warming up"`); after that, the base class passes exactly the last `warmup_candles` to the strategy's `decide()`. **Strategies are stateless (D28):** a signal depends only on that window.
- `Signal`: `action` (`buy`/`sell`/`hold`), `ts` (the candle's open time), `reason`, `indicators` (the values behind it, for logging and UX9), and optional `size_hint` (0–1) and `stop` price, both Decimal. Strategies don't log; whoever calls them logs the signal.
- `on_fill()`, `on_start()`, `on_stop()` hooks: deferred until P3, when there are fills
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

**Backtester (D29)** (`engine/src/engine/backtest/`): `run_backtest()` in `simulator.py` is pure and synchronous. It replays stored candles through the strategy's `on_candle`, giving it the last `warmup_candles` each time. The warm-up candles are loaded from before the range, so trading starts exactly at `start`.
- A signal on a candle fills at the **next candle's open**, never its own close (that would be look-ahead bias). A signal on the last candle stays unfilled.
- Slippage moves every fill against us. Defaults: 0.05% for BTC/USDT and ETH/USDT, 0.30% for other pairs. The fee is `fee_rate` (default 0.1%) on each fill's value, charged in the quote currency.
- Spot, long only, one position. Until the RiskManager sizes orders (P4), a BUY spends all the cash times `size_hint` and a SELL sells everything.
- Amounts round down to the market's step. Orders below `min_amount`/`min_cost` are skipped with a reason, never rounded up. A gap in the candles refuses the run (D25).
- Each run is stored in `backtest_runs` (`store.py`). `run.reproduce()` re-runs a stored run from its inputs. `strategies/registry.py` maps a stored strategy name back to its class.

## 8. Exchange integration

- `ExchangeAdapter` (`engine/src/engine/exchange/`) wraps CCXT: balances, candles, tickers, market rules (step sizes, minimums), place/cancel limit order, plus a sandbox flag. Nothing else imports CCXT, and a test enforces that. It returns pydantic models with `Decimal` money: CCXT runs in string-number mode, so values never pass through float. It raises its own errors, of which only `ExchangeNetworkError` is retryable. Tests replay recorded MEXC JSON through real CCXT parsing, with no network.
- **Adapter guards (D22):** order calls refuse unless the adapter was built with `trading_enabled=True`. Orders off the market's step sizes or below its minimums are refused before sending, never rounded. `sandbox=True` on an exchange with no sandbox is an error, never a quiet fallback to live.
- First exchange: **MEXC spot** (`mexc` in CCXT), see D21. **MEXC has no spot testnet**, so paper mode is the testing environment. For LiveBroker's real-HTTP order path, P9 adds a step on **Binance's spot testnet** before the tiny real MEXC order (D23). The adapter loads spot markets only, which sandbox mode on Binance needs. CCXT's `watch*` WebSockets work for MEXC spot (Protobuf, ccxt ≥ 4.5.18; decoding needs the `protobuf` package, pinned to 5.x to match CCXT's generated code); REST polling stays as the fallback (PRD §9).
- **Live prices (D26):** the engine runs one `PriceStream` per symbol in `PRICE_SYMBOLS` (`engine/src/engine/marketdata/prices.py`). It watches the WebSocket ticker. On MEXC spot that's the book ticker: best bid and ask, up to 10 updates a second, no last trade. Every update goes to Redis: the key `price:<exchange>:<symbol>` holds the latest one (no TTL), and the channel `prices:<exchange>:<symbol>` gets each one. The payload is `shared.prices.PriceUpdate` (money as strings; `ts` is the exchange's time, `source` is `ws` or `rest`). If the stream errors, or is silent for 30 s, the engine drops that symbol's connection and reconnects after an exponential backoff with jitter (`engine/src/engine/backoff.py`, 1 s doubling to 60 s), polling the REST ticker every 2 s meanwhile. Each symbol has its own WebSocket connection, sharing the REST client's markets.
- **API keys:** IP-lock every key. On MEXC a key with no IP expires after 90 days. Give it spot read + trade only (no withdraw/transfer), and optionally limit it to the traded pairs.
- **MEXC candle history** (probed October 2026, BTC/USDT): at most 500 candles per request. Kept for about **30 days at 1m**, about **360 days at 5m/15m**, and back to at least 2019 at 1h/4h/1d; older requests return an empty list, not an error. So 1m history older than 30 days can't be backfilled: once the engine keeps candles current (P1-3), an outage longer than that leaves a permanent 1m gap. The backfill (`engine/src/engine/marketdata/`) pages forward with `fetch_candles`, stores closed candles only, and fills gaps between stored candles.
- Pairs are USDT-quoted. Small-cap pairs can have thin order books: model higher slippage for them in backtests and prefer limit orders.
- **Rate limits and retries (D27):** CCXT's `enableRateLimit` spaces requests by MEXC's endpoint weights. The adapter's reads (`fetch_candles`, `fetch_ticker`, `fetch_balances`, market loading) retry on `ExchangeNetworkError` only (timeouts, 5xx, 429, maintenance): up to 5 tries, with the same jittered backoff as reconnects (about 1, 2, 4, 8 s, `engine/src/engine/exchange/retry.py`). Each retry logs `exchange.retrying`, then `exchange.retry.recovered` or `exchange.retry.gave_up`. Rejections, auth errors and bad data are never retried. `place_order`/`cancel_order` are sent once and never retried by the adapter; WebSocket reconnects belong to `PriceStream` (`prices.stream.reconnecting`/`.recovered`).
- **Accepted regulatory risk (D21):** MEXC isn't registered with the Philippine SEC (named in the August 2025 advisory; ISP-blocked). Treat the account as something that could be restricted or frozen without notice: keep only the trading budget on it, and move profits off by hand regularly (the app itself never withdraws).

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
- **Environments:** local (Docker Desktop), server (paper first, then live). There's no separate staging server: paper mode on the server plays that role. Binance's spot testnet is a test-only exchange for LiveBroker in P9 (D23); it never holds real money.

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
| D4 | ~~**Coins.ph (`coinsph`) first, behind a generic `ExchangeAdapter`**~~ **Superseded by D21** | BSP-licensed with an API; most global exchanges are blocked for PH users | — |
| D5 | **PostgreSQL + TimescaleDB** instead of a separate time-series DB | One database for candles and trading records | Candle queries get slow at the data volume we keep |
| D6 | **Redis** for live prices, pub/sub commands and locks | Simple and fast; one extra container | Commands must survive a Redis restart → move them to Redis Streams or a DB queue |
| D7 | **Same strategy code in backtest, paper and live; strategies return signals only** | Comparable results; safe switching; no exchange access from strategies | — (core rule) |
| D8 | **RiskManager on every order path, as a stub from Phase 3** | No order can ever bypass risk; the wiring never changes | — (core rule) |
| D9 | **Decimal for money; floats only inside indicator math** | Avoid rounding errors in prices, amounts, fees and P&L | — |
| D10 | **Self-hosted on one Singapore VPS with Docker Compose** instead of managed services | Low cost; low latency from Manila and to the exchange; TimescaleDB is costly as a managed service | Server upkeep takes more time than building |
| D11 | **Single user, JWT cookie + TOTP**, dashboard behind Tailscale/IP limits | The app controls money; nobody else uses it | More users are needed |
| D12 | **Exchange keys encrypted with Fernet**, master key in server env | Keys never stored in plain text | Moving to a managed secrets store |
| D13 | **uv** instead of Poetry, as **one workspace with a single root `uv.lock`** (adopted in P0-T2) | Faster; lockfile; installs Python itself; one lock keeps API and engine on the same library versions | Tooling friction in Docker or CI |
| D14 | **pnpm** for the frontend | Fast, strict installs | — |
| D15 | **Custom event-driven backtester** as the source of truth; vectorbt only if sweeps are too slow (proposed) | Realistic fills/fees; reuses live strategy code (D7) | Parameter sweeps take too long → add vectorbt for coarse sweeps only |
| D16 | **Telegram** for alerts and remote commands | Free, instant on the phone, has a bot API | — |
| D17 | **Paper is the default mode**; live needs a config flag + confirmation. The flag is the global `MODE` setting (`paper`/`live`, default `paper`, P0-T3); each bot still has its own mode, and none can go live while `MODE=paper` | Stops accidental real-money trading | — |
| D18 | **Unique client order IDs + reconcile with the exchange on startup** | Retries never duplicate orders; state stays correct after a crash | — |
| D19 | **Phases with exit criteria, not dates** | Part-time solo work; timing is unpredictable | — |
| D20 | **Decision log lives here** instead of a separate `docs/decisions.md` | One source of truth for the "why" | — |
| D21 | **MEXC spot (`mexc`) first, behind the generic `ExchangeAdapter`** (supersedes D4) | Owner tested both: MEXC has lower fees, deeper USDT order books and many more pairs than Coins.ph. Accepted risk: MEXC isn't registered with the PH SEC, so the account could be restricted or frozen | MEXC restricts the account or region, or a registered PH exchange matches its fees and liquidity |
| D22 | **`ExchangeAdapter` has its own trading gate and never resizes orders** (adopted in P1-1). Order calls need `trading_enabled=True`, which only LiveBroker sets, and only when `MODE=live`. Orders that don't fit the market's steps/minimums are refused, not rounded. Limit orders only for now | Defense in depth on top of D17: a bug elsewhere still can't place a real order. Silent rounding would change size without the RiskManager seeing it; rounding belongs to sizing (P4-1) | A broker needs market orders (kill-switch close-out, P4-5) |
| D23 | **Binance spot testnet as a P9 test step for LiveBroker**, before the tiny real MEXC order. Test only: fake money, keys via GitHub login, no Binance account. The adapter loads spot markets only, because CCXT refuses Binance's futures testnet | MEXC has no testnet. The testnet exercises our generic order path against a real server (signing, client order IDs, place/fill/cancel, rejections, reconciliation), so the first real-money step only has MEXC's quirks left to find. It can't test MEXC-specific behaviour or strategies (thin, synthetic order books) | Binance retires the spot testnet, it becomes unreachable, or MEXC adds a testnet |
| D24 | **asyncpg as the single Postgres driver; migrations run by a one-shot `migrate` Compose service** (adopted in P1-2). Alembic's async `env.py` uses the same driver as the app. `api` and `engine` start only after `migrate` exits successfully | One driver for app and migrations. psycopg 3's async mode doesn't run on Windows' default event loop, which the local tests use. A separate migrate step means no service ever runs against an old schema, and two services never race to migrate | Need a driver feature asyncpg lacks, or migrations become slow enough to delay startup |
| D25 | **Missing candles are reported, never synthesized** (adopted in P1-2). Gaps the exchange can't fill are logged (`candles.gap_unfillable`) and the backfill CLI exits 1; nothing is forward-filled | A made-up flat candle is a fake price: strategies and backtests would trade on data that never existed. A visible gap can be judged by hand | Real gaps keep blocking backtests → decide per case (e.g. skip the window), still without writing fake rows |
| D26 | **Live prices: WebSocket book ticker → Redis key (latest, no TTL) + channel (every update); silence watchdog; REST polling during the reconnect backoff** (adopted in P1-3). `Ticker.last` is optional, because MEXC's spot stream carries bid/ask only. Any stream error reconnects; nothing crashes the stream | Bid/ask is the price you can actually trade at, and what the PaperBroker fills against (P3), so subscribing to trades only for `last` isn't worth a second stream. No TTL: a stale price must show as old (UX5), not vanish. A pulled network often leaves a socket silent rather than erroring, so silence counts as failure. Polling during the backoff keeps prices fresh without a separate fallback mode. Retrying is safe here because these are public prices, not orders (P1-4's rule) | A strategy or the UI needs the last trade price, or many symbols make per-symbol connections too costly (MEXC allows 30 subscriptions per connection) |
| D27 | **The adapter retries reads, never orders** (adopted in P1-4). Reads retry network errors up to 5 times with jittered backoff; orders and cancels are sent once, and a network error on them is passed up as "outcome unknown" | A read can be repeated freely. A timed-out order may already be on the book, and it isn't verified that MEXC refuses a repeated client order ID, so a blind resend could double a position. LiveBroker (P9) looks the order up by client order ID and reconciles before deciding (D18) | LiveBroker lands (P9): decide its resend rule, after checking how MEXC treats a duplicate client order ID |
| D28 | **Strategies are stateless over a warm-up window; indicators are hand-rolled** (adopted in P2-1). `on_candle` gets the candle history and the strategy decides on exactly the last `warmup_candles` of it, keeping nothing between calls. Indicators are plain float functions in `strategies/indicators.py`, tested against hand-worked values, with no pandas/pandas-ta/TA-Lib | Same window means same signal, so backtest, live and a restarted bot agree by construction, and hot-swaps (P6) have no state to hand over. That makes the parity test (P2-6) and restart recovery (P3-4) simple. An EMA's start-up seed fades by (1−α) per candle, so EMA crossover's 3 × slow warm-up leaves ~0.2% of it. Recomputing a window per candle is cheap at 1m+. EMA, RSI and Bollinger are a few lines each, and need no new install in Docker | Recomputing gets slow (very long windows, sweeps in P2-5), a strategy genuinely needs memory beyond a window, or the indicator list outgrows hand-rolling (ADX/ATR in P6 are the test) |
| D29 | **Backtest fills at the next candle's open, with slippage against us and a fee on each fill's value; gaps refuse the run; runs are stored with a hash of their candles** (adopted in P2-2). Defaults: fee 0.1%, slippage 0.05% for BTC/ETH and 0.30% for other pairs; all-in sizing until P4 | The next open is the earliest price a strategy acting on a closed candle could really get, so this avoids look-ahead bias. The defaults are deliberately pessimistic: a next-open fill acts like a taker order, and thin pairs slip more. A fill can't jump a gap to an unrelated price. The hash, the strategy version and `backtester_version` make "reproducible from stored inputs" checkable, not just hoped for | Paper results drift from backtests (PRD target ~20%): tune slippage and fees per pair from real fills; or the RiskManager (P4) takes over sizing |
