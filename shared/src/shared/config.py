"""Typed settings for the API and the engine, read from environment variables.

Every setting is an upper-case env var (`MODE`, `POSTGRES_PASSWORD`, ...); see
.env.example for the full list. Call `load_settings()` once at startup.
"""

from typing import Literal

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Locally, values are also read from .env in the current directory. In Docker
    # there is no .env in the image (see .dockerignore): Compose passes them in.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The global live gate (D17): while this is "paper", no bot can trade real money.
    mode: Literal["paper", "live"] = "paper"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    postgres_user: str
    # SecretStr prints as '**********' in repr() and logs; use .get_secret_value().
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "db"  # the Compose service name
    postgres_port: int = 5432

    redis_url: str = "redis://redis:6379/0"


def load_settings(env_file: str | None = ".env") -> Settings:
    """Load and validate settings, or exit with a readable list of what's wrong.

    `env_file=None` skips the .env file (tests use this).
    """
    try:
        # mypy can't tell that pydantic fills the required fields from the environment.
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as exc:
        raise SystemExit(_describe(exc)) from None


def _describe(exc: ValidationError) -> str:
    # Only the setting name and pydantic's message, never the input value,
    # so a mistyped secret can't end up in the logs.
    lines = ["Configuration error. Fix these settings in .env (see .env.example):"]
    for error in exc.errors():
        name = str(error["loc"][0]).upper() if error["loc"] else "?"
        problem = "required but not set" if error["type"] == "missing" else error["msg"]
        lines.append(f"  {name}: {problem}")
    return "\n".join(lines)
