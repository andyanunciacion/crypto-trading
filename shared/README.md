# shared

Code used by both `backend/` and `engine/`: SQLAlchemy models, pydantic schemas, typed config (pydantic-settings), Decimal/money helpers, and Alembic migrations.

Config: `from shared.config import load_settings`; call it once at startup. It reads env vars (and `.env` locally) and exits with a readable list of anything missing or invalid. Add new settings to `Settings` and `.env.example` together.

Database: tables are SQLAlchemy models in `shared/db/models.py`; `shared/db/session.py` makes the async engine. Schema changes are Alembic migrations in `shared/db/migrations/versions/`, applied by `python -m shared.db.migrate` (the `migrate` Compose service). A test checks that the models and the migrations agree.

Tests: `shared/testing.py` is a pytest plugin (loaded from pyproject.toml) with the `db_url` fixture: a throwaway, migrated TimescaleDB in Docker.

See `AI_DOCS/ARCHITECTURE.md` §5 for the data model and its conventions.
