# crypto-trading — UX & Design

> **How crypto-trading looks and behaves.** Product scope: [PRD.md](PRD.md). Tech: [ARCHITECTURE.md](ARCHITECTURE.md). Order: [ROADMAP.md](ROADMAP.md).
> The UX decisions in §6 are **proposed defaults**. Try them during paper trading, then update this file.

## 1. How design works on crypto-trading

1. **Design in code.** There's no designer and no mockups: build screens straight in Next.js with shadcn/ui defaults.
2. **Function first.** Screens show state clearly and make dangerous actions hard to do by accident. Visual polish comes last.
3. **Test it with your own use.** Run paper bots from the dashboard for weeks before going live; fix whatever is confusing or slow.

## 2. UX principles

1. **Safety is always visible.** The kill switch and the current mode (PAPER / LIVE) are on every screen.
2. **Show freshness.** Live numbers say how old they are. Stale data is visibly marked, never shown as current.
3. **Dangerous actions take deliberate effort; stopping is fast.** Going live or loosening risk limits needs confirmation. Pausing and killing are one step.
4. **Every number has a unit.** Currency (USDT, BTC) and sign (+/−) are always shown.
5. **Explain trades.** Any trade on the chart or in history links to why it happened: the signal, its inputs, and the risk check.
6. **Desktop first.** The phone experience is Telegram; the dashboard only needs to be usable on a phone, not optimized for it.

## 3. Users & contexts

| Who | Context | Designs for |
|---|---|---|
| Owner at the desk | Laptop/desktop; reviewing, configuring, backtesting | Bots, Backtests, Settings, History |
| Owner checking in | Quick look; wants status at a glance | Overview |
| Owner reacting to a problem | Urgent; something looks wrong | Kill switch, Logs, bot pause |
| Owner away from PC | Phone only | Telegram (alerts, /status, /kill), not the dashboard |

## 4. Information architecture

```text
Login (password + TOTP)
└── App shell — header: mode badge · engine status · data freshness · KILL SWITCH
    ├── Overview      equity, today's P&L, open positions, bot status
    ├── Market        candlestick chart, indicators, trade + switch markers
    ├── Bots          list → bot detail (strategy, params, mode, history, switches)
    ├── Backtests     run form → results (metrics, equity curve, trades)
    ├── History       trades & orders, filters, CSV export
    ├── Logs          engine decisions, risk events, errors
    └── Settings      risk limits, exchange keys (alerts from P7; 2FA is reset on the server)
```

## 5. Screen inventory

| Screen | Roadmap slice | Status |
|---|---|---|
| Login with TOTP | 5.1 | ✅ |
| App shell + kill switch + Overview | 5.2 | ✅ |
| Market chart with trade markers | 5.3 | ✅ |
| Bots list (create/start/pause/stop, D47) | 5.4 | ✅ |
| Switching a bot's strategy or params (Edit on the Bots list, D51) | 6.1 | ✅ |
| Bot detail (setup, strategy history and rollback, D52) | 6.2 | ✅ |
| Backtests (run + results, D48) | 5.5 | ✅ |
| History with filters and CSV export (orders and fills, D49) | 5.6 | ✅ |
| Logs (engine log and risk events, D49) | 5.6 | ✅ |
| Settings (risk limits and exchange keys, D50) | 5.7 | ✅ |
| Alert preferences (per event, on Settings) | 7.1 | ⬜ |
| Strategy switch markers, switch log and auto-switching (bot detail, D54) | 6.4 | ✅ |

## 6. Key UX decisions (proposed; validate during paper trading)

| # | Topic | Decision |
|---|---|---|
| UX1 | **Kill switch** | Red button in the header on every screen. One click opens a confirm dialog with two buttons: "Stop all bots" and "Stop all + close positions". Fast, but can't be triggered by a stray click. |
| UX2 | **Mode badge** | A global and per-bot badge: neutral **PAPER**, high-contrast **LIVE**. Shown as text, not just color. |
| UX3 | **Going live** | Switching a bot to LIVE needs the global live flag (D17) plus a dialog where you type the bot's name and enter a TOTP code. |
| UX4 | **Loosening risk limits** | Raising any limit needs a TOTP code; lowering one doesn't. A shorter breaker window, or turning any setting off, counts as raising; saving exchange keys needs a code too (D50). |
| UX5 | **Freshness** | The header shows "last price Xs ago" and engine heartbeat. Numbers older than the stale cutoff are greyed out and show a warning. |
| UX6 | **Numbers** | Tabular (monospace) figures, right-aligned in tables, with currency code. P&L shows a +/− sign as well as green/red. |
| UX7 | **Time** | Shown in Asia/Manila time, with UTC on hover. Stored as UTC. |
| UX8 | **Theme** | Dark by default (charts are easier to read), with a light toggle. |
| UX9 | **Trade explanation** | Clicking a trade marker or history row opens a panel with the signal, indicator values, risk decision and fills. |

## 7. Brand & visual identity

A placeholder until (if ever) a brand is needed: shadcn/ui's neutral theme, system font stack, and a single accent color. Red is used only for danger (kill switch, LIVE, losses), never decoration.

## 8. Design tokens & components

- **Tokens:** shadcn/ui CSS variables in `frontend/app/globals.css` (Tailwind v4: no config file; `@theme inline` maps each variable to a utility). Semantic tokens `profit`, `loss`, `live`, `paper`, `stale` are in place (P5-1), in light and dark, e.g. `text-profit`, `bg-live`.
- **Components:** shadcn/ui for the basics (`components/ui/`: button, card, input, label, badge, table, tooltip, alert-dialog, sheet). Project components (in `components/`; ✅ = built):
  - ✅ `AppHeader`: name, nav, mode, freshness, kill switch, theme, sign-out.
  - ✅ `KillSwitch` (UX1), `ModeBadge` (UX2), `FreshnessIndicator` (UX5).
  - ✅ `Money` and `Percent`: format the API's Decimal strings with `Intl.NumberFormat`, which reads a numeric string exactly (no float), with the currency and, for P&L, a sign and color.
  - ✅ `Timestamp` (UX7), `ThemeToggle` (UX8).
  - ✅ `OverviewProvider`: the one WebSocket a screen's header and body share (D45).
  - ✅ `CandleChart` (D46): Lightweight Charts with candles, volume, the strategy's lines (a legend shows the hovered candle's OHLC and each line's value; RSI gets its own pane), the forming candle drawn faint, and trade markers at the fill price (▲ buy / ▼ sell, labelled B, S, SL, TS, TP, KILL, CLOSE, SW; ink-colored, so shape and label carry the meaning, not color). Line colors are the first two slots of the dataviz reference palette (blue, orange), validated for color-blind separation in both themes; candles use `profit`/`loss`.
  - ✅ `TradeExplainPanel` (UX9, D46): a side sheet with the signal and its indicator values, the risk decision (and on a resize, what was asked for and the account it was checked against), the strategy, sizing, stops or the exit, the fills, and everything stored. Opened by a marker or, from the keyboard, a row of the trades table.
  - ✅ `StrategyParamsForm` (D47): inputs for a list of settings the engine read from a pydantic model (a strategy's params, a sizing mode's, a stop's), each with its default, description and bounds, saying what's wrong as you type. Values stay strings.
  - ✅ `NewBotSheet` (D47): a side sheet with the bot's name, cash, symbol and timeframe, the strategy and its params, sizing and stops; the engine's refusal is shown in it. Bots are created stopped.
  - ✅ `StopBotDialog` (D47): like the kill switch's, focus on Cancel; "Stop" says the position is left with no one watching its stops, "Stop + close position" sells it until it's gone.
  - ✅ `EditStrategySheet` (D51): "Edit" on a bot's row. A side sheet starting from the strategy and params it runs now, and the switch policy as three radios (keep / close first / hand over), each saying in a line what happens to the open position, and whether it holds one now. "Switch" is disabled while nothing changed; a policy alone saves as "Save policy". The engine's refusal is shown in it; its reply says when the switch applies (the next candle, or at Start) and whether the position is being sold. The list's Strategy column shows the policy ("on a switch: keep"). The policy radios are `SwitchPolicyFields`, shared with `RollbackDialog`.
  - ✅ Bot detail (D52, `/bots/[id]`, opened from a bot's name on the list, always underlined so it reads as a link): the bot's name, status, mode and market, Edit, and its setup (strategy and params, switch policy, sizing, stops, cash, why it's paused or stopped). Then "Strategy history": every config it has had, newest first, numbered per bot (#), with its strategy and version, params, when (Manila) and by whom it was made, and a note ("running now", "rollback to #1", "same as running now", or why it can't be rolled back to: its strategy has a new version now). Roll back isn't offered on a row identical to the running config. The result of a command takes the focus, as on the Bots list.
  - ✅ `RollbackDialog` (D52): "Roll back" on every config but the active one. A dialog like `StopBotDialog`, focus on Cancel: "Roll back e2e-eth to #1?", what it runs now and what it goes back to, and `SwitchPolicyFields` starting from the bot's policy. It says the settings are added to the history as a new config and nothing is deleted. The engine's refusal is shown in it.
  - ✅ Auto-switching on the bot detail page (D54): a section saying whether it's on, with "Set up" (or "Change" and "Turn off"). On, a table maps each regime to its strategy and params, under a line with the minimum hold and policy. While it's on, Edit is disabled with a note beside it ("Auto-switching is on: turn it off to switch by hand"), each Roll back is disabled with the same reason in its row, and the Bots list says "auto-switching by regime (Edit is off)".
  - ✅ `AutoSwitchSheet` (D54): a side sheet like Edit's. It explains in a few sentences what a regime is, the dead band and the hold. Then, per regime, a fieldset with a line saying what it means (e.g. "no trend: ADX below 20"), a strategy select (Stay flat among them) and `StrategyParamsForm`; the minimum hold (1–100, in the bot's candles, saying longer is steadier but later); and `SwitchPolicyFields`. It starts from the stored settings, or trend → EMA crossover, range → RSI, down and volatile → stay flat. The engine's refusal is shown in it.
  - ✅ Switch log (D54, bot detail): every switch, newest first: when (Manila), "#1 → #2", by hand / rollback / regime (with the regime's name), the policy, and the reason (the regime's says how long it held and the reading).
  - ✅ Switch markers on `CandleChart` (D54): a small square above the candle the switch was made in, labelled "→ RSI" (by hand) or "auto → RSI" (the regime), in the `primary` token. In the neutral theme that's close to the trades' ink, so shape (a square, not an arrow), place (above the bar, not at a price) and label tell them apart. On the newest candle the label is cut by the price scale until later candles push it left. Not clickable: the switch log explains each. A new switch reloads the chart, whose lines are the new strategy's.
  - ✅ `StatusBadge`, `BotWhy`: a bot's status as a word, and why it's paused or stopped (the Overview and Bots pages).
  - ✅ `NativeSelect` (`components/ui/`): the browser's `<select>`, styled like `Input`.
  - ✅ `StrategySetup` (D48): the strategy and its params, the sizing and the stops, drawn from the catalog; shared by `NewBotSheet` and `BacktestForm`, so a bot and a backtest are set up alike.
  - ✅ `BacktestForm` (D48): the market, days, cash, fee rate and slippage with the CLI's defaults (each fraction's hint shows it as a percent), what's stored of the series, then `StrategySetup`. Run shows "Queued… / Running…" and opens the result; the backtester's refusal is an alert in the form. Refused for too little stored history, it says how many days fit and how far to backfill, with a "Use N days" button. (The form can't say beforehand: the warm-up depends on the params, sizing and stops, which only the engine reads.)
  - ✅ `EquityChart` (D48): equity at each close (the first line slot, blue) with the starting cash dashed, and the drawdown in its own pane below (`loss`), never on a second scale. A legend shows the hovered candle's exact equity and drawdown; the trades table is the table view. One series, so no legend box.
  - ✅ `Ratio`, `Plain`: a fraction from the API shown as a percent by moving the point in the string (`shiftPoint`), and a plain ratio to two places (Sharpe, profit factor); "—" when there's none.
  - ✅ `PageTabs`, `Filter` (D49): a page's views as links setting `?tab=` (so a view can be linked to and survives a reload), and a labelled control in a filter bar. Filters live in the URL.
  - ✅ `usePages` (D49): a newest-first list paged by id, with "Load older"; rows from an earlier query are never shown as the current one's.
  - ✅ History (D49): Orders | Fills with bot, market, side, status, why, and From/To **Manila days** (To included). Export CSV downloads exactly the rows the filters match, named by those days. A row's time is a button that opens `TradeExplainPanel` (UX9).
  - ✅ `CodeDialog` (UX4, D50): asks for a TOTP code before a change that adds risk, with focus in the code field; a wrong or used code, or a lock, is shown in it. The code just used to sign in won't do (each is good once). P9's going-live (UX3) can reuse it.
  - ✅ `RiskLimitsForm` (D50): a mode's limits and circuit breakers, each with its unit, an Off box and a one-line hint, the breaker pairs as one sentence ("Pause a bot after [3] failed orders within [10] min"). Lowering saves at once; anything the API calls looser opens `CodeDialog` listing each change ("Max drawdown: 10% → 15%").
  - ✅ `ExchangeKeys` (D50): the stored accounts by label, exchange and the key's last 4 characters, with Replace and Delete (a confirmation, no code); a form with the reminder "spot read and trade only, never withdrawal, IP-locked", whose password fields are cleared once saved.
  - ✅ Logs (D49): the engine log (Show: everything / decisions / warnings and errors / errors; an event prefix, typed and applied on Enter; bot; service), newest first and read again every 5 s, held still while looking back. Levels are words, warnings and errors colored too. A row's fields open in a native `<details>`. Risk events: bot, decision, rule, days, with each one's inputs.
- **Freshness cutoffs (UX5):** the engine is stale after 30 s without a heartbeat (as Docker's health check), a price after 10 s (the PaperBroker won't fill on an older one), and the feed itself after 5 s without a snapshot. A stale number is greyed out, with a warning icon and the word "stale". The prices shown are the oldest of the ones the engine streams, so a stuck stream can't hide behind a busy one.
- Money arrives from the API as **strings**, not JSON numbers, so Decimal precision survives (D9).

## 9. Performance budget

| Metric | Budget (proposed) |
|---|---|
| Price change in Redis → shown on screen | < 1 s |
| Kill switch click → engine acknowledges | < 2 s |
| First load of Overview (over Tailscale) | < 2 s |
| Market chart with 5,000 candles | Smooth pan/zoom, no visible lag |

## 10. Accessibility

- Aim for WCAG 2.2 AA where it's practical for a personal tool.
- Never use color alone: P&L has a sign, mode badges have text, and stale data has an icon and label.
- The kill switch and its dialog work from the keyboard, and focus goes to the dialog when it opens.

## 11. Testing the UX

- Playwright tests for the critical flows: login with TOTP, start/stop a paper bot, kill switch, going live (with the flag off and on).
- Use the dashboard daily during Phases 5–8; write friction points into §12 and fix them before Phase 9.

## 12. Design questions

- [ ] Should the kill switch close positions by default, or only stop bots? (UX1 asks each time for now.)
- [ ] Which Overview numbers matter most at a glance? Revisit after two weeks of paper trading.
- [ ] Is a phone layout needed at all, or is Telegram enough?
