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
