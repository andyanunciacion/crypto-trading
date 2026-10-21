# crypto-trading

A self-hosted web app that watches crypto markets, runs swappable trading strategies and places orders automatically, with a risk layer that can't be bypassed. A personal tool for one operator, trading spot on MEXC from the Philippines (ARCHITECTURE D21).

## Docs — read before feature work
- `AI_DOCS/PRD.md`: what and why (features are tagged by phase: [P0]–[P9])
- `AI_DOCS/ARCHITECTURE.md`: stack, data model, risk limits, strategy system, decision log
- `AI_DOCS/DESIGN.md`: UX principles, screens, UX decisions, tokens, performance and accessibility budgets. Read it before any UI work.
- `AI_DOCS/ROADMAP.md`: build order as vertical slices. **Tick the checkbox when a slice ships.**
- `AI_DOCS/github-issues.md`: the roadmap broken into GitHub-issue-sized pieces
- `AI_DOCS/archive/`: superseded drafts (including the original `main-file.md`). Ignore them; don't use them as a source of truth.

If code and docs disagree, ask which is right, then update the one that's wrong. When a decision changes, update ARCHITECTURE.md's decision log in the same change.

## Hard rules
- **Every order goes through the RiskManager.** No bypass flags, no direct broker calls.
- **Strategies return signals only.** They never place orders or touch the exchange.
- **Only `ExchangeAdapter` imports CCXT.** Only the engine talks to the exchange; API and UI send commands via Redis.
- **Default mode is PAPER.** LIVE needs an explicit config flag and a confirmation.
- **Never enable or use withdrawal endpoints.**
- **Never commit secrets.** Keys come from environment variables; exchange keys are encrypted at rest.
- **Every order has a unique client order ID.**
- **Use Decimal, not float**, for prices, amounts, fees, balances and P&L (floats only inside indicator math).

## Current status
Phase 0: planning docs written; repo on GitHub (`andyanunciacion/crypto-trading`, branch `production`). The Docker Compose stack runs: TimescaleDB, Redis, a FastAPI `/health` stub, an engine heartbeat loop and a Next.js hello page, all with health checks. Quality gates are in place: ruff, mypy strict, pytest, ESLint, Prettier and gitleaks, all run by pre-commit.

**Work queue:** `AI_DOCS/github-issues.md` until the issues exist on GitHub, then GitHub. Done: planning docs, P0-T1, P0-T2, P0-T3, P0-T4. Next: P0-T5 (CI) → Phase 1. Branches: `<issue#>-<short-slug>`. Commit titles: `#<issue#> <title>` (e.g. `#1 Switch first exchange to MEXC; mark P0-T1 done`), every commit, taking the issue number from the branch name.

## Commands
Only list commands that exist. First run: copy `.env.example` to `.env` and set a password.
- `docker compose up --build -d`: build and start all five services; `docker compose ps` should show each one `(healthy)`
- `docker compose logs -f engine`: follow the engine's JSON logs
- `docker compose down`: stop everything (data stays in the `pgdata`/`redisdata` volumes; `-v` would delete it)
- `uv sync --all-packages`: create or update the local `.venv` for backend, engine and the dev tools (Python 3.12, from `.python-version`)
- `uv run pytest`: run every Python package's tests
- `uv run ruff check .` / `uv run ruff format .`: lint / format Python
- `uv run mypy`: strict type check of all Python packages and tests
- `uv run pre-commit install`: once per clone, so the hooks run on every commit
- `uv run pre-commit run --all-files`: every check, incl. gitleaks, ESLint and Prettier
- In `frontend/`: `pnpm install`, `pnpm typecheck`, `pnpm lint`, `pnpm format` / `pnpm format:check`

Planned: CI (P0-T5).

## Working with the owner
- Solo developer **learning both the software and the trading side**. Briefly explain the **why** behind non-obvious code patterns (async, migrations, Docker, Next.js) *and* trading concepts (slippage, drawdown, walk-forward, regimes). Keep it to a sentence or two and name the concept so it can be looked up.
- Work one phase at a time. Use plan mode for each slice: propose the plan, wait for review, then build.
- Keep changes small and reviewable: one slice or issue at a time, with a clear "done when".
- **Tests first for money code** (risk rules, sizing, fees, P&L). Run tests, ruff and mypy before calling anything done; explain failures before fixing them.
- Flag any diff that touches orders, risk or keys so the owner reviews it by hand.
- Before adding a dependency or service, say what it is and why, and prefer what ARCHITECTURE.md already lists.
- Don't commit unless asked.

## Engineering conventions
- Python 3.12+, fully typed; mypy strict and ruff clean. TypeScript `strict`; no `any`.
- Validate at boundaries with pydantic: env config, API inputs, exchange responses, strategy params.
- Trading logic lives in `engine/`; `backend/` only reads data and sends commands; `frontend/` is a client only.
- Timestamps are UTC (`timestamptz`); the UI shows Asia/Manila. Money is `NUMERIC` in Postgres and strings in JSON.
- Schema changes go through Alembic migrations only.
- Log every signal, risk decision, order and strategy switch with structlog, including its inputs. Never log keys, secrets or TOTP codes.
- Tests live in each package's `tests/`; strategies are tested on known price series; critical UI flows (login, bot control, kill switch) are tested with Playwright.

## Domain glossary
- **Candle**: OHLCV bar for one symbol and timeframe. Strategies act only on **closed** candles.
- **Signal**: a strategy's output for a candle: BUY, SELL or HOLD, with an optional size hint and stop.
- **Strategy**: a plug-in that turns market data into signals. It has a name, version, params and warm-up.
- **Strategy config**: a strategy plus specific params, versioned and append-only.
- **Bot**: one strategy config running on one symbol/timeframe/account, in `paper` or `live` mode.
- **Mode**: `backtest`, `paper` (fake balances, live prices) or `live` (real money).
- **Broker**: places orders. `PaperBroker` simulates fills; `LiveBroker` uses the exchange. Both share one interface.
- **RiskManager**: sits between signals and the broker; sizes, blocks or allows every order.
- **Kill switch**: cancels orders, stops all bots, optionally closes positions.
- **Circuit breaker**: automatic pause on stale data, repeated errors or price spikes.
- **Regime**: market state (trending up/down, ranging, high volatility) used for auto-switching.
- **Slippage**: the difference between the expected price and the fill price.
- **Drawdown**: the fall in equity from its peak.
- **Walk-forward**: optimize on one window, test on the next, then roll forward, to catch overfitting.
- **Reconciliation**: on startup, comparing engine records with the exchange and fixing mismatches before trading.
- **Client order ID**: our own unique ID on each order, so retries never duplicate.
