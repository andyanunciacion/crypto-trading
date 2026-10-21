# shared

Code used by both `backend/` and `engine/`: SQLAlchemy models, pydantic schemas, typed config (pydantic-settings), Decimal/money helpers, and Alembic migrations.

Config: `from shared.config import load_settings`; call it once at startup. It reads env vars (and `.env` locally) and exits with a readable list of anything missing or invalid. Add new settings to `Settings` and `.env.example` together.

See `AI_DOCS/ARCHITECTURE.md` §5 for the data model and its conventions.
