# crypto-trading — Product Requirements

> **What** we're building and **why**. For *how*, see [ARCHITECTURE.md](ARCHITECTURE.md). For *order and progress*, see [ROADMAP.md](ROADMAP.md).
> Working name: **crypto-trading**. It's a personal tool, so there's no brand, go-to-market or monetization section.

## 1. Summary

A self-hosted web app that watches crypto markets, runs swappable trading strategies, and places orders automatically on a Philippine-licensed exchange (Coins.ph / Coins Pro first). One person runs it. Strategies are backtested, then paper traded, then run live with a small amount. A risk layer that can't be bypassed checks every order. A dashboard and Telegram give live P&L, control and a kill switch. Real money is used only in the last phase.

## 2. Problems

| # | Problem | How crypto-trading solves it |
|---|---|---|
| P1 | Markets run 24/7; I can't watch them or act on signals around the clock | An always-on trading engine on a server runs strategies on every closed candle |
| P2 | Manual trading is inconsistent and emotional | Strategies are code with fixed rules and logged inputs, so every trade can be explained ("why did it buy?") |
| P3 | A strategy that looks good on a chart can lose money once fees, slippage and overfitting are counted | Backtest (with fees, slippage, walk-forward) → paper trade → live small, never skipping a step |
| P4 | Bugs and leaked API keys can drain an account faster than any market move | Every order passes a RiskManager; trade-only, IP-locked keys encrypted at rest; kill switch; 2FA dashboard |
| P5 | No single strategy works in every market (trending vs. ranging) | Strategies are plug-ins; switch them by hand or automatically by market regime |
| P6 | Away from the PC, I don't know when something goes wrong | Telegram alerts for trades, errors and downtime, plus remote /kill |
| P7 | Most big global exchanges are blocked or restricted for Philippine users | Built on CCXT behind a generic `ExchangeAdapter`, starting with Coins.ph; other exchanges can be added if they become legal |

## 3. Users

| Persona | Needs | Account? |
|---|---|---|
| **Owner / operator** (me) | Build, test and run strategies; watch P&L; stop everything fast | Single account with password + TOTP, from P5 |
| **Owner on the phone** | Know about trades, errors and downtime; check status; kill switch while away | Telegram, limited to my chat ID, from P7 |

There are no other users. Adding more users is a non-goal (see §8).

## 4. Product principles

1. **Safety before features.** Every order passes the RiskManager, with no exceptions and no bypass flags.
2. **Backtest → paper → live small.** No strategy touches real money without going through each step. Paper is the default mode.
3. **The engine outlives the UI.** Trading, risk checks and the kill switch keep working if the API or dashboard crashes.
4. **One path end to end, then widen.** One exchange, one pair and one strategy work fully before adding more.
5. **Same code everywhere.** Backtester, paper broker and live broker run identical strategy code. That's what makes results comparable and switching safe.
6. **Every decision is explainable.** Each signal, risk block, order and strategy switch is logged with its inputs.

## 5. Goals and success metrics

Targets are starting guesses. Refine them after the first month of paper trading.

| Metric | Why it matters | Target (proposed) |
|---|---|---|
| Orders that bypassed the RiskManager | Measures P4 directly | 0, always |
| Paper vs. backtest drift (return, trade count) over the same period | Shows the backtest is honest (P3) | Within ~20% for 2–4 weeks before going live |
| Live vs. paper drift, reviewed weekly | Says when to scale up or switch a bot off | Track; no target until Phase 9 |
| Engine uptime on the server | Measures P1 | 2 weeks with no manual fixes before going live |
| Time from engine down to alert on phone | Measures P6 | ≤ 2 minutes |
| Kill switch: time to cancel orders and stop all bots | Measures P4 | ≤ a few seconds, from the dashboard and from Telegram |
| Max drawdown on live capital | Keeps losses survivable | Stays within the 10% limit (see ARCHITECTURE §6) |

## 6. Feature requirements

Phase tags: **[P0]** Setup · **[P1]** Market data · **[P2]** Strategies & backtesting · **[P3]** Paper trading · **[P4]** Risk management · **[P5]** API & dashboard · **[P6]** Strategy switching · **[P7]** Alerts & monitoring · **[P8]** Deployment · **[P9]** Live trading, small.

### 6.1 Trading engine

**Market data**
- [P1] Historical candles (1m, 5m, 15m, 1h, 4h, 1d) downloaded and stored with gap filling.
- [P1] Live ticker and candle stream with auto-reconnect and backoff; latest prices cached and published.
- [P1] Testnet/sandbox switch per exchange (where the exchange offers one).
- [P1] Rate-limit-aware requests with retries on network errors.

**Strategies and backtesting**
- [P2] Strategy plug-ins with typed settings, warm-up period, and signals (BUY / SELL / HOLD, with an optional size hint and stop level).
- [P2] Starter strategies: EMA crossover, RSI mean reversion, Bollinger breakout.
- [P2] Event-driven backtester using the same strategy code, with fees, slippage and minimum order sizes.
- [P2] Backtest report: total return, max drawdown, Sharpe ratio, win rate, profit factor, number of trades, equity curve.
- [P2] Parameter sweeps and walk-forward testing.
- [P6] Grid trading and DCA strategies.

**Paper trading**
- [P3] Paper broker: fills at live bid/ask with fees and partial fills; virtual balances.
- [P3] Bot runner: runs active strategy configs on every closed candle.
- [P3] State recovery after restart, with no lost or duplicated positions.

**Risk**
- [P4] Position sizing: fixed amount, percent of equity, or ATR-based.
- [P4] Stop-loss, take-profit and trailing stops.
- [P4] Limits: max open positions, max exposure per coin, max daily loss, max drawdown.
- [P4] Circuit breakers: repeated errors, stale data, price spikes.
- [P4] Global kill switch: cancel orders, optionally close positions, stop all bots.

**Strategy switching**
- [P6] Manual hot-swap of a bot's strategy or settings without restarting the engine.
- [P6] Per-bot open-position policy on a switch: keep, close first, or hand over.
- [P6] Regime detector (trending up, trending down, ranging, high volatility) and rule-based auto-switching with a cooldown.
- [P6] Version history of every strategy config, with rollback.

**Live trading**
- [P9] Live broker on the same interface as the paper broker.
- [P9] On startup, check the engine's records against the exchange and fix mismatches before trading.

### 6.2 Dashboard (web)

- [P5] Login with password and TOTP two-factor code.
- [P5] Overview: total equity, today's P&L, open positions, bot status.
- [P5] Market: live candlestick chart with trade markers and indicators.
- [P5] Bots: create, start, pause and stop bots; edit strategy settings.
- [P5] Backtests: run a backtest and view its results.
- [P5] History: trades and orders with filters and CSV export.
- [P5] Logs page, and a large kill switch that's always visible.
- [P5] Settings: exchange API keys (encrypted), risk limits, alert preferences.
- [P6] Strategy switches shown on the chart and in a switch log.

### 6.3 Alerts and remote control (Telegram)

- [P7] Alerts: trades, stop-loss hits, strategy switches, errors, limit breaches, daily summary.
- [P7] Commands: /status, /pnl, /pause, /kill, accepted only from my chat ID.
- [P7] Heartbeat: alert if the engine stops sending updates.

### 6.4 Operations

- [P0] One-command local start (`docker compose up`) and CI on every push.
- [P7] Health endpoint and error tracking.
- [P8] Runs 24/7 on a hardened VPS with HTTPS, private access, nightly off-server backups and one-command deploys.

## 7. Non-goals (for now)

- **Multiple users or accounts.** It's a personal tool. Adding users would change auth, data isolation and the risk model.
- **Withdrawals or moving funds.** Withdrawal endpoints are never used, and API keys never have withdrawal permission.
- **Exchanges that are blocked in the Philippines, or reaching them through a VPN.** Their terms and the legal risk make it not worth it.
- **Derivatives, margin and leverage.** Spot only.
- **High-frequency or latency-sensitive strategies.** Strategies run on closed candles.
- **A mobile app.** Telegram covers the phone.
- **Prometheus + Grafana.** Optional later; structlog and Sentry are enough to start.

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| A bug places wrong or duplicate orders | RiskManager on every order; unique client order IDs; tests first on money code; I review every diff that touches orders, risk or keys |
| API key leak | Trade-only, IP-locked key; encrypted at rest; `.env` in `.gitignore` from day one; gitleaks in pre-commit; separate exchange sub-account with only the trading money |
| Overfitted strategy loses money live | Walk-forward testing; 2–4 weeks of paper trading close to the backtest; go-live checklist; start with money I can afford to lose |
| Thin PHP order books → high slippage | Model higher slippage in backtests; prefer limit orders; check order-book depth before choosing pairs |
| Engine down unnoticed | Heartbeat alert ≤ 2 min; Docker restart policies |
| Exchange rules change in the Philippines | Generic `ExchangeAdapter`; check the BSP licensed VASP list and SEC advisories before switching exchanges |
| Dashboard compromised | 2FA; reachable only via Tailscale or IP limits; no withdrawal ability anywhere in the app |
| State mismatch after a crash or restart | Persist all state; reconcile with the exchange on startup before trading |
| Scope creep stalls the project | Phases with exit criteria; one exchange, one pair and one strategy end to end first |

Not financial advice: treat every strategy as an experiment.

## 9. Open questions

- [x] Build custom on CCXT, or extend Freqtrade? → Custom on CCXT (ARCHITECTURE D1).
- [ ] Does Coins Pro have a testnet or sandbox? If not, rely on paper mode plus one tiny real order test before Phase 9.
- [ ] Does CCXT support WebSocket streams (`watch*` methods) for `coinsph`? If not, poll REST for live prices in Phase 1.
- [ ] Which pairs and quote currency come first: PHP pairs (e.g. BTC/PHP) or USDT pairs on Coins Pro? Check order-book depth and fees.
- [ ] pandas-ta or TA-Lib for indicators? Check maintenance status and how each installs in Docker before Phase 2.
- [ ] Which VPS provider in Singapore (DigitalOcean, Vultr, Lightsail or Hetzner)? Decide in Phase 8.
- [ ] Dashboard access: Tailscale only, or a domain with IP limits? Decide in Phase 8.
- [ ] Starting live capital and the threshold for scaling up. Decide before Phase 9.
- [ ] Are trade records needed for Philippine tax reporting? CSV export covers it for now.
