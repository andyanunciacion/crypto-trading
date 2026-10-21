# frontend

Next.js (TypeScript) dashboard with Tailwind CSS, shadcn/ui, TradingView Lightweight Charts and TanStack Query.

It's a client only: all data and actions go through the API. Read `AI_DOCS/DESIGN.md` before any UI work.

## TypeScript 6 and 7 side by side

`pnpm typecheck` runs TypeScript 7 (`@typescript/native`). TypeScript 7.0 has no JavaScript API yet, so typescript-eslint can't use it. That's why the `typescript` package name points to `@typescript/typescript6`, which ESLint and Next.js import. Drop the alias once typescript-eslint supports TS 7 (tracked in typescript-eslint#10940).

`pnpm peers check` warns that a few plugins inside `eslint-config-next` list ESLint ≤ 9. `eslint-config-next` itself supports ESLint ≥ 9, and ESLint 9 is deprecated, so we stay on 10.
