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
- [x] uv project(s) for Python; pnpm for frontend
- [x] ruff, mypy (strict), ESLint, Prettier configured
- [x] pre-commit with ruff, mypy, gitleaks
- [x] pytest with one passing test per Python package

**Done when:** `pre-commit run --all-files` passes, and committing a fake API key is blocked by gitleaks.

### P0-T5 · CI
`infra` · Depends on: P0-T4
**Goal:** checks run on every push.
- [x] GitHub Actions: ruff, mypy, pytest, ESLint, frontend type check (+ Prettier, gitleaks)

**Done when:** a push runs the workflow green, and a deliberate lint error turns it red.

---

## Phase 1 — Exchange connection and market data

### P1-1 · ExchangeAdapter for mexc
`engine` `money` · Depends on: P0-T3
**Goal:** the only gateway to the exchange.
- [x] Async CCXT wrapper: balances, candles, tickers, place/cancel order; sandbox flag
- [x] Decimal conversion at the adapter boundary
- [x] Tests with recorded responses (no network in tests)
- [x] Find out: spot testnet? `watch*` WebSocket support? API key rules (IP lock, trade-only, expiry)? Record answers in PRD §9

**Done when:** tests pass offline, nothing outside the adapter imports `ccxt`, and the three open questions are ticked in the PRD.

### P1-2 · Candle store and backfill
`engine` · Depends on: P1-1
**Goal:** complete price history in the database.
- [x] `candles` hypertable via Alembic
- [x] Downloader for 1m, 5m, 15m, 1h, 4h, 1d; idempotent upserts
- [x] Gap detection and filling

**Done when:** a year of 1h candles for the first pair is stored, and a gap-check query returns no gaps.

### P1-3 · Live price stream
`engine` · Depends on: P1-1
**Goal:** current prices always available in Redis.
- [x] WebSocket stream, or REST polling fallback
- [x] Latest price per symbol in Redis + published on a channel
- [x] Auto-reconnect with exponential backoff

**Done when:** prices update in Redis for one hour without a crash, including after pulling the network for 30 seconds.

### P1-4 · Rate limits and retries
`engine` · Depends on: P1-1
**Goal:** the engine is polite to the exchange and resilient to network errors.
- [x] `enableRateLimit`; retry with backoff on network errors only (never on rejected orders)
- [x] structlog entries for every retry and reconnect

**Done when:** tests show network errors retry and rejected orders don't.

---

## Phase 2 — Strategy engine and backtesting

### P2-1 · Strategy interface + EMA crossover
`strategy` · Depends on: P1-2
**Goal:** a plug-in contract every strategy follows.
- [x] Base class: name, version, pydantic params, `warmup_candles`, `on_candle` → signal
- [x] EMA crossover strategy
- [x] Tests on hand-built price series with known crossovers

**Done when:** EMA crossover emits BUY/SELL exactly at the expected candles in tests.

### P2-2 · Event-driven backtester
`strategy` `money` · Depends on: P2-1
**Goal:** honest backtests using live strategy code.
- [x] Candle replay through the strategy
- [x] Fees, slippage (configurable, higher default for small-cap pairs), minimum order sizes
- [x] `backtest_runs` table storing inputs and results

**Done when:** a backtest's results can be reproduced from its stored inputs, and fees match a hand calculation in a test.

### P2-3 · Backtest report
`strategy` · Depends on: P2-2
**Goal:** the metrics needed to judge a strategy.
- [x] Total return, max drawdown, Sharpe, win rate, profit factor, trade count, equity curve

**Done when:** each metric matches a hand-computed value on a small fixture.

### P2-4 · RSI mean reversion and Bollinger breakout
`strategy` · Depends on: P2-1
**Goal:** strategies for ranging and breakout markets.
- [x] Both strategies with typed params and tests

**Done when:** both pass tests on known series and run through the backtester.

### P2-5 · Parameter sweeps and walk-forward
`strategy` · Depends on: P2-3
**Goal:** catch overfitting before paper trading.
- [x] Grid sweep over params
- [x] Walk-forward: optimize on window N, test on N+1, roll forward

**Done when:** a walk-forward report for one strategy shows in-sample vs. out-of-sample metrics side by side.

### P2-6 · Backtest/live parity test
`strategy` · Depends on: P2-2, P1-3
**Goal:** prove backtest signals equal live signals.
- [x] Record a live candle stream; replay it through the live path and the backtester

**Done when:** both paths produce identical signal lists in CI.

---

## Phase 3 — Paper trading

### P3-1 · Broker interface + PaperBroker
`engine` `money` · Depends on: P2-2
**Goal:** simulated trading with realistic fills.
- [x] `Broker` interface; `PaperBroker` filling at live bid/ask with fees and partial fills
- [x] `orders`, `fills`, `positions`, `paper_balances` tables
- [x] Unique `client_order_id` on every order

**Done when:** tests show fills, fees and balances are correct, and a retried order never creates a duplicate.

### P3-2 · RiskManager stub in the order path
`risk` `money` · Depends on: P3-1
**Goal:** every order passes the RiskManager from day one.
- [x] Pass-through RiskManager that logs every order with inputs
- [x] Brokers can only be reached through it

**Done when:** a test shows that no code path reaches a Broker without the RiskManager.

### P3-3 · Bot runner
`engine` · Depends on: P3-2
**Goal:** active bots run automatically.
- [x] `bots` and `strategy_configs` tables
- [x] Load active bots; run on each closed candle

**Done when:** a seeded paper bot trades on new candles without manual steps.

### P3-3b · Paper fills for orders that cross the book
`engine` `money` · Depends on: P3-3 · Blocks: the 24-hour run in P3-4
**Goal:** a paper order that crosses the book when placed fills the way it would live, not at the worst price it allows, and not on the same liquidity over and over.
Found on the first live paper buy (D36): 0.0122 BTC against 0.000612 BTC at the best ask. One slice filled at the ask (81646.11); the other 19 rested and filled at the order's limit (81686.94, ask + 0.05%), within 2 s. Two parts of D34 cause it:
1. A resting order fills at its own limit. That fits an order that waited for the price to come to it, but this one crossed from the start, so its remainder always pays the full slippage buffer (pessimistic).
2. Each book-ticker update offers the top-of-book size again, even when nothing changed (MEXC re-sends the same bid/ask about every 100 ms). The same 0.000612 BTC filled 20 times (optimistic).
- [x] Decide how the rest of a crossing order fills. Suggested: while it still crosses, fill at the current best ask/bid (taker), never worse than its limit. Record the decision in D34
- [x] Decide when top-of-book size counts again. Suggested: only when the best price or its size changed since the last update that filled against it. Record the decision in D34 (decided: at an unchanged best price only the size beyond what we took there; a new best price resets it)
- [x] Tests first in `matching.py`/`paper.py`, with hand-worked numbers: a crossing remainder fills at the ask, not its limit; an unchanged quote fills nothing more; a changed size or price does; an order that rested from the start still fills at its limit
- [x] Re-run a live paper bot and check its fills against the logged quotes by hand (a 124.8 ETH paper buy through the RiskManager against 4.1 ETH at the ask: 5 fills, each at the ask it met, never more than shown at a price)

**Done when:** the tests pass, and on a live paper buy bigger than the top of book every fill is between the asks seen and the limit, with no two fills against the same unchanged quote.

### P3-4 · Restart recovery
`engine` `money` · Depends on: P3-3
**Goal:** no lost or duplicated positions after a restart.
- [x] Rebuild state from DB on startup (open orders cancelled before prices are fed, `bot.recovered` logs each bot's state, D38)
- [x] Test: kill the engine mid-run, restart, compare state (`test_bot_runner.py`; plus a crash mid-fill in `test_paper_broker.py`)
- [x] Don't trade on missed candles. After the engine was down (PC asleep, network out, a restart), the candle stream delivers every candle it missed at once, and today each would place an order at the current price on a signal that may be hours old. Suggested rule: a bot acts only on a candle that closed less than one timeframe step ago; older ones just fill its warm-up window, logged as `bot.caught_up`. EMA crossover's event exit then misses a cross that happened while down, but the state exits still fire on the newest candle (D31). Record the rule in the decision log
- [x] Test: the engine misses several candles and comes back; each bot acts on the newest only
- [x] By hand: `docker compose kill engine` mid-run, snapshot before and after the restart, compare (bot 4, 2026-10-24: identical; the 2 candles missed while down weren't traded; the first live candle after it sold exactly the recovered position)
- [x] The 24-hour run: bot 4 `btc-ema-1m` (EMA 3/5, BTC/USDT 1m, 1000 paper USDT), from 2026-10-24 01:35 UTC (09:35 Manila) to 2026-10-25 01:35 UTC. Passed:
  - The engine stayed up for the whole run: 0 restarts and one `engine.starting`.
  - There was 1 error, the known race below. Counted anyway: it didn't crash the engine and left the database consistent.
  - 5 WebSocket drops on MEXC's side were each recovered on the first attempt.
  - 1,440 of 1,440 candles were stored and none were missed.
  - 219 orders and 269 fills, every fill at the best price in its quote, inside its limit and size, with a 0.1% fee.
  - Bot 4's fills add up exactly to its balance: 804.2509094530745 USDT and 0 BTC. 109 trades: −190.98 net, of which 194.74 was fees.

**Found during the run:** one `bot.error`, `KeyError: 'buy'`, at 2026-10-24 14:30:02 UTC on bot 4's buy `b4-202610241429-b`. It's a race in `PaperBroker` (`brokers/paper.py`):
- `_open` matched the new order against the quote and stored the result in `self._taken`.
- While `_fill` was waiting on the database, `on_price` handled a new quote whose ask had moved.
- `forget_moved` dropped the buy side from `_taken`. It was right to, because the ask had moved, and `on_price` couldn't see the order, which wasn't committed yet.
- `_fill` then read `self._taken[symbol][side]` for its `taken_at_best` log field and raised.
- The whole placing transaction rolled back: order id 138 never existed, and balances and the position stayed consistent. The bot missed that one buy, its error was logged, and the engine carried on (D36). The only wrong record left is a `broker.order.placed` log line for an order that doesn't exist.

**After the 24-hour run**, in this order:
All steps done 2026-10-25 (the run counts).
1. Check it: `docker compose ps engine` still shows it up since the start, and the logs since then (`docker compose logs engine --since 2026-10-24T01:35:00Z`) have one `engine.starting` at most (the 01:35 start) and no `"level": "error"` lines besides the known one above.
2. Check the trades by hand: each `broker.order.filled` price is between the ask/bid in its logged `quote` and the order's `limit` (D34), and the snapshot query (CLAUDE.md) agrees with the fills.
3. Decide whether the run counts with that error. It didn't crash the engine, and the trades checked out; the stricter option is a shorter re-run after step 4. If it counts, tick the box above, tick P3-4 in ROADMAP, and mark Phase 3 done in CLAUDE.md's status. If anything else failed, note it here and fix that first.
4. [x] Fix the race (2026-10-25). Two tests in `test_paper_broker.py` (Concurrency) both failed before the fix:
   - an `on_price` with a moved ask runs while `_open`'s fill is waiting on the database; this reproduced the `KeyError`;
   - a rolled-back order leaves no order, balance, record or taking state behind.

   What changed:
   - `_fill` now gets `taken` from its own match's result instead of reading `self._taken` after an `await`.
   - `place_order` logs `broker.order.rolled_back` and drops the ID from `_taking` when its transaction fails, so a retry of that ID doesn't inherit the failed try's taking.
   - `_open` still claims its size in `self._taken` before the commit, not after as first suggested: claiming it after would let `on_price` spend the same size in between (the double fill P3-3b fixed). A rollback then undercounts fills until the price moves, which is the safe side (D34).
5. [x] Apply the waiting migrations (0005 from P4-1, 0006 from P4-2a): `docker compose up --build -d`, then `docker compose exec db psql -U crypto -d crypto_trading -tAc "SELECT version_num FROM alembic_version"` shows `0006`. The engine's `bot.recovered` still lists bot 4, now with all-in sizing. (2026-10-25: 0004 → 0005 → 0006 ran; all services healthy; bot 4 `{"mode": "all_in"}`, recovered flat at 803.11 USDT with nothing to cancel.)
6. [x] Try sizing on real candles: `engine.backtest.run ema_crossover BTC/USDT 1h --days 360` with no `--sizing`, then `--sizing '{\"mode\": \"percent_of_equity\", \"percent\": 25}'`, then `--sizing '{\"mode\": \"atr_risk\"}'`. The first matches an earlier all-in run's numbers; the other two buy less per trade. Then try stops: the same run with `--stops '{\"stop_loss\": {\"kind\": \"percent\", \"percent\": 3}}'` prints an `exits:` line, and no trade loses much more than 3% plus fees and slippage, unless a candle gapped through the stop.

   Done 2026-10-25 with EMA 9/21, BTC/USDT 1h, 360 days:
   - Stored run 2 (from before sizing existed) reproduces identically: 386 fills, 435.4473 USDT.
   - The four new runs:

     | Run | Settings | Result | Fees | Notes |
     |---|---|---|---|---|
     | 41 | all-in | 435.45 USDT | 220.23 | the same as run 2 |
     | 42 | 25% of equity | 818.81 USDT | 83.69 | average buy 217 vs 571 |
     | 43 | `atr_risk` | 551.74 USDT | 207.71 | average buy 539 |
     | 44 | 3% stop-loss | 453.38 USDT | 223.87 | `exits: 4 stop_loss` |

   - Run 43 is often capped at the free cash: 1% risk at a 2×ATR stop asks for about all of the equity on 1h.
   - In run 44 every stop exit lost exactly 3.05% (3% plus 0.05% slippage). The worst trade went from −5.89% to −3.05%, and no candle gapped through a stop.
7. [x] Decide whether bot 4 keeps running (stop it as CLAUDE.md says, then restart the engine). Stopped 2026-10-25 at 803.11 USDT, flat, no open orders; the engine now logs `bots.loaded` with `[]`.

**Done when:** both tests pass, and a 24-hour paper run (after P3-3b) of a fast 1m bot completes with no crashes and with trades that check out by hand (D37). Keep the PC from sleeping for the run; running for days is Phase 8's 2-week server run.

---

## Phase 4 — Risk management

### P4-1 · Position sizing
`risk` `money` · Depends on: P3-2
- [x] Fixed amount, percent of equity, ATR-based; respect minimum order size (`risk/sizing.py`, shared by backtests and bots; all-in stays the default, D39)

**Done when:** tests cover each mode, including rounding and minimum-size edge cases.

### P4-2 · Stops
`risk` `money` · Depends on: P4-1. Split in two (D40):

**P4-2a · Stop rules + backtester** (#44)
- [x] Stop-loss, take-profit, trailing stop: percent or ATR distances, R-multiple take-profits (`risk/stops.py`)
- [x] Backtests check each candle's high/low pessimistically; `--stops`; stored per run (migration 0006)

**Done when:** tests trigger each stop on known price paths. ✔

**P4-2b · Stops for paper bots** (#48)
- [x] `bots.stops` (migration 0007) and `bots.create --stops`
- [x] Levels placed when a buy fills, from `avg_entry` (again if a later slice moves it), with the settings and ATR the buy recorded in its `reason`. Kept on `positions` (`stop`, `take_profit`, plus `stop_loss`, `peak`, `entry_atr`, `levels_entry`), so a restart keeps them. The trailing stop follows the bid, never lowered
- [x] `BotRunner.on_price`, after each update's fills (`PaperDesk.feed_prices(then=...)`): bid ≤ stop or ≥ take-profit cancels the bot's open orders and sells the whole position through the RiskManager, as a marketable limit. Its ID is `<buy id minus b>x<n>` (`b4-202610241429-x1`, `-x2` after a cancel). Once fired, it sells until the position is gone; an exit the bid fell below is replaced; never two at once
- [x] Around the strategy's own orders: the candle's leftover cancel also cancels an exit's remainder, then a SELL sells what's left (or HOLD, and the next price sells it). A bot's candle and its prices take turns (one lock per bot)
- [x] Compare a paper bot's stop exits with a backtest over the same candles (below)

**Done when:** tests show each stop selling a paper position on a known price path, a restart keeps the levels, and a stopped paper run's exits check out by hand against the logged prices. ✔

Tests: `test_stops.py` (`exit_at_price`, `after_price`, `reentry`), `test_bot_exits.py` (`on_bid`, exit IDs), `test_bot_orders.py` (stops before a buy, `exit_order`), `test_bot_runner.py` § Stops (one price path each: stop-loss, trailing, take-profit, a replaced exit, a buy cancelled first, exit + SELL on one candle, a fired stop after the candle's cancel, restart, a later buy fill, ATR stop, a dust exit tried once a candle, no stops), `test_risk_paper.py` (`then` after the fills).

**The paper run** (2026-10-25 and 26, before merging; the code was the branch's, on migration 0007):
1. [x] Apply migration 0007: `docker compose up --build -d`, then `docker compose exec db psql -U crypto -d crypto_trading -tAc "SELECT version_num FROM alembic_version"` shows `0007`.
2. [x] Create a fast bot with tight stops, so a few hours give several exits: `docker compose run --rm engine python -m engine.bots.create btc-stops ema_crossover BTC/USDT 1m --params '{\"fast\": 9, \"slow\": 21}' --cash 1000 --stops '{\"stop_loss\": {\"kind\": \"percent\", \"percent\": 0.3}, \"take_profit\": {\"kind\": \"r\", \"multiple\": 2}, \"trailing\": {\"kind\": \"percent\", \"percent\": 0.2}}'`, then `docker compose restart engine`. Keep the PC awake.
   - Bot 5 `btc-stops` (9/21) never traded: its cross up came before it started, and entries are events (D31). It was stopped.
   - Bot 6 `btc-stops-5-13` (EMA 5/13, the same stops) ran from 2026-10-25 12:03 UTC.
   - Bot 7 `btc-stops-9-21` (9/21) joined at 15:50. It was added because 5/13's own SELL kept beating the trailing stop. A 7-day backtest put 9/21's stop exits at 31% of its exits, against 15% for 5/13.
   - The PC was shut down from 16:10 to 02:49 UTC the next day, with both bots flat. The night's candles were backfilled, and each part was compared on its own.
3. [x] Once it holds a position, restart the engine (`docker compose kill engine`, then `docker compose up -d engine`). `bot.recovered` shows the levels, and the position still exits on them afterwards.
   - Killed at 12:58:45 UTC while bot 6 held 0.01241318 BTC at 80438.78.
   - `bot.recovered` logged the same levels, and the `positions` row was identical before and after: stop 80299.42930, take-profit 80921.41268, stop-loss 80197.46366, peak 80460.35.
   - The trailing stop kept rising after the restart. The strategy's SELL closed the position at 13:05, above the stop.
4. [x] After a few hours, stop the bot (CLAUDE.md) and restart the engine. Check each `bot.exit` by hand:
   - its `bid` is ≤ its `stop` (`stop_loss`/`trailing_stop`), or ≥ its `take_profit`;
   - its fills (`broker.order.filled` for that client order ID) are between the bid in their logged `quote` and the exit's `limit`;
   - the levels in `bot.levels_placed` match the entry (0.3% below it, and 2 × 0.3% above);
   - each `bot.stop_raised` is 0.2% below the bid it logs.

   Four exits, all `trailing_stop` (with these settings the trailing stop starts above the stop-loss, so a `stop_loss` exit can't happen):

   | Time (UTC) | Bots | Peak bid | Stop | Bid | Fills | Limit |
   |---|---|---|---|---|---|---|
   | 2026-10-26 03:17:55 | 6, 7 | 79831.41 | 79671.74718 | 79669.95 | all at 79669.95 | 79630.11 |
   | 2026-10-26 04:22:39 | 6, 7 | 80075.53 | 79915.37894 | 79914.84 | all at 79914.84 | 79874.88 |

   - Each stop is 0.2% below its peak, and each limit is the bid × (1 − 0.05%), rounded down.
   - At 03:17:55 the bid showed only 0.001957 BTC. Bot 6 took all of it and bot 7 none. The next two quotes (0.002279, then 0.05107949, at the same bid) filled the rest of both orders. `taken_at_best` came to 0.02491404, which is both positions, so nothing filled twice (D34).
   - A script checked every `bot.levels_placed` (19) and `bot.stop_raised` (785) in the engine's log against these rules: 0 problems.
   - Four of the 19 were levels placed again while bot 6's 03:01 buy filled in slices, each from the new `avg_entry` and keeping the peak.
   - No `bot.error`, `bot.exit_skipped`, `bot.levels_failed` or `paper.feed.listener_error`.
   - Bot 7 was stopped at 04:32 UTC. Bot 6 held a position then, so it kept running until flat (a stopped bot's position isn't watched, D40). Its last trade closed by the strategy's SELL at 04:43, above the stop. It was stopped at 04:44, at 980.50 USDT. Bot 7 ended at 994.74 USDT. The engine now logs `bots.loaded` with `[]`.
5. [x] Compare with a backtest over the same candles: `docker compose run --rm engine python -m engine.backtest.run ema_crossover BTC/USDT 1m --days 1 --params '{\"fast\": 9, \"slow\": 21}' --stops '<the same>'`. For the trades in the bot's hours, match exits by time and kind. Note where they differ, and why (the bid path vs. the candle's high and low, D40). Over ~20% apart → revisit the candle assumptions (D40).
   - Bot 6 on 2026-10-25, 12:38–16:10, against run 45: 7 trades, all closed by the strategy's SELL. The backtest had the same buys and sells in the same minutes, and no stop exits either. The closest call was 17 USDT above the stop.
   - 2026-10-26 from 03:02, against runs 49 (5/13) and 50 (9/21): the same trades in the same minutes. Bot 6: 4 round trips, 2 `trailing_stop`. Bot 7: 3 round trips, 2 `trailing_stop`. The backtest's 02:48–02:50 trade came before the engine's first candle that day, so it's left out.
   - Paper's stop fills were 36–39 USDT better. The backtest fills a stop at the stop minus 0.05% slippage, while paper took the live bid.
   - The backtest's first stop sat about 2 USDT higher, because it trails the candle's high (79833.79) rather than the bid (79831.41), as D40 expects.
   - Strategy SELLs and buys were also about 40 USDT better in paper: the same slippage assumption. So backtests here are on the cautious side.
6. [x] Tick the box above and ROADMAP's Stops, and note the result here.

   Not a stop result, but worth knowing: these 1m EMA bots lose money. Over 7 days the backtests lost 69% (5/13) and 51% (9/21), with fees of 458 and 332 USDT on 1000. A round trip costs about 0.3% in fees and slippage, more than a 0.2% trailing stop can save. They were only here to exercise the stops.

### P4-3 · Limits
`risk` `money` · Depends on: P4-1
- [x] Max open positions, max exposure per coin, max daily loss (resets at Asia/Manila midnight), max drawdown
- [x] `risk_settings` and `risk_events` tables
- [x] Never block a sell or a stop's exit (`reason.exit`): they only lower exposure (D40)

**Done when:** a test per limit shows it blocks orders, and every block is written to `risk_events`.

Done (D41): `test_each_limit_blocks_a_buy_and_records_it` (test_risk_manager.py) blocks a buy with each limit and checks its `risk_events` row. `test_limits.py` covers each rule's boundary, the resize and the daily-loss latch, and `test_a_limit_blocks_the_next_buy_but_never_the_stops_exit` (test_bot_runner.py) runs it end to end. The limits are account-wide and a buy over the coin cap is shrunk to fit. A retried client order ID gets its first answer (D35's open question).

### P4-4 · Circuit breakers
`risk` · Depends on: P4-3
- [x] Stale data (> 2 candles), 3 failed orders in 10 min, price spike

**Done when:** tests show each breaker pauses trading and logs why.

Done (D42):
- `test_each_breaker_pauses_buys_and_records_why` (test_risk_manager.py) blocks a buy with each breaker and checks its `risk_events` row and `risk.decision` log. The breakers are a paused bot, a stopped bot, stale data and a price spike.
- `test_three_failed_orders_in_ten_minutes_pause_the_bot` checks the pause, its `risk.breaker.tripped` log, the blocked buy after it, a sell still going through, and resuming. Its database twin checks that the pause in `bots` survives a restarted RiskManager.
- `test_breakers.py` covers each rule's boundary, `PriceSwings` and `Failures`.
- `test_a_price_spike_blocks_the_next_buy_but_never_the_stops_exit` (test_bot_runner.py) runs it end to end.

The breakers block buys only. Settings are in `risk_settings` (migration 0009). Paused bots still load and keep their stops.

### P4-5 · Kill switch (engine)
`risk` `money` · Depends on: P4-3
- [x] Redis command channel; cancel orders, stop bots, optional close positions
- [x] Works with the API container stopped

**Done when:** an integration test sends the command and every bot is stopped within seconds.

Done (D43):
- `test_a_kill_over_redis_stops_every_bot_within_seconds` (test_kill_switch.py) sends the kill over Redis, as the CLI does, to three bots: one holding a position with an open buy, one with an open buy, and a paused one. Within 5 s the reply arrives (`took_ms` < 2000), every bot is `stopped` with its `stopped_reason`, no order is open, and the position is kept. After that, neither the next candle nor a bid below the stop places an order, and the RiskManager blocks a stopped bot's buy.
- The other tests in test_kill_switch.py cover:
  - closing positions: selling until flat, carrying on a fired stop's exit numbering, and placing the exit again when the bid falls below it;
  - a restart while closing;
  - a position with no fresh price, and one it can't sell;
  - a buy under way when the kill comes;
  - a kill queued while the engine was down, drained before any bot loads;
  - bad and failing commands, and the CLI (both its reply and its timeout);
  - waiting past redis-py's 5 s read timeout.
- `next_exit` (test_bot_exits.py) is the rule shared with the stops.
- On the stack (2026-10-26), with `docker compose stop api`, two 1m test bots (8, 9) were running and bot 8 held 0.00124536 BTC. `python -m engine.control.kill --close-positions` replied `ok` in 197 ms. Both bots were stopped with the reason recorded, the position sold in one fill (`b8-202610260755-x1`), and no order was left open. A restart then logged `bots.loaded` `[]`.
- The first try on the stack found a bug the tests had missed: redis-py's default 5 s read timeout broke every idle 5 s `BRPOP`, so the listener backed off and ran the kill 25 s late. Fixed: every blocking pop waits 1 s and loops (D43). Two tests now idle past 5 s.
- Not done: tripping the kill switch automatically on a drawdown breach (left manual, D43).

---

## Phase 5 — API and dashboard

### P5-1 · Auth
`backend` `frontend` `security` · Depends on: P0-T2
- [x] `users` table; argon2 password; TOTP enrolment and check
- [x] JWT in httpOnly secure cookie; auth on every REST/WS route
- [x] Login page

**Done when:** a Playwright test logs in with password + TOTP, and API tests show every route rejects unauthenticated calls.

Done (D44):
- `e2e/login.spec.ts` (`pnpm e2e`, against a throwaway Compose project) does the following, with a TOTP code worked out in the test:
  - A visit to `/` with no session lands on `/login?next=%2F`.
  - A wrong code shows the error.
  - The right password and code land on `/` with "Signed in as e2e".
  - The cookie is HttpOnly, Secure and SameSite=Strict, and not in `document.cookie`.
  - After signing out, the old cookie gets a 401.
  - The API answers 401 through `/api` with no session.
- `test_every_route_needs_auth.py` calls every route the app has, WebSockets included, with no session and requires a 401 (or a close with 1008); `PUBLIC` must be exactly `/health` and `/auth/login`.
- `test_auth_routes.py` covers:
  - the cookie's attributes;
  - one answer for every wrong part;
  - a code used twice;
  - the lockout and its reset;
  - expiry;
  - forged, stale and unknown tokens;
  - logout ending every session.
- `test_users_cli.py` covers enrolment (QR, encrypted secret, confirming code), the single-user rule, bad passwords, the e2e flags, and the resets and unlock. `test_auth_helpers.py` covers the pure parts.
- Found building it: a fresh stack's `migrate` failed because the `db` health check passed on the image's socket-only init server; it now checks over TCP.
- Checked by hand in Chrome (2026-10-26, a throwaway stack on port 3100):
  - The dark theme renders, and focus starts in Username.
  - Sign in stays disabled until every field is filled.
  - A wrong code shows the error and outlines Code in red.
  - The right code signs in and returns to `next`, with no console errors.
  - Sign out and a revisit both land on `/login`.
- It found two things:
  - `--password-stdin` kept the byte-order mark PowerShell pipes first (UTF-8 output encoding), giving a password nobody could type. It's stripped now (`test_a_byte_order_mark_from_a_powershell_pipe_isnt_part_of_the_password`).
  - After a failed sign-in, focus now returns to Code (checked in `login.spec.ts`).
- On the dev stack (2026-10-26):
  - The API answers 401 on `/auth/me` and `/docs`, and 200 on `/health`.
  - `/` redirects to `/login?next=%2F`.
  - Migration 0011 is applied, with no user yet: the owner creates theirs with `backend.users create`.

### P5-2 · App shell, Overview, kill switch
`frontend` `backend` · Depends on: P5-1, P4-5
- [x] WebSocket feed for equity, P&L, positions, bot status, heartbeat
- [x] Header with mode badge, freshness, kill switch (UX1, UX2, UX5)

**Done when:** a Playwright test triggers the kill switch from the UI and the engine stops all bots.

Done (D45):
- `e2e/kill-switch.spec.ts` (`pnpm e2e`): global setup creates the running paper bot `e2e-bot` and starts the engine (the e2e stack has one now). The test signs in and checks the header shows PAPER and a fresh engine, and the Overview shows `e2e-bot` running. It opens the kill switch and checks focus is on Cancel, then clicks "Stop all bots". The reply ("Stopped 1 bot: e2e-bot") comes within 2 s of the click (DESIGN §9), and the live table then shows the bot `stopped` with "kill switch from dashboard". It took 3.1 s in all; the login test passes too.
- API tests:
  - `test_overview.py` checks the account against the RiskManager's numbers (the same account as `test_risk_account.py`), positions at the bid and with no price, every bot with why it's paused or stopped, the heartbeat and streamed prices, and an empty account.
  - It also checks the WebSocket resends with a new bid, and closes with 1008 after logout.
  - `test_kill_route.py`: the command reaches the engine (source `dashboard`, the flag, the time) and its reply comes back; no reply means 504 with the command left queued; unknown fields get 422.
  - `test_every_route_needs_auth.py` now requires `GET /overview`, `WS /ws/overview` and `POST /engine/kill`.
- `shared/tests/test_account.py` covers the moved account math (written first). `engine/tests/test_risk_account.py` and `test_risk_manager.py` pass unchanged. `test_heartbeat.py` checks the Redis beat, and that the file still beats without Redis.
- Checked by hand in Chrome (2026-10-26, a throwaway stack on port 3100):
  - Tab and Enter open the dialog, with focus on Cancel.
  - "Stop all + close positions" answered in 59 ms, and the bot showed `stopped` with the command.
  - The light theme survives a reload.
  - With the engine stopped, Engine and Prices turn STALE (icon and word) after ~35 s, and the kill switch says the command is queued after 10 s.
  - No console errors.
- Found: shadcn's destructive button fades to 60% red in dark mode; the kill switch now sets full red in both themes. The shadcn CLI resolved `@/lib/utils` to an npm package called `cn` and added it; reverted, and the components were written in the project's style instead.

### P5-3 · Market chart
`frontend` · Depends on: P5-2
- [x] Lightweight Charts candles, indicators, trade markers
- [x] Trade explanation panel (UX9)

**Done when:** live candles update on screen and clicking a trade marker shows its signal and risk decision.

Done (D46):
- `e2e/market.spec.ts` (`pnpm e2e`): one filled buy is written for `e2e-bot` in its newest stored candle. The test opens Market from the nav and waits for the forming candle to move (the chart's `data-last-bar`, since a canvas can't be read). It clicks the marker where the chart reports it drew it, and checks the panel's Signal section shows the signal's reason and its Risk check section shows "allowed". Escape closes the panel; the trades table's row opens it again from the keyboard. It runs as its own Playwright project before the others, as the kill switch test stops the bot. All 4 e2e tests pass (2026-10-26).
- `shared/tests/test_chart.py`: each strategy's lines, each point over its own warm-up window, none while warming up or across a gap. `engine/tests/test_chart_parity.py`: for every registered strategy, the chart's lines equal its signals' `indicators` at every candle.
- `test_candle_stream.py`: the `on_kline` hook gets every update's newest candle; a failing hook is logged once and never makes the stream reconnect; a hung one is given up on. `test_redis_sink.py` checks the forming candle in Redis.
- `backend/tests/test_market.py`: candles with warm-up-backed lines, the forming candle only while newer, trades (signal, stop-loss, kill switch; unfilled orders and other bots' left out), 404/422, explanations (signal, resize with its event, exits, an old reason that doesn't fit), and the WebSocket (new candle with its point, forming, trades on change, 4404 for an unknown bot, 1008 after sign-out). `test_every_route_needs_auth.py` requires the three new routes.
- Found running it: each Playwright project runs in its own worker, so `freshCode` couldn't remember the TOTP step another worker used, and the next sign-in reused it. It now reads `users.totp_last_step` from the e2e database.
- Checked by hand in Chrome (2026-10-27, a throwaway stack on port 3100: `e2e-bot` with EMA crossover and `rsi-bot` with RSI, both on BTC/USDT 1h, 5,280 backfilled candles, three seeded trades):
  - The chart draws both EMAs, volume, the forming candle (moving every second; "Live: forming candle 0 s ago") and the B and S SL markers. Its axis and crosshair read Manila time.
  - The legend follows the cursor: OHLC and each line's value.
  - A marker click opens the panel: the signal's reason and indicator values, `allowed`, the strategy and sizing; for the stop-loss exit, the bid, levels and fill.
  - rsi-bot's resized buy shows the requested 0.0089 BTC against the placed 0.003 BTC, and the account it was checked against.
  - The keyboard path: Tab goes from the bot picker, past TradingView's attribution link, to the newest trade; Enter opens it, Escape closes it.
  - Dark and light themes re-color everything without a reload.
  - With 5,000 candles (`?limit=5000`), the API answered in 240 ms (615 ms with RSI's 85-candle windows), and 2.4 s of scripted zooming and panning ran at 60 fps with no long tasks (DESIGN §9).
  - With the engine stopped, the chart said "stale: the forming candle last moved 41 s ago", and the header turned STALE.
  - No console errors.
- It found three things:
  - Closing the panel left focus on `<body>`. The panel has no Radix Trigger (a marker or a row opens it), and Radix only returns focus to a Trigger. It now returns focus to whatever opened it, and `market.spec.ts` checks the row is focused again.
  - The RSI pane was squashed to about 12% of the chart. The panes are now set to 2:1.
  - The page always loaded 1,000 candles, so the 5,000-candle budget couldn't be tried. `?limit=` (1 to 5,000) now passes through to the API.

### P5-4 · Bots page
`frontend` `backend` · Depends on: P5-2, P3-3
- [x] Create, start, pause, stop via engine commands
- [x] Params form generated from the strategy schema

**Done when:** a Playwright test creates and starts a paper bot, then stops it.

Done (D47):
- `e2e/bots.spec.ts` (`pnpm e2e`): signs in, opens Bots from the nav, and opens New bot: the EMA crossover's params show their defaults (9, 21). With fast 30 the engine refuses ("fast (30) must be shorter than slow (21)") and the sheet shows it. With fast 9, a 3% stop-loss and ETH/USDT (a symbol nothing else trades), `e2e-eth` is created stopped with 1,000 USDT. Start: running at once, then "in the engine" once its heartbeat lists it, and ETH/USDT is among the streamed symbols. Stop, from the dialog (focus on Cancel): stopped, "stopped from dashboard". 5.7 s. The kill switch and login tests pass after it (2026-10-27).
- `engine/tests/test_bot_control.py`: create (stopped, funded, sizing/stops/config stored; nine kinds of refusal, a name in use), start without a restart (twice is once), an unknown bot or a stale strategy version refused, pause blocks buys (`bot_paused`) and start resumes, stop cancels its orders and keeps its position (and the bot never acts again), stop + close sells until flat (start refused meanwhile), a restart carries on a close, over Redis, an expired command dropped unanswered, a start drained at startup, a listener without bot control.
- `test_bot_runner.py`: a bot started on a streaming series joins with a whole window (REST lacked the newest candle; the series gave it) and both bots buy on the next cross; one on a new series starts its stream (with zero bots at first) and reports its symbol; one stopped while warming up never joins; a stopped bot started again starts its ended series anew. Two tests now stop `run()` themselves, as it stays up.
- `test_bot_catalog.py` (every strategy, sizing mode and stop kind; each default builds), `shared/tests/test_catalog.py`, `shared/tests/test_commands.py`; `backend/tests/test_bots.py` (each route's command, 422/504/503, the list); `test_every_route_needs_auth.py` requires the six new routes. All 827 Python tests pass.
- Found: `e2e/market.spec.ts` (P5-3) failed, on the base commit (`ef265f5`) too: the click landed on the B marker, but nothing opened. Lightweight Charts hit-tests the topmost series first, and the EMA lines are drawn above the candles; that day the slow EMA ran a few pixels from the marker, so the click reported the line, not the marker. Anyone clicking a marker under a line had the same problem. `CandleChart` now looks for a marker within the arrow's size of the click itself when the chart reports something else; the test is unchanged and passes. All 5 e2e tests pass (2026-10-27).
- Checked by hand in Chrome (2026-10-27, the e2e stack on port 3100, live MEXC prices):
  - New bot: RSI's params, ATR-risk sizing and an ATR stop-loss (with `atr_period`) drawn from the catalog; hints say what's wrong as you type ("above 0", "required"); Create stays disabled until it's all valid. `NOPE/USDT` is refused by the engine ("mexc does not have market symbol NOPE/USDT"); `rsi-hand` on SOL/USDT is created stopped in 65 ms.
  - Start: `rsi-hand` (a new symbol: its price stream and feed started, 85 warm-up candles) and `e2e-bot` (its series had ended at the kill: started anew, 64 candles) answer in ~40 ms, show "warming up…", then "in the engine".
  - Pause and Start again; Stop + close on `e2e-bot` with a seeded 0.001 BTC position, from the keyboard (focus on Cancel, Tab, Enter): exit `e2e-chart-x1` of kind `close` filled at the bid, the position closed (+0.0071 USDT realized, net of the fee). The chart shows its "S CLOSE" marker, and a click on it opens the panel ("from a close from the Bots page"; the sell is `reduces_exposure`, never blocked).
  - With the engine stopped, Start answers the 504 message after 10 s; when the engine is back, the start is dropped (`command.expired`, 23 s old) and the bot stays stopped.
  - Light theme; no console errors.
- It found four things, fixed:
  - The long "why" (the kill switch's command id) ran under the action buttons, and the table scrolled sideways: the text columns now wrap.
  - Just after a command, the row showed the old status (and buttons) for up to a second, as the feed's snapshot was older than the answer: the engine's answer now wins for 3 s.
  - After Pause, or a Stop from the dialog, focus fell to `<body>` (the button that sent it is replaced): the result message takes focus, and is read out.
  - The close's label read badly in the panel ("from a stopped and closed from the Bots page") and its command was called "Kill switch command": now "close from the Bots page" and "Command".

### P5-5 · Backtests page
`frontend` `backend` · Depends on: P5-1, P2-3
- [x] Run form; results with metrics, equity curve and trades

**Done when:** a backtest run from the UI shows the same metrics as the CLI run with the same inputs.

Done (D48):
- `e2e/backtests.spec.ts` (`pnpm e2e`): global setup backfills 120 days of BTC/USDT 4h, which no bot writes. The test runs EMA crossover (defaults) over 60 days from the Backtests page, waits for the run's page, and then runs `engine.backtest.run ema_crossover BTC/USDT 4h --days 60`. The CLI's printed return, max drawdown, Sharpe, trades, win rate and profit factor equal the page's values at the CLI's precision. In the database, both runs have the same range, candle hash, final equity and result (md5 of the JSON). 18 s. All 6 e2e tests pass (2026-10-27).
- `engine/tests/test_backtest_worker.py`: a job stores a run whose config, candle hash and result equal the CLI's path with the same arguments. The job goes queued → running → done, and its record expires. Eight kinds of refusal (params, strategy, timeframe, sizing, stops, symbol format, no candles, too little warm-up) and a symbol the exchange doesn't list fail with the reason, storing nothing. A command that doesn't parse is dropped.
- The metrics moved to `shared/backtest.py`. `test_backtest_report.py`'s hand calculations pass unchanged through the engine's `report()`. New: a stored result reads back with the same report without the engine, the shared literals equal the engine's, and a trade says which stop sold it. `shared/tests/test_backtest.py` reads a stored result.
- `backend/tests/test_backtests.py`:
  - a run is queued as a job with every field; a bad body is a 422 and queues nothing;
  - the job reads back as it moves on; an unknown one is a 404;
  - the list is newest first with `metrics()`'s numbers, and pages back;
  - a run shows trades with their exits, drawdowns per point and skipped orders; an unknown one is a 404, and one whose result doesn't read is shown without metrics;
  - the candle range.
  - `test_every_route_needs_auth.py` requires the five new routes. All 855 Python tests pass.
- Checked by hand in Chrome (2026-10-27, the e2e stack on port 3100, 119 days of BTC/USDT 1h):
  - From the keyboard: Enter in the form with the default 365 days was refused ("needs 64 warm-up candles before start (2025-10-27 10:00 UTC), has 0"), with focus on the alert. With 90 days, a 3% stop-loss and a 2R take-profit, it showed "Queued… / Running…" and opened run #1 in about 3 s.
  - Run #1: −12.25%, 13.83% max drawdown, Sharpe −2.20, 51 trades (3 sold by the take-profit), 21.6% won, profit factor 0.63, 98.08 USDT fees. `engine.backtest.report 1` printed the same numbers.
  - The equity legend follows the cursor; the drawdown hangs from 0% in its own pane; dark and light themes re-color the chart without a reload.
  - With the engine stopped, an RSI run still ran (2.9 s), its form drawn from the backtester's catalog.
  - With the backtester stopped, a run said "Queued, 11 s: is the backtester running?", then ran 4.7 s after the backtester started.
  - The kill switch answered in 23 ms (10 ms in the engine) while a backtest ran.
  - The runs list shows all four runs, newest first. No console errors.
- It found two things, fixed:
  - Fill prices showed every digit of their slippage ("64,341.784815 USDT"): prices of 1 or more now show 2 decimals (smaller ones up to 8), the exact value on hover.
  - The button read "Running…" while the job was only queued: it says "Queued…" until it runs.
- The owner found two more, fixed:
  - In dark mode a dropdown's list was white with white text (every `NativeSelect`, the Bots page's too): Chrome drew it light because the page never said it was dark. `globals.css` now sets `color-scheme` per theme, and `NativeSelect` gives its options the popover colors.
  - Only a run's `#id` opened it: the whole row does now (the link stays the keyboard's way in).
  - 365 days on 120 days of candles said only "needs 85 warm-up candles … has 0". `build_config()` now checks the history first, for the CLI too, and says how many days fit and how far to backfill ("use at most 367 days, or backfill more (--days 403)"); the job carries `max_days`, and the form offers "Use 367 days" (tested in `test_backtest_worker.py`; checked by hand: run #8).

### P5-6 · History and Logs
`frontend` `backend` · Depends on: P5-2
- [x] Trades/orders with filters and CSV export; logs page

**Done when:** CSV export matches the DB rows for a filtered range.

Done (D49):
- `backend/tests/test_history.py`: two bots, six orders around midnight (one at 23:59:59.999999, one at exactly 00:00), partial fills, fees in two currencies and a fee of 0.00000001. For eleven filter sets, every cell of `orders.csv` and `fills.csv` equals the text Postgres gives for the same rows from its own SQL (NUMERIC's digits, `to_char` UTC times). Also: exact digits (never `1E-8`), blank fees across currencies, an empty range is just the header, paging newest first, and nine bad filters are 422s.
- `e2e/history.spec.ts` (`pnpm e2e`): four of e2e-bot's orders around Manila's midnights. With From and To set to 11 Sep, the table shows B and C only. Export CSV downloads `orders_2026-09-11_to_2026-09-11.csv`, which equals Postgres's own `COPY` of the same range cell for cell; the same for the three fills. A row's time opens the explanation. The Logs page: `bots.loaded` from the engine is listed, "Errors" hides it, and a seeded risk event shows with its detail. All 8 e2e tests pass (2026-10-28).
- `shared/tests/test_logs.py`: INFO+ lines reach the stream with their service; DEBUG, the heartbeat and the mirror's own lines don't; Decimals stay exact; a full buffer drops the oldest and says how many; with Redis down nothing raises, `logs.mirror.failed` is logged once, and the lines ship once it's back; `ship()` flushes when stopped; the stream is capped. `backend/tests/test_logs_api.py`: paging by stream id, every filter, several event prefixes, filtering across scan batches, unreadable lines skipped, risk events filtered and paged. `test_every_route_needs_auth.py` requires the six new routes. All 916 Python tests pass.
- Found running it: the kill switch test timed out at 30 s. The kill had worked; its sign-in had waited most of the 30 s for an unused TOTP step, because the new tests signed in just before. `freshCode` (`e2e/auth.ts`) now adds that wait to the test's time limit.
- Checked by hand in Chrome (2026-10-28, the e2e stack on port 3100):
  - History: Manila times, exact fees (0.06395001 USDT), the Fills tab from the URL's filters, a row's time opening the panel, and Escape returning focus to that button.
  - Logs: lines from the engine and the API, "Decisions" (kill switch, bot commands), and the light theme.
  - Risk events: the inputs open in place.
  - No console errors.
  - The stream: 63 lines in 5 min, ~380 bytes a line (~19 MB at the 50,000 cap).

### P5-7 · Settings
`frontend` `backend` `security` `money` · Depends on: P5-1
- [x] Exchange keys encrypted with Fernet (`exchange_accounts`); never sent back to the browser
- [x] Risk limits editor (raising needs TOTP, UX4); ~~alert preferences~~ moved to P7-1 (D50)

**Done when:** keys are stored encrypted (checked in the DB), and raising a limit without TOTP is rejected by the API.

Done (D50):
- `backend/tests/test_risk_settings.py` (done-when, limits):
  - Loosening any of the nine settings without a code is a 403 that names what loosens, and the row is unchanged. So is turning one off, and an edit that's tighter on one side and looser on the other.
  - The right code saves. The same code again is refused, and so is the code just used to sign in.
  - A wrong code is refused and counts toward the lockout (committed, though the edit isn't). The 5th locks: 429, the right code refused unchecked, and sign-in locked too.
  - Tightening, or turning a limit back on, needs no code. A code sent with a tightening isn't used up. The same values (`10.00` for `10`) write and log nothing. The two modes are separate rows.
  - Bounds, half a pair, NaN, an unknown field or a missing one are 422s, and no 422 echoes its input.
- `backend/tests/test_exchange_accounts.py` (done-when, keys):
  - A stored key's columns are Fernet tokens (`gAAAA…`), not the plaintext, and decrypt to it with `ENCRYPTION_KEY`. No response (add, list, replace, 403, 409, seven kinds of 422) contains the key or secret, and neither do the logs.
  - Adding or replacing needs a code; deleting doesn't. A label in use is a 409 that leaves the code unused.
  - MEXC with `sandbox` is refused; Binance's testnet is allowed.
- `shared/tests/test_risk.py`: which edits loosen, field by field, both ways (the two windows looser when shorter), off and on again, `10` vs `10.00`, and every setting has a direction. `RiskSettings` moved to `shared/risk.py`, and the engine's limit and breaker tests pass unchanged. `test_every_route_needs_auth.py` requires the six new routes.
- `e2e/settings.spec.ts` (`pnpm e2e`):
  - From the nav: lowering max drawdown to 8% saves with no prompt (8 in the database). Raising it to 15% opens the code dialog ("Max drawdown: 8% below the peak → 15% below the peak"), with focus in the code field. A five-minute-old code is refused and nothing is saved; a fresh one saves 15.
  - A MEXC key is saved with a code: listed as "…Q7r5", the typed keys gone from the page's HTML. In the database, both columns are `gAAAA…` tokens with no plaintext in them.
  - The seeded limits are put back afterwards. All 10 e2e tests pass (2026-10-28).
- Checked by hand in Chrome (2026-10-28, the e2e stack on port 3100):
  - Without a session, `PUT /risk-settings/paper` and `GET /exchange-accounts` are 401s. Signed in, a raise with no code is a 403 listing `max_drawdown_pct` 10 → 50. A 300-character secret is a 422 naming `body.api_secret` without echoing it.
  - From the keyboard: drawdown 8 and Enter saves with no prompt, and focus goes to "Saved". Drawdown 12 with the price spike turned off opens the dialog listing all three changes (the pair goes off together), focus in the code field.
  - A five-minute-old code: "wrong or already used code", the field cleared, focus kept, and `failed_logins` 1. A fresh code saves: drawdown 12, spike off.
  - Exchange keys, in dark theme: a made-up MEXC key saved with a code, listed as "…K3y9". The typed values are gone from the page. The row holds `gAAAA…` tokens (140 characters), with no plaintext.
  - Delete: the dialog opens with focus on Cancel; Delete removes the row and focus goes to the message.
  - The Logs page lists every `api.risk_settings.changed`/`.refused` and `api.exchange_account.added` line. Light and dark themes; no console errors.
- It found three things, fixed:
  - **A second save, without the code, on every confirm.** The code dialog sat inside the limits form in the React tree. React events bubble through portals, so its submit also submitted the form: a code-less PUT 30 ms before each coded one. The answers could arrive in either order and reopen the dialog after a save. The dialog now sits outside the form and stops its submit from bubbling. `settings.spec.ts` submits with Enter and requires exactly one PUT per submit; it failed on the old build (an extra code-less PUT after each coded one) and passes now.
  - All seven Off boxes had the same accessible name, and each row's `<legend>` wasn't a fieldset's first child, so it named nothing. Each row is now a labelled group, and each box is named for its setting ("Max drawdown: off").
  - On the Price spike row, the long sentence pushed the Off box onto a line of its own. It now has its own column.

---

## Phase 6 — Strategy switching

### P6-1 · Manual hot-swap
`engine` `money` · Depends on: P5-4
- [x] Change strategy/params live; open-position policy keep / close first / hand over

**Done when:** a paper-mode test switches strategy under each policy and positions stay correct.

**Done** (D51):
- `test_bot_control.py` switches a bot holding 9.0768 BTC from EMA 3/5 to RSI(2) under each policy, against a real database, PaperDesk, kill switch and runner:
  - hand over: RSI's SELL sells the whole position, as config 2; the trade's stop-loss still fires after the switch;
  - keep: RSI's SELL is skipped, EMA's sells it as config 1, then RSI makes the next buy; a restart in between rebuilds the keeper;
  - close first: exits `-x1`, `-x2` of kind `switch` sell it until it's gone while the bot only takes candles in, then it trades again, still running; a restart carries on the sale.
- Also tested: a policy alone (no new config, the keeper dropped in memory and on reload), a stopped bot (the database only), refusals, a keeper whose config no longer builds (handed over), a switch still warming up (the old config acts), and a candle REST already had (never taken twice). Writing the tests found a bug, now fixed: a replaced switch's cancelled warm-up dropped the newer one's bookkeeping.
- `bots.spec.ts` switches `e2e-eth` from the keyboard to RSI with hand over, while it runs, with no engine restart.

### P6-2 · Config history and rollback
`engine` `frontend` · Depends on: P6-1
- [x] Append-only `strategy_configs`; rollback from bot detail

**Done when:** rolling back restores the previous params, and both versions stay in history.

**Done** (D52):
- `test_bot_control.py` (done-when): a running bot switches from EMA 3/5 to RSI(2) (config 2), then rolls back to config 1.
  - The result is config 3: EMA 3/5 with `restored_from` 1, active. Configs 1 and 2 are unchanged.
  - EMA's next BUY names config 3, with no restart. `bot.switch` logs `restored_from`.
- Also tested:
  - Under `keep`, rolling back to the params that bought the open position ends the keeper, after a reload too, and EMA's SELL is placed as config 3.
  - A `close_first` rollback sells the position as exits of kind `switch`, and a second rollback is refused while it sells.
  - A stopped bot changes in the database only. It works over Redis.
  - Refusals: another bot's config, an unknown config, the params it runs now, and a strategy version that moved on.
- `test_bot_store.py`: UPDATE and DELETE on `strategy_configs` raise the trigger's error (migration 0013), and INSERT still works. The tests that used to UPDATE a config (an old version, bad params; `test_market.py`'s unknown strategy) now insert one.
- `test_bots.py`:
  - `POST /bots/{id}/rollback` sends a `RollbackCommand`. A bad policy, an extra field (strategy) or a missing config never reaches the engine, and the engine's refusal is a 422.
  - `GET /bots/{id}` numbers the configs per bot, newest first, with `restored_from` and `active`, and leaves out the other bot's. An unknown bot is a 404.
  - `test_every_route_needs_auth.py` requires the new routes (and `/strategy`, which it had missed).
- `bots.spec.ts`:
  - After the switch to RSI, `e2e-eth`'s name opens its detail page, which lists #2 (running now) and #1.
  - From the keyboard, Roll back on #1 (focus starts on Cancel, the policy starts as the bot's hand over, set to keep) adds #3, EMA 9/21, "rollback to #1", running now. Focus moves to the result message, and all three rows stay.
  - Back on the list: EMA 9/21, "on a switch: keep", still in the engine. All 10 e2e tests pass (2026-10-28).
- It found one thing, fixed: once the dialog closed, Radix gave focus back to its Roll back button, which the reloaded history no longer shows in that place, so the result message lost it. The dialog now keeps focus off the button after a rollback (Cancel still returns it).
- Checked by hand in Chrome (2026-10-28, the main stack after `up --build`, migration 0013 applied):
  - On the database, an UPDATE and a DELETE of a config (each in a rolled-back transaction) are refused by the trigger, and the rows are unchanged.
  - `chrome-switch-test` (stopped, flat, configs #1–#4 alternating EMA 2/3 and RSI 14) is opened from its name on the list. From the keyboard, Roll back on #1 adds #5, EMA 2/3, "running now · rollback to #1". In the database that's row 14 with `restored_from` 10, and the bot points at it.
  - Light and dark themes; no console errors.
- The hand check found two more things, fixed:
  - **The bot's name didn't look like a link.** It only underlined on hover, so the detail page was hard to find. It's underlined all the time now.
  - **A row identical to the running config offered Roll back,** and the engine's refusal named the database id ("config 11") where the page says #2. Such a row now says "same as running now" with no button, and the refusal no longer names an id ("it already runs that config's strategy and params"). `bots.spec.ts` checks the note after the rollback.

### P6-3 · Regime detector
`strategy` · Depends on: P2-1
- [x] ADX, ATR, EMA slope → trending up/down, ranging, high volatility

**Done when:** tests on labeled historical windows give the expected regimes.

**Done** (D53):
- `test_regime_history.py` (done-when): Binance BTC/USDT 1d, 2019-06 to 2023 (`engine.regime.record_history`; MEXC keeps only about a year), default settings. Nine spans labeled from market history, each read mostly as its label:
  - **Up:** Oct–Dec 2020, 11k → 19k (56 of 67 days `trending_up`); Oct–Dec 2023, 28k → 43k (52 of 54).
  - **Down:** Nov 2021–Jan 2022, 65k → 35k (49 of 68 `trending_down`, the rest `unclear`); Apr–May 2022, 46k → 34k.
  - **Ranging:** May–Jul 2020 at 9–10k; Jun–Aug 2023 at 29–31.5k.
  - **High volatility:** the COVID crash (9 of 12 days), China's mining ban (7 of 13), Celsius and 3AC (8 of 8).
  - No trend span ever reads as the opposite trend.
- **Found on the first run:** 7 of 9 passed with no allowance. ADX lags weeks on daily candles. The 2023 range read as the June rally's uptrend until 25 July (31 of 51 days), and the April 2022 slide read as ranging until ADX crossed 25 on 27 April. The owner chose to keep the textbook settings and judge trend and range spans from 2 × `adx_period` (28) candles in, rather than tune ADX to fit; crashes get no allowance. The lag is recorded in D53 for P6-4.
- `test_regime.py`: each rule on a hand-built series:
  - a steady rise, a fall and a tight zigzag;
  - one wide candle after a calm stretch, and high volatility beating an uptrend;
  - the 20–25 dead band and DI vs. slope disagreeing (both `unclear`), and warming up;
  - history before the window changes nothing, a flat market divides by nothing, and the params are checked.
- `shared/tests/test_indicators.py`: `adx()` against a hand-worked period-2 series, a steady rise (ADX 100, −DI 0), a flat market (all 0), lengths and bad inputs.

### P6-4 · Auto-switcher
`engine` `frontend` · Depends on: P6-1, P6-3
- [x] Regime → strategy mapping; minimum hold time
- [x] `strategy_switches` table; switch markers on chart and switch log

**Done when:** a replay shows switches only after the minimum hold, each logged with a reason.

**Done** (D54):
- **The owner's choices:** the mapping is per bot (on its page), Edit and rollback are refused while auto-switching is on, and "stay flat" is a strategy (`stay_flat`).
- `test_auto_switch_replay.py` (done-when): the BTC/USDT 1d 2019–2023 fixture, replayed through `AutoSwitcher.check()` with trend → EMA 9/21, range → RSI, down and volatile → stay flat, a bot starting on EMA.
  - With a hold of 3 and 5, every switch comes after that many identical readings (checked against the detector's own labels), has a reason ("ranging for 3 candles in a row (ADX 18.1, below 20: no trend): switch to rsi_mean_reversion v1 {…}"), and is at least the hold after the last.
  - 57, 41 and 34 switches for a hold of 1, 3 and 5: about 9, 9 and 7 a year. That's the lag/flip trade-off P6-5's note is about.
- `test_regime_switcher.py` (pure, a scripted detector): confirms only after `min_hold` identical readings; `unclear` never confirms and breaks a run; a regime the bot already runs, or two regimes on one config, never switch; warming up and a gap start the readings over; every regime must be mapped (and `unclear` can't be); a mapping that doesn't build is refused.
- `test_bot_control.py` (real database, PaperDesk, kill switch and runner; a scripted detector):
  - A bot on EMA 3/5 reads ranging for 1, 2, then 3 candles: only the third sends the switch. It takes over at the next candle. One `strategy_switches` row: `regime`, ranging, the confirming candle, the reason, the reading with its hold, source `regime`.
  - A crash with `hand_over` switches a holding bot to stay flat, whose SELL sells it whole, as config 2.
  - A restart mid-hold neither switches early nor starts the count over: the third candle after it switches.
  - One at a time: a second confirming candle sends nothing while the first switch is still being made.
  - Edit and rollback are refused while it's on, work once it's off, and write `manual` and `rollback` rows.
  - Turning it on reloads a running bot with no config added; off, the bot stops reading regimes.
  - Refusals: an unknown bot, bad params or strategy in a regime (named), `min_hold` 0, nothing to change.
  - A regime switch is dropped if the settings changed or the bot was stopped before it was made.
- `test_bot_store.py`: a bot loads with its switcher; a mapping whose strategy version moved on loads with auto-switching off (`bot.auto_switch_invalid`); `strategy_switches` refuses UPDATE and DELETE.
- `test_migrations.py`: 0014 records the earlier switches from the config history (a manual one and a rollback, at their configs' times, policy NULL).
- `test_stay_flat.py`; the chart parity test covers `stay_flat` (no lines).
- Backend:
  - `test_bots.py`: `POST /bots/{id}/auto-switch` sends the mapping (version left to the engine), or off. A missing regime, `unclear`, `min_hold` 0, a bad policy or an extra field never reaches the engine. `GET /bots/{id}` has the switches with per-bot numbers.
  - `test_market.py`: the chart's switches start at its first candle; the WebSocket sends them first, then only when they change.
  - `test_every_route_needs_auth.py` requires the new route.
- `bots.spec.ts`:
  - On `e2e-eth`'s page, the switch log lists the manual switch and the rollback.
  - Auto-switching is set up from the keyboard (every regime on EMA 9/21, so a closing candle can't switch it; a min hold of 0 disables Turn on). The mapping and "held for 5 closed candles" show.
  - Edit and Roll back are disabled with the reason; no config is added. Turned off, Edit works again. All 10 e2e tests pass (2026-10-29).
- Checked by hand in Chrome (2026-10-28, the main stack after `up --build`, migration 0014 applied):
  - 0014 recorded `chrome-switch-test`'s four earlier switches (three by hand, one rollback). Its switch log lists them with policy "—".
  - Set up from its page with a hold of 1 and hand over, then started. **A real switch:** on the 23:42 (Manila) BTC/USDT 1m candle the regime read `trending_down` (ADX 26.2, −DI 38.8 above +DI 22.7, the EMA falling). The engine switched the bot from EMA 2/3 to stay flat as config #6, `created_by` regime, and the new config warmed up in 0.3 s.
  - The switch log shows "#5 → #6, regime, Trending down, hand over" with the reason. The Market chart shows "auto → FLAT" above that candle, in both themes, with no console errors.
  - The bot was stopped and auto-switching turned off afterwards.
- The hand check found four things, fixed:
  - In the sheet, each regime's description was pulled up into its heading, overlapping it (the heading had no bottom margin).
  - "1 candles": the page's message and summary, and the engine's reason, now say "1 candle".
  - Stay flat's params read "defaults"; a strategy with nothing to set now reads "none", everywhere params are shown.
  - DESIGN said switch markers stand out by colour. In the neutral theme `primary` is close to the trades' ink, so it now says they're told apart by shape, place and label.
- The e2e test found one more, fixed: after Turn on, Radix gave focus back to the sheet's "Set up" button, which the reloaded page replaces with "Change", so the result message lost it (as P6-2's Roll back did). The sheet now keeps focus off the button after a save; Cancel still returns it.
- Seen, not changed: right after a rebuild, the bot's page answered HTTP 500 for a few seconds while the API container was still starting (Next's `/api` rewrite had nothing to reach). The engine took about 45 s to log its first line after the rebuild; that fits Avast holding new executables.

### P6-5 · Combined auto-switching backtest
`strategy` · Depends on: P6-4
- [x] Backtester supports the switcher as one combined strategy

**Done when:** an auto-switching backtest runs end to end and produces a full report.

**Done** (D55):
- **The owner's choices:** the detector experiments (the note below) move to P6-5b; dashboard runs to P6-5c; the CLI prints baselines, not stored.
- `test_backtest_auto_switch.py` (done-when): the BTC/USDT 1d 2019–2023 fixture, with holds 1/3/5 under `keep` and 3 under `close_first` and `hand_over`.
  - Every switch (candle and target) equals `AutoSwitcher.check()` fed candle by candle, as `test_auto_switch_replay.py` does.
  - Every fill pairs into trades with the config that bought each, the equity curve covers the range, and the report prints.
- On a scripted detector:
  - A switch confirmed at a candle takes over at the next, and its signal fills at the open after.
  - `hand_over`: the new config sells.
  - `keep`: the buyer sells, even through two switches, the new config's signals are skipped with the reason, and the new config trades once flat.
  - `close_first`: the position is sold at the next open (exit `switch`, fee and slippage). A BUY on the confirming candle is bought and then sold at that open, as live does it. A SELL on it sells once. Flat, nothing.
  - A stop-loss still fires after a hand-over. The warm-up counts the detector's window and every mapped config. Without `auto_switch` nothing changes.
- `test_backtest_compare.py`: each distinct config alone equals a plain run of it. Buy-and-hold buys all in at the first open (checked by hand). The table ranks by return.
- `test_backtest_store.py`: a run stores its settings and the detector's version, round-trips exactly and reproduces. It's refused once the detector's version moves on; a plain run stores none.
- `test_backtest_cli.py`: `--auto-switch` stores each config at its version with its defaults. Bad params, an unknown strategy (both naming the regime), an unmapped regime and non-objects are refused.
- `test_backtests.py`: the list and detail carry `auto_switch`, the switches and each trade's `decided_by`. An older run reads with none.
- **Found on the first comparison** (the fixture, from 2019-12-19, a hold of 3; D55): auto-switching lost to every config it switches between. It made −30% (`keep`), −26% (`close_first`) and −5% (`hand_over`) in 5 or 6 trades, against EMA 9/21 alone +641% (24 trades), buy-and-hold +480% and RSI +62%. EMA buys on the cross (D31), early in a trend, but is switched on weeks later, once ADX confirms the trend (D53), so it mostly waits for a fresh cross. P6-5b and EMA crossover v2 (a state entry) are the levers.
- Checked on the main stack (2026-10-29, after `up --build`, migration 0015 applied):
  - `engine.backtest.run ema_crossover BTC/USDT 1d --days 165 --auto-switch ...` (hold 1, hand over) stored run 51: 5 switches, no trades, and the baselines printed (EMA alone +12%, RSI +9%, buy-and-hold +2%).
  - The same mapping on 4h, 300 days, a hold of 3 and close first, stored run 52: −9.97% in 14 trades, 11 of them sold by a switch, from 53 switches. The candles each config decided add up to the range: 452 + 642 + 706 = 1800. Alone, EMA made −20%, RSI −24.5% and buy-and-hold −7.8%.
- Checked by hand in Chrome on the same stack:
  - The runs list shows "auto-switching · starts on ema_crossover" on both runs, and plain runs as before.
  - Run 52's page has the mapping, all 53 switches with their reasons, and the trades' "Bought by" and "switch (close first)". Light and dark themes; no console errors.
  - The first switch is on the last warm-up candle, so it takes over at the range's first candle, as intended. The first trade is the documented same-open buy-then-sell (−3 USDT).
- The hand check found two things, fixed:
  - "On a switch, close first its position" now reads "An open position on a switch: close first".
  - A backtest's trades can span years, so their Entry and Exit times now show the year (`Timestamp`'s `year`).
- The e2e suite found one more, fixed, from P5-5: `global-setup.ts` rebuilt the engine's image but never the backtester's. The page's backtest ran on a 37-hour-old image, whose fills have no `decided_by`. Its numbers matched the CLI's, but `backtests.spec.ts`'s stored-result check failed. The setup now builds both.

### P6-5b · Regime detector lag
`strategy` · Depends on: P6-5
- [x] Lag/flip report on the 2019–2023 fixture; Kaufman's efficiency ratio and a fast trend exit judged with the combined backtest against its baselines
- [x] Also weigh EMA crossover v2 (enter while fast > slow, a state, D31): P6-5 found an event entry and a late switch don't mix

**Note: the regime detector's lag (from P6-3, D53).** ADX is smoothed twice, so the detector recognises a new trend or range about 2 × `adx_period` candles late. On 1d that's 2–4 weeks: summer 2023 read `trending_up` until Jul 25, though BTC had flattened out by late June. Most of the lag is in *leaving* a trend, because ADX takes weeks to fall below 20. Once this backtest exists, try two candidate fixes against the current rules:
- **Kaufman's efficiency ratio** (net change ÷ the sum of absolute changes over 20 candles) as the strength measure. It has a fixed window and no smoothing tail.
- **A fast trend exit:** `unclear` as soon as the DI lines cross or the EMA slope flips, rather than waiting for ADX < 20. "ADX falling" was considered and dropped: in July 2023 ADX bounced back up mid-fade, so that rule would flicker.

Judge them in two steps:
- **First, a lag/flip report over the 2019–2023 fixture:** days until each labeled span is recognised, and label changes per year. Faster labels also flip more, which means a longer minimum hold.
- **Then this combined backtest:** keep a variant only if it makes more money net of switching. Don't tune to the 9 labeled spans. A shorter `adx_period` is the fallback: easy, but noisier everywhere.

**Done when:** the lag/flip report exists, and each variant is kept or dropped on the combined backtest's numbers against its baselines. A kept one bumps `RegimeDetector.version`.

**Done** (D56): nothing was promoted. The detector stays at version 1 and EMA crossover keeps its cross entry.
- **The owner's choices:**
  - A probe showed the issue's "fast trend exit" (unclear on a DI cross or slope flip) is already the rule above ADX 25. In summer 2023 neither happened: +DI stayed above −DI and the EMA rose until Jul 24–25, while ADX climbed back to 34.7 mid-July. So the second variant became **ADX in, ER out** (a trend also needs the efficiency ratio at the range level).
  - The efficiency ratio's levels **match ADX's base rates**: on the fixture, ADX ≥ 25 on 60.8% of candles and < 20 on 19.1%, so the levels are 0.15 and 0.05 (0.158 and 0.070 unrounded).
  - The state entry would ship as an `entry` param at version 1. That was moot once it was dropped.
  - After the numbers came in, the owner first chose to promote `er` and the state entry, as the rule said. Then `er` failed `test_regime_history.py` (below), and the owner chose to revert both.
- **The study:**
  - `engine/regime/study.py` (`uv run python -m engine.regime.study FILE...`, about a minute) runs four variants as subclasses of `RegimeDetector`, swapped into an `AutoSwitcher` as the switcher tests do: v1, `er` (ER(20) replaces ADX), `adx_er_exit` and `adx10`.
  - `EmaStateEntry` (`ema_crossover_state`, not registered) is the state entry.
  - The lag/flip measures are `regime/lag.py` (pure). `efficiency_ratio()` joined `shared/indicators.py`.
  - The labeled spans moved to `regime/history.py`, shared with the history test.
  - `record_history` takes `--symbol/--start/--end`; its defaults re-record the BTC fixture byte for byte. It recorded two holdouts: ETH/USDT 2019–2023 and BTC/USDT 2024-01 to 2026-09.
- **The keep rule, fixed before any numbers:** a variant must beat v1's combined return (cross entry) in ≥ 6 of 9 hold × policy cells on BTC 2019–2023 and ≥ 2 of 3 (hold 3) on each holdout. The state entry is judged the same way, against the cross entry under the same detector.
- **Lag (BTC 2019–2023, days from a span's start to its first reading):**

  | | Uptrends | Downtrends | Ranges | Label changes/yr | Confirmed (hold 1/3/5)/yr |
  |---|---|---|---|---|---|
  | v1 | 7, 2 | 19, 22 | 25, 35 | 36 | 16 / 11 / 9 |
  | `er` | 1, 0 | 10, 8 | 1, 19 | 103 | 35 / 13 / 6 |
  | `adx_er_exit` | 7, 2 | 19, 22 | 25, 35 | 50 | 16 / 11 / 8 |
  | `adx10` | 2, 0 | 8, 8 | 53, 33 | 47 | 17 / 14 / 10 |

  Crashes read alike under all four (0–6 days), since ATR decides them.
- **Combined backtest (total return; all fills at the next open, 0.1% fee, 0.05% slippage):**

  | | BTC 2019–23 (9 cells) | ETH 2019–23 (hold 3) | BTC 2024–26 (hold 3) |
  |---|---|---|---|
  | v1, cross entry | −36% to −5% | −20% to +29% | −13% to −8% |
  | v1, state entry | +162% to +456% | +195% to +435% | −25% to −20% |
  | `er`, cross entry | −39% to −4% | −12% to +75% | −7% to +40% |
  | `er`, state entry | +104% to +344% | +802% to +1193% | +10% to +34% |
  | Best baseline | EMA alone +641% | buy-and-hold +1677% | EMA alone +45% |

- **Verdicts:**
  - `er` passed (6/9, 3/3, 3/3), and so did the state entry under it (9/9, 3/3, 2/3).
  - The state entry under v1 failed (0/3 on BTC 2024–26), as did `adx_er_exit` (0/9; the summer-2023 fade was a slow drift, so the ratio stayed above its level) and `adx10` (1/3 on BTC 2024–26).
  - With the unrounded levels (`--er-levels 0.16 0.07`) the verdicts were the same (`er` 8/9).
- **Why `er` wasn't promoted after all:** as detector version 2 it failed 3 of the 9 labeled spans v1 passes.
  - **The 2023 summer range** read as it on 9% of its days after the allowance. The ratio ignores how big a move is, so early August's 0.01%-a-day drift read `trending_down`.
  - **The 2020 range** narrowly lost: unclear 17 days, ranging 15.
  - **The Luna span's first day** read the March rally's `trending_up`.

  Its backtest wins were also fragile. It beat v1 only while both lost money with the cross entry. With the state entry it won both holdouts but lost 5 of 9 cells on the main file.
- **Nothing beat its baselines** on any file, so D55's rule stands: no money on auto-switching.
- **Tests:**
  - `test_regime_study.py`: the study's v1 makes D54's 57/41/34 switches; the matched levels are 0.15/0.05; each variant reads as its rule says; the memo returns the detector's readings; the state entry buys mid-rise where the cross entry waits; the keep rule counts as above.
  - `test_regime_lag.py` (scripted labels) and `test_indicators.py` (`efficiency_ratio`).
- **Checked:** the promoted version (detector v2 and the `entry` param) reproduced the study's report line for line before it was reverted. The reverted study gives the same report again.

### P6-5c · Auto-switching backtests from the dashboard
`frontend` · Depends on: P6-5
- [x] `auto_switch` on `RunBacktestCommand`; an auto-switching section in `BacktestForm` reusing `AutoSwitchSheet`'s mapping fields

**Done when:** a run from the form equals `engine.backtest.run --auto-switch` with the same arguments (e2e). The baselines stay CLI-only unless this issue decides to store them.

**Done (D57):**
- **The owner's choice:** store the baselines and show them.
- **The command:** `RunBacktestCommand.auto_switch` / `BacktestRequest.auto_switch` (`AutoSwitchSettings`: an unmapped regime is a 422). The worker checks each config with `checked_auto_switch()`, which `--auto-switch` now shares, so both refuse with the same words.
- **The baselines:** `run.backtest()` runs `compare.baselines()` with the run and stores each as a `BaselineResult` in `backtest_runs.baselines` (migration 0016): fills and equity curve only, metrics worked out when shown. The CLI prints them from there.
- **The page:** `AutoSwitchFields` (extracted from `AutoSwitchSheet`) and the policy radios in `BacktestForm`, behind "Switch strategy by regime"; the run's page ranks the run among its baselines ("Against each config alone").
- **Tests:** `test_backtest_compare.py`, `test_backtest_store.py`, `test_backtest_worker.py` (a dashboard job equals the CLI's run, baselines included; a bad mapping fails with the CLI's words; the detector's warm-up gives `max_days`), `backend/tests/test_backtests.py`, and the e2e `backtests.spec.ts` (form vs. CLI: the same mapping, result and baselines).

### P6-6 · Grid and DCA strategies
`strategy` · Depends on: P2-2
- [x] Grid (range, number of grids); DCA (amount, interval, dip trigger)

**Done when:** both pass tests on known series and run in the backtester.

**Done (D58):**
- **The owner's choices:** backtester and paper bots together; the grid decides on closes (no resting orders); DCA's amount is the bot's sizing, with an optional take-profit.
- **The contract:** `Strategy.scales_in` lets a BUY while holding add to the position; a SELL's `size_hint` sells that share of it (`sell_amount()` in `risk/sizing.py`, for the simulator and `bots/orders.py` alike).
- **Grid** (`strategies/grid.py`: `lower`, `upper`, `grids`): a slice below each line, sold one line up, everything at or above the top. Stateless over a 200-candle replay; hints are slices of the bot's sizing (the whole grid's budget). **DCA** (`strategies/dca.py`: `interval`, `dip_percent`, `take_profit_percent`): a buy every `interval` candles by open time, one more on a dip through the level below the last buy, and everything sold at the take-profit. Version 2 measures the dip from the last buy (`Position.last_entry`, filled in by the simulator and `PaperBroker.position()`); version 1 measured it from the average, and run 57 showed it buying on every wobble.
- **Backtester:** adds are sized with the coins in the equity, averaged as the ledger averages, with stops placed again from the new average; `close_first` closes what a partial sell leaves. `trades()` makes each sell a trade against the average cost.
- **Paper:** `max_open_positions` lets an add through (`Account.adds`); `opening_buy()` finds the buy that opened the current position (stop exits, the stop plan and the keeper stay its); no add while a stop's exit sells the position; a partial sell records `"sell"` in its `reason`.
- **Chart:** the grid's lines are drawn on the candles.
- **Tests:** `test_grid.py`, `test_dca.py` (hand-worked series), `test_sizing.py` (`sell_amount`), `test_backtest_report.py` (per-sell trades), `test_backtest_simulator.py` (adds, partial sells, stops after an add, grid and DCA end to end), `test_bot_orders.py`, `test_limits.py`, `test_risk_account.py`, `test_risk_manager.py`, `test_bot_runner.py` (a DCA bot past the open-positions limit, stops after an add, no add while a stop sells, a grid's partial sell), `test_chart_parity.py`.

---

## Phase 7 — Alerts and monitoring

### P7-1 · Telegram alerts
`ops` · Depends on: P3-3
- [x] Alerts bot service consuming engine events; per-event toggles (`alert_settings`); daily summary
- [x] Alert preferences on the Settings page (moved from P5-7, D50): a toggle per event the service sends

**Done when:** a paper trade, a risk block and an error each produce a Telegram message.

**Done (D59):**
- **The owner's choices:** the events come from the log stream (no engine change), the summary at 08:00 Manila about the day before, and toggles with no code.
- **The service:** `alerts`, a 7th Compose service from the API's image (`backend/alerts/`). It reads the `logs` stream from a cursor in Redis, maps lines with `shared.alerts.to_alert()`, checks `alert_settings` (migration 0017) for each batch, holds back repeats (`Quieter`: a block per bot and rule an hour, an error per service, event and bot every 10 min) and sends with python-telegram-bot, one a second, at least once. Without `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` it only logs. `chat-id`, `test` and `summary` subcommands help set it up.
- **The events:** trade (one per order: average price, fee, why, a closing sell's realized P&L), stop, blocked and shrunk buys (shrunk off by default), the error breaker, switches, the kill switch, any error line, and the daily summary.
- **Robust:** database reads give up after 5 s and fall back, so an error alert still goes with the database down. Telegram's flood control is waited out, network errors retried, refusals dropped and shown on the Settings page. The token never reaches a log.
- **The page:** Settings → Alerts (`AlertSettingsForm`), with whether the service is running and Telegram set up. "alerts" is on the Logs page's service filter. The e2e stack blanks the token.
- **Also:** `Backoff` moved to `shared/backoff.py`.
- **Tests:** `test_alerts.py` (mapping, quieter, the migration's defaults), `test_alerts_format.py`, `test_alerts_telegram.py` (fake bot: interval, flood control, retries, refusals, no token in logs), `test_alerts_service.py` (test Redis and DB: cursor, toggles, quieting, trades from their rows or their line, the database down, the summary once a day), `test_alert_settings.py`, and `alert_of()` checks in the engine tests that log those lines; e2e `settings.spec.ts` toggles an alert.

### P7-2 · Telegram commands
`ops` `security` · Depends on: P7-1, P4-5
- [x] /status, /pnl, /pause, /kill; ignore all other chat IDs

**Done when:** /kill stops all bots, and a message from another chat ID is ignored and logged.

**Done (D60):**
- **The owner's choices:** /kill asks with buttons, as UX1's dialog does ("Stop all bots" / "Stop all + close positions" / "Cancel"). `/pause <name or id>` pauses one bot, and `/pause` alone offers a button per running bot plus "All running bots". Strangers are logged, not alerted.
- **The service:** `CommandBot` (`backend/alerts/commands.py`) runs in the alerts service beside the alert loop when Telegram is set up. It long-polls `get_updates` and replies through the same `TelegramSender`, now locked so alerts and replies keep to one a second. `/kill` and `/pause` send the dashboard's `KillCommand`/`BotCommand` with source `telegram`, so the engine needed no change.
- **Safe by default:**
  - Another chat gets no reply and is logged (`alerts.command.ignored`).
  - A command waiting more than 2 min in the backlog after a start or an outage isn't run; later batches aren't judged by date, so clock drift can't refuse them.
  - Buttons work once, for 2 min, from this run only.
  - `/start` (which Telegram sends by itself) only shows the help; nothing starts a bot.
- **Replies:** `replies.py` (pure). `/status`: engine heartbeat age, bots, positions, oldest price. `/pnl`: equity, today's and total P&L, drawdown, positions, all from `read_overview()`. The kill's result reads as its alert does (`format.kill_lines`), and that alert isn't sent again (`ShownKills`) unless the reply timed out.
- **Checked live (2026-11-01):** from the phone, `/pause` paused a bot (41 ms). `/kill` stopped the two active bots ("kill switch from telegram", 181 ms), with no repeated kill alert. `/status` sent in a group (another chat ID) got no reply and logged `alerts.command.ignored`, although the owner sent it.
- **Also:** `ENGINE_STALE_SECONDS` (`shared/heartbeat.py`) and `PRICE_STALE_SECONDS` (`shared/prices.py`), the UX5 cutoffs, for P7-3 too. `chat-id` explains a `Conflict` with the running service.
- **Tests:** `test_alerts_commands.py` (parsing, reading updates, every reply's text), `test_alerts_command_bot.py` (test DB and Redis, a fake Telegram, an engine stand-in: the kill buttons, close, cancel, double taps, expiry and restarts, no reply; strangers ignored and logged; the backlog rule; pause by name, id, buttons and all, refusals; status and pnl, the database down; offsets, the menu, polling errors without the token), `test_alerts_telegram.py` (the lock, prompts, edits) and `test_alerts_service.py` (a shown kill isn't alerted).

### P7-3 · Heartbeat alert
`ops` · Depends on: P7-1
- [x] Engine heartbeat; alert when missing

**Done when:** stopping the engine container triggers an alert within 2 minutes.

**Done (D61):**
- **The owner's choices:** a 60 s cutoff (a restart or rebuild of the engine stays quiet); a toggle on Settings → Alerts, on by default; only "down" and "back up", no message for a quick restart.
- **The watch:** `EngineWatcher` (`backend/alerts/watch.py`), a third task in the alerts service, reads `engine:heartbeat` every 5 s. The engine is down when the beat hasn't changed for 60 s on the service's own monotonic clock (never its `ts` against a clock, so Docker Desktop's drift after a sleep can't fake one). That's at most ~70 s after the last beat. Down and back up are each said once, and back up only if down was. While it's down, `alerts:engine` keeps the state, so a restart of the alerts service neither repeats nor forgets.
- **Redis too:** a minute of failed reads says Redis is unreachable, then when it's back, and the engine gets a fresh minute.
- **The toggle:** `engine_down` in `alert_settings` (migration 0018).
- **Checked live (2026-11-01):** `docker compose stop engine` at 02:52:09 UTC sent "Engine DOWN" at 02:53:10 (61 s); `start` sent "Engine back up" 6 s after its first beat. A `restart` sent nothing, and muted, the outage was only logged (`alerts.engine_muted`).
- **Also:** `age()` moved from `replies.py` to `format.py`.
- **Tests:** `test_alerts_watch.py` (the rule on fake clocks: first look, a far-off clock, once each, restarts, no beat ever, the key going away, carried state, Redis; the texts; the watcher over a fake Redis that breaks) and `test_alerts_service.py` (test DB and Redis: one message each way, muted, the database down). `shared/tests/test_alerts.py` checks 0017 + 0018 seed every default.

### P7-4 · Health and Sentry
`ops` · Depends on: P0-T2
- [x] `/health` per service; Sentry in API and engine

**Done when:** a deliberate exception shows up in Sentry with a stack trace.

**Done (D62):**
- **The owner's choices:** a health endpoint plus a Settings → System tab; Sentry in all four long-running services (engine, backtester, API, alerts), one project with a `service` tag; crashes and every error line, rate-limited.
- **Sentry (`shared/errors.py`):** `init_errors(settings, service)` starts Sentry only with `SENTRY_DSN` set (blank: off, as the e2e stack has it) and returns an `ErrorForwarder`, a structlog processor beside the log mirror.
  - **Error lines:** every ERROR/CRITICAL line goes to Sentry. Inside an `except` block, it goes as that exception with its stack trace (`sys.exc_info()`), so `bot.error` and the like carry one with no change where they're logged. Outside one, it's a message grouped by service and event.
  - **Rate limit:** the same event for the same bot at most once per 10 min; the next one says how many were held.
  - **Breadcrumbs:** INFO and WARNING lines become breadcrumbs.
  - **Crashes:** they come from Sentry's own excepthook, FastAPI and logging integrations. Deliberate 5xx answers aren't reported (they have their own error line).
- **No secrets:**
  - **Off:** frame locals, request bodies, cookies, headers, query strings and user info, through `data_collection`.
  - **Scrubbed:** every event and breadcrumb is cleared of the settings' `SecretStr` values, the known secret variables (`POSTGRES_PASSWORD`, `JWT_SECRET`, `ENCRYPTION_KEY`, `TELEGRAM_BOT_TOKEN`, `SENTRY_DSN`) and anything shaped like a Telegram bot token, which Sentry's httpx breadcrumbs would otherwise carry in the URL.
  - **Tracing:** off.
- **Health:**
  - **`GET /health`** (public, Docker's check) runs `SELECT 1` and `PING`, 2 s each: 200, or 503 saying which is down, never why. A change is logged once (`api.health.failed`/`.recovered`).
  - **`GET /health/services`** (signed in) adds the engine's, backtester's (`backtester:heartbeat`, new) and alerts service's beats in Redis and whether Sentry is on. It's judged by the pure `backend/health/report.py` and shown by `SystemHealth` on Settings → System, refreshed every 5 s.
- **Checked live (2026-11-01):** with the owner's DSN, `python -m shared.errors test` from the engine and API images each showed up in Sentry as an issue with its stack trace and `service` tag; all four services logged `errors.reporting`, and `/health` answered 200.
- **Also:** `python -m shared.errors test [--service NAME]` sends a deliberate exception. The `sentry_events` fixture (`shared.testing`) keeps events instead of sending them. `json_default` in `shared/logs.py` is public.
- **Tests:**
  - `test_errors.py`: stack traces with no locals, messages, breadcrumbs, the rate limit, scrubbing, no DSN, a failing capture, the test command.
  - `test_health.py`: 200, 503 for the database or Redis, logged once and on recovery.
  - `test_services_health.py`: every state, the route reading each beat, an unhandled route exception reaching Sentry without the body or cookie.
  - `test_heartbeat.py`: the backtester's beat.
  - e2e `settings.spec.ts`: the System tab.

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
- [ ] Binance spot testnet run (D23): `ExchangeAdapter("binance", sandbox=True)` with testnet keys (GitHub login at testnet.binance.vision). Place, partly fill and cancel through the full path, plus a rejected order and a restart with reconciliation
- [ ] Tiny real order test on MEXC (covers what the testnet can't: MEXC's replies, error codes, minimums, 0.5% price band)

**Done when:** the full path passes on the Binance testnet, then a tiny real MEXC order is placed and cancelled through it, and reconciliation fixes a seeded mismatch in a test.

### P9-2 · Go-live gate and first live bot
`money` `blocked` · Depends on: P9-1
- [ ] Every box in the ROADMAP Phase 9 go-live gate ticked
- [ ] Switch one bot to live via the UX3 flow, with a small amount

**Done when:** the go-live gate is fully ticked and one live bot has made its first trade.

### P9-3 · Weekly review routine
`ops` · Depends on: P9-2
- [ ] Weekly live vs. paper vs. backtest comparison (report or dashboard view)

**Done when:** the first weekly comparison has been produced and reviewed.
