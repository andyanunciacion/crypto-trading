"""Typed settings for the API and the engine, read from environment variables.

Every setting is an upper-case env var (`MODE`, `POSTGRES_PASSWORD`, ...); see
.env.example for the full list. Call `load_settings()` once at startup.
"""

from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL

# CCXT's unified format: upper-case BASE/QUOTE, e.g. BTC/USDT.
Symbol = Annotated[str, Field(pattern=r"^[A-Z0-9]+/[A-Z0-9]+$")]


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

    # Symbols whose live price the engine streams into Redis: PRICE_SYMBOLS=BTC/USDT,ETH/USDT.
    # The running bots' symbols are streamed too; this list adds any others.
    # NoDecode: read the env value as plain text (comma-separated), not as JSON.
    price_symbols: Annotated[list[Symbol], NoDecode, Field(min_length=1)] = Field(
        default_factory=lambda: ["BTC/USDT"]
    )

    # Where the long-running services report errors (P7-4, D62; shared/errors.py): a
    # Sentry project's DSN. Blank: errors stay in the logs only. Anyone with it can send
    # events to the project, so it's kept like a secret.
    sentry_dsn: SecretStr | None = None
    # Sentry's environment tag, e.g. "local" here and "server" on the VPS.
    sentry_environment: str = Field(default="local", min_length=1, max_length=64)

    @field_validator("price_symbols", mode="before")
    @classmethod
    def _split_commas(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("sentry_dsn", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `SENTRY_DSN=` in .env, as .env.example has it, means not set.
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def database_url(self) -> URL:
        # Built from parts, not a formatted string, so a password containing '@', ':' or
        # '/' can't break the URL. A URL object also hides the password when printed.
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


def load_settings(env_file: str | None = ".env") -> Settings:
    """Load and validate settings, or exit with a readable list of what's wrong.

    `env_file=None` skips the .env file (tests use this).
    """
    try:
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        raise SystemExit(describe_errors(exc)) from None


def describe_errors(exc: ValidationError) -> str:
    """A settings error as a list of what to fix, for the API's settings too."""
    # Only the setting name and pydantic's message, never the input value,
    # so a mistyped secret can't end up in the logs.
    lines = ["Configuration error. Fix these settings in .env (see .env.example):"]
    for error in exc.errors():
        name = str(error["loc"][0]).upper() if error["loc"] else "?"
        problem = "required but not set" if error["type"] == "missing" else error["msg"]
        lines.append(f"  {name}: {problem}")
    return "\n".join(lines)
