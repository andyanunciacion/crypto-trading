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
Phases 0, 1 and 2 done; Phase 3 in progress (P3-1, P3-2, P3-3 and P3-3b done; P3-4's code and kill check done, the 24-hour run remains); Phase 4 started (P4-1 done). Strategies live in `engine/src/engine/strategies/`: a stateless `Strategy` base (`on_candle(candles, position)` → `Signal`, deciding on exactly the last `warmup_candles`, D28), hand-rolled float indicators (EMA, Wilder's RSI, Bollinger Bands, Wilder's ATR), three strategies (`EmaCrossover`, `RsiMeanReversion`, `BollingerBreakout`; entries on events, exits on states, D31), and a registry mapping stored names back to classes. An event-driven backtester (`engine/src/engine/backtest/`, D29) replays stored candles through a strategy, filling each signal at the next candle's open with fees, slippage and minimum order sizes, and stores every run in `backtest_runs` with a hash of its candles so it can be reproduced. `report.py` turns a run's result into metrics (total return, max drawdown, Sharpe, trades, win rate, profit factor), computed when shown rather than stored (D30). `sweep.py` backtests every combination of a param grid over the same candles and ranks them by Sharpe; `walk_forward.py` picks the best set on each rolling train window and tests it unchanged on the window after, reporting in-sample and out-of-sample side by side (D32). `ExchangeAdapter` for MEXC exists (`engine/src/engine/exchange/`), tested offline against recorded responses; its reads retry network errors with backoff, orders are never auto-retried (D27). Candles are stored in a TimescaleDB hypertable (schema in `shared/src/shared/db/`, Alembic migrations applied by the one-shot `migrate` service); a backfill CLI (`engine/src/engine/marketdata/`) downloads history and finds/fills gaps. The engine streams live prices for `PRICE_SYMBOLS` into Redis (`price:<exchange>:<symbol>` key + `prices:<exchange>:<symbol>` channel, D26), over CCXT's WebSocket, with REST polling while it reconnects. The live signal path: `CandleStream` (`marketdata/candle_stream.py`) uses the WebSocket kline stream only to tell when a candle closed, then fetches it over REST, so live candles equal stored ones (D33). `SignalRunner` (`strategies/runner.py`) feeds them to a strategy over its warm-up window. A parity test replays a recorded 3-hour MEXC session through both and the backtester and requires identical signals. Brokers live in `engine/src/engine/brokers/` (D34): a `Broker` interface and a `PaperBroker` that fills limit orders at the live best bid/ask from Redis, capped by the size on offer there (partial fills), with the backtest fee. An order that crossed when placed keeps filling at the best price while it still crosses; a resting one fills at its limit. The size at a best price counts once (what our fills took there is remembered in memory), so a repeated quote doesn't fill again. It reserves funds for open orders, and stores everything in the `orders`, `fills`, `positions` and `paper_balances` tables (migration 0003). Client order IDs are deterministic per bot, candle and side, and the unique column makes a retried order return the first one. Money logic is pure and separately tested (`matching.py`, `ledger.py`). Only the `RiskManager` (`engine/src/engine/risk/manager.py`, D35) calls a broker. `submit()` logs `risk.decision` with the order's inputs, then places the order and stores the decision in its `reason`; `cancel()` is never blocked. For now it's a pass-through stub (P4 adds the rules). `test_broker_boundary.py` fails if anything outside `engine/brokers/` and `engine/risk/` imports more than the broker's data types, so whatever builds a `PaperBroker` lives in `engine/risk/`. The bot runner (`engine/src/engine/bots/`, D36) runs the `running` bots from the `bots` and `strategy_configs` tables (migration 0004, which also points the trading tables' `bot_id` at `bots`). It loads them once, at engine startup. Bots on a symbol/timeframe share one `CandleStream`. On each closed candle the candle is stored, then each bot cancels its leftover orders (an order lives one candle), gets its signal and, on BUY/SELL, submits a marketable limit order to the RiskManager. `orders.py` (pure) prices it at the ask/bid ± the backtest's slippage; a sell sells the whole position. Position sizing (D39) is one pure function, `entry_amount()` in `engine/risk/sizing.py`, which the bot runner and the backtester both call, so paper sizes exactly like its backtests. Its modes are `all_in` (default), `fixed`, `percent_of_equity` and `atr_risk` (lose `risk_percent` of equity at a stop `multiple` × ATR below). Each is scaled by `size_hint`, rounded down, capped at the free cash and refused below the market's minimums. A bot's sizing is JSON in `bots.sizing` and a backtest's in `backtest_runs.sizing` (migration 0005); each buy's `reason` records how it was sized. The RiskManager's limits (P4-3) will cap what sizing asks for. A bot's error is logged and never stops the others. `PaperDesk` (`engine/risk/paper.py`) builds the one shared PaperBroker, funds bots, and feeds it live prices from the Redis channels so resting orders fill. The quote reader is `engine/marketdata/quotes.py`. Restart recovery (D38): at startup, before prices are fed, `BotRunner.recover()` cancels every open paper order (stopped bots' too) and logs each bot's state from the database (`bot.recovered`); the newest closed candle only seeds the window. A bot acts only on a candle that closed less than one timeframe step ago; older ones (an outage's backlog) fill its window and cancel its leftovers (`bot.caught_up`). Repo on GitHub (`andyanunciacion/crypto-trading`, branch `production`). The Docker Compose stack runs: TimescaleDB, Redis, a FastAPI `/health` stub, the engine (heartbeat, price streams, bots) and a Next.js hello page, all with health checks. Quality gates are in place: ruff, mypy strict, pytest, ESLint, Prettier and gitleaks, all run by pre-commit and again by GitHub Actions CI on every push.

**Work queue:** `AI_DOCS/github-issues.md` until the issues exist on GitHub, then GitHub. Done: planning docs, P0-T1, P0-T2, P0-T3, P0-T4, P0-T5, P1-1, P1-2, P1-3, P1-4, P2-1, P2-2, P2-3, P2-4, P2-5, P2-6, P3-1, P3-2, P3-3, P3-3b, P4-1. Next: finish P3-4 (the 24-hour run, D37), and P4-2 (stops). Branches: `<issue#>-<short-slug>`. Commit titles: `#<issue#> <title>` (e.g. `#1 Switch first exchange to MEXC; mark P0-T1 done`), every commit, taking the issue number from the branch name.

## Commands
Only list commands that exist. First run: copy `.env.example` to `.env` and set a password.
- `docker compose up --build -d`: build and start all five services; `docker compose ps -a` should show each one `(healthy)`, plus `migrate` as `Exited (0)` (it applies database migrations, then stops). After adding a migration use this, not `docker compose build engine`: `migrate` has its own image, and a stale one skips the new migration
- `docker compose run --rm migrate`: apply database migrations by hand (`up` already does this)
- `docker compose run --rm engine python -m engine.bots.create btc-ema ema_crossover BTC/USDT 1h --params '{"fast": 9, "slow": 21}' --cash 1000`: create a running paper bot with that strategy config and paper cash in the quote currency (checks the params and that the exchange lists the symbol). `--sizing '{"mode": "percent_of_equity", "percent": 25}'` sets how much each buy spends (default all-in; also `{"mode": "fixed", "amount": 100}` and `{"mode": "atr_risk", "risk_percent": 1, "period": 14, "multiple": 2}`). The engine loads bots when it starts, so then `docker compose restart engine`. No stop command until P5: `docker compose exec db psql -U crypto -d crypto_trading -c "UPDATE bots SET status = 'stopped' WHERE name = 'btc-ema'"`, then restart the engine
- `docker compose run --rm engine python -m engine.marketdata.backfill BTC/USDT 1h 4h --days 365`: download candles and fill gaps (exit 1 if gaps remain); add `--check` to only report gaps. MEXC keeps ~30 days of 1m and ~360 days of 5m/15m, so use `--days 30` for 1m
- `docker compose run --rm engine python -m engine.backtest.run ema_crossover BTC/USDT 1h --days 360`: backtest on stored candles, save the run to `backtest_runs` and print its metrics. The range ends at the last stored candle; warm-up comes from before it, so `--days` must leave room for it. Options: `--params '{"fast": 9, "slow": 21}'`, `--fee-rate`, `--slippage`, `--cash`, `--sizing` (as for `bots.create`; `sweep` and `walk_forward` take it too). The other strategies are `rsi_mean_reversion` (params `period`, `oversold`, `overbought`) and `bollinger_breakout` (`period`, `width`, `volume_factor`). After changing engine code, run `docker compose build engine` first, because `run` uses the built image
- `docker compose run --rm engine python -m engine.backtest.sweep ema_crossover BTC/USDT 1h --grid '{"fast": [5, 9, 12], "slow": [21, 30, 50]}' --days 90`: backtest every combination in the grid over the same range, print them ranked by Sharpe (plus combinations the params refuse, like fast ≥ slow), and store the winner. Takes `run`'s cost options. In Windows PowerShell 5.1, escape the JSON's inner quotes as `\"` (also for `--params`)
- `docker compose run --rm engine python -m engine.backtest.walk_forward ema_crossover BTC/USDT 1h --grid '...' --days 360 --train-days 90 --test-days 30`: pick the grid's best set on each 90-day window, test it on the 30 days after, roll forward; prints in-sample vs. out-of-sample per window and stores both runs of each. Needs warm-up candles before the first window, so keep `--days` a little under what's stored
- `docker compose run --rm engine python -m engine.backtest.report <run_id>`: print a stored run's metrics. `docker compose run --rm -T engine python -m engine.backtest.report <run_id> --equity-csv | Set-Content -Encoding utf8 equity.csv` saves its equity curve (ts, equity, drawdown) for a spreadsheet. `-T` stops Docker adding `\r` to lines; in Windows PowerShell 5.1, `>` would write UTF-16
- `docker compose run --rm -v ${PWD}/engine/tests/fixtures:/fixtures engine python -m engine.marketdata.record_live BTC/USDT 1m --candles 180 --out /fixtures/mexc/live_btcusdt_1m.json.gz`: re-record the parity test's live session (about 3 hours; `docker compose build engine` first). It runs the real live candle path and prints any candle that differs from a later REST re-fetch (exit 1 if any). Re-record when the parity test says "re-record"
- `uv run alembic -c shared/alembic.ini revision --rev-id 0002 -m "..."`: start a new migration file (then write it by hand)
- `docker compose exec db psql -U crypto -d crypto_trading -c "SELECT b.id, b.name, b.status, pb.currency, pb.free, pb.used, p.amount AS position, p.avg_entry FROM bots b LEFT JOIN paper_balances pb ON pb.bot_id = b.id LEFT JOIN positions p ON p.bot_id = b.id AND p.closed_at IS NULL ORDER BY b.id, pb.currency" -c "SELECT bot_id, client_order_id, side, amount, filled FROM orders WHERE status = 'open' ORDER BY id"`: snapshot every bot's balances, position and open orders, e.g. before and after a restart. `docker compose kill engine` stops it with no clean shutdown (SIGKILL), as a crash would
- `docker compose logs -f engine`: follow the engine's JSON logs
- `docker compose exec redis redis-cli GET "price:mexc:BTC/USDT"`: the latest live price; `redis-cli SUBSCRIBE "prices:mexc:BTC/USDT"` (or `PSUBSCRIBE "prices:*"`) shows every update
- `docker compose down`: stop everything (data stays in the `pgdata`/`redisdata` volumes; `-v` would delete it)
- `uv sync --all-packages`: create or update the local `.venv` for backend, engine and the dev tools (Python 3.12, from `.python-version`)
- `uv run pytest`: run every Python package's tests (offline; the exchange tests replay recorded MEXC responses). Database tests start a throwaway TimescaleDB container (testcontainers), so keep Docker Desktop running; without it they're skipped locally (in CI they fail)
- `uv run python -m engine.exchange.record mexc` (or `binance_testnet`): re-record that exchange's public test fixtures (needs network); then update the values the tests expect
- `uv run ruff check .` / `uv run ruff format .`: lint / format Python
- `uv run mypy`: strict type check of all Python packages and tests
- `uv run pre-commit install`: once per clone, so the hooks run on every commit
- `uv run pre-commit run --all-files`: every check, incl. gitleaks, ESLint and Prettier
- In `frontend/`: `pnpm install`, `pnpm typecheck`, `pnpm lint`, `pnpm format` / `pnpm format:check`

CI: `.github/workflows/ci.yml` runs the same checks (plus a full-history gitleaks scan) on every push; results are on the repo's Actions page.

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
- **RiskManager**: sits between signals and the broker; blocks, shrinks or allows every order.
- **Position sizing**: how much a buy spends (`risk/sizing.py`): all-in, fixed, percent of equity, or ATR risk. Shared by backtests and bots; the RiskManager's limits cap it.
- **ATR (Average True Range)**: how far the price typically moves in one candle, gaps included; bigger means more volatile.
- **Kill switch**: cancels orders, stops all bots, optionally closes positions.
- **Circuit breaker**: automatic pause on stale data, repeated errors or price spikes.
- **Regime**: market state (trending up/down, ranging, high volatility) used for auto-switching.
- **Slippage**: the difference between the expected price and the fill price.
- **Drawdown**: the fall in equity from its peak.
- **Walk-forward**: optimize on one window, test on the next, then roll forward, to catch overfitting.
- **Reconciliation**: on startup, comparing engine records with the exchange and fixing mismatches before trading.
- **Client order ID**: our own unique ID on each order, so retries never duplicate.
