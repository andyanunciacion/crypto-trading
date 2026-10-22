"""Apply database migrations. Run with `python -m shared.db.migrate`.

Compose runs this as the one-shot `migrate` service before the API and engine start,
so the schema is always current. Alembic is configured here in code rather than from
an alembic.ini, so the migrations ship inside the package and work in Docker as-is.
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL

from shared.config import load_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: URL) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Handed to env.py as an object, never written into the config as text,
    # so the password isn't rendered into a string anywhere.
    config.attributes["url"] = url
    return config


def upgrade(url: URL, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: URL, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def main() -> None:
    settings = load_settings()
    # Shows Alembic's "Running upgrade ..." lines.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    upgrade(settings.database_url)


if __name__ == "__main__":
    main()
