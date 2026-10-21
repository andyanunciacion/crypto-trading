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
    └── Settings      exchange keys, risk limits, alerts, 2FA
```

## 5. Screen inventory

| Screen | Roadmap slice | Status |
|---|---|---|
| Login with TOTP | 5.1 | ⬜ |
| App shell + kill switch + Overview | 5.2 | ⬜ |
| Market chart with trade markers | 5.3 | ⬜ |
| Bots list and detail (create/start/pause/stop, edit params) | 5.4 | ⬜ |
| Backtests (run + results) | 5.5 | ⬜ |
| History with filters and CSV export | 5.6 | ⬜ |
| Logs | 5.6 | ⬜ |
| Settings (keys, risk limits, alerts) | 5.7 | ⬜ |
| Strategy switch markers and switch log | 6.4 | ⬜ |

## 6. Key UX decisions (proposed; validate during paper trading)

| # | Topic | Decision |
|---|---|---|
| UX1 | **Kill switch** | Red button in the header on every screen. One click opens a confirm dialog with two buttons: "Stop all bots" and "Stop all + close positions". Fast, but can't be triggered by a stray click. |
| UX2 | **Mode badge** | A global and per-bot badge: neutral **PAPER**, high-contrast **LIVE**. Shown as text, not just color. |
| UX3 | **Going live** | Switching a bot to LIVE needs the global live flag (D17) plus a dialog where you type the bot's name and enter a TOTP code. |
| UX4 | **Loosening risk limits** | Raising any limit needs a TOTP code; lowering one doesn't. |
| UX5 | **Freshness** | The header shows "last price Xs ago" and engine heartbeat. Numbers older than the stale cutoff are greyed out and show a warning. |
| UX6 | **Numbers** | Tabular (monospace) figures, right-aligned in tables, with currency code. P&L shows a +/− sign as well as green/red. |
| UX7 | **Time** | Shown in Asia/Manila time, with UTC on hover. Stored as UTC. |
| UX8 | **Theme** | Dark by default (charts are easier to read), with a light toggle. |
| UX9 | **Trade explanation** | Clicking a trade marker or history row opens a panel with the signal, indicator values, risk decision and fills. |

## 7. Brand & visual identity

A placeholder until (if ever) a brand is needed: shadcn/ui's neutral theme, system font stack, and a single accent color. Red is used only for danger (kill switch, LIVE, losses), never decoration.

## 8. Design tokens & components

- **Tokens:** shadcn/ui CSS variables in `frontend/` (Tailwind config + `globals.css`). Add semantic tokens for `profit`, `loss`, `live`, `paper`, `stale`.
- **Components:** shadcn/ui for the basics. Project components: `KillSwitch`, `ModeBadge`, `FreshnessIndicator`, `Money` (formats Decimal strings with currency and sign), `CandleChart` (Lightweight Charts wrapper with markers), `TradeExplainPanel`, `StrategyParamsForm` (rendered from the strategy's pydantic schema).
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
