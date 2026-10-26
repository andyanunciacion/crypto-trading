"""The API's settings: the shared ones (shared/config.py) plus the API's own secrets.

Kept out of the shared `Settings`, so the engine never needs (or sees) them.
"""

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, ValidationError, field_validator

from shared.config import Settings, describe_errors


class ApiSettings(Settings):
    # Signs the session JWTs. Anyone who has it can sign in as anyone: keep it secret.
    jwt_secret: SecretStr
    # Fernet key: encrypts the TOTP secret at rest (and exchange keys from P5-7, D12).
    encryption_key: SecretStr
    session_hours: int = Field(default=12, ge=1, le=24 * 7)
    # Browsers accept Secure cookies on http://localhost, so this stays on locally too.
    cookie_secure: bool = True

    @field_validator("jwt_secret")
    @classmethod
    def _long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            # Not how long it is: that says something about a secret too.
            raise ValueError("must be at least 32 characters (see .env.example to make one)")
        return value

    @field_validator("encryption_key")
    @classmethod
    def _a_fernet_key(cls, value: SecretStr) -> SecretStr:
        try:
            Fernet(value.get_secret_value())
        except ValueError:
            # Not the value itself: it's a secret.
            raise ValueError("must be a Fernet key (32 url-safe base64-encoded bytes)") from None
        return value


def load_api_settings(env_file: str | None = ".env") -> ApiSettings:
    """Load and validate the API's settings, or exit with a readable list of what's wrong.

    `env_file=None` skips the .env file (tests use this).
    """
    try:
        return ApiSettings(_env_file=env_file)
    except ValidationError as exc:
        raise SystemExit(describe_errors(exc)) from None
