# backend

FastAPI app: REST + WebSocket API for the dashboard, single-user auth (password + TOTP).

It reads from the database and sends commands to the engine through Redis. It never talks to the exchange and never places orders.

See `AI_DOCS/ARCHITECTURE.md` §2–4 and §9.
