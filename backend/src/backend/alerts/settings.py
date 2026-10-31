"""The alerts service's settings: the shared ones plus the Telegram bot's.

Kept out of the shared `Settings` (and the API's), so only the alerts service sees the
bot token. With no token it runs dry: each message is logged instead of sent, so a fresh
clone and the e2e stack come up healthy without a Telegram bot.
"""

from pydantic import SecretStr, ValidationError, field_validator, model_validator

from shared.config import Settings, describe_errors


class AlertsSettings(Settings):
    # From @BotFather. Anyone who has it can send messages as the bot: keep it secret.
    telegram_bot_token: SecretStr | None = None
    # The one chat alerts go to (yours); `python -m backend.alerts chat-id` finds it.
    telegram_chat_id: int | None = None

    @field_validator("telegram_bot_token", "telegram_chat_id", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `TELEGRAM_CHAT_ID=` in .env, as .env.example has it, means not set.
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _token_with_the_chat(self) -> "AlertsSettings":
        if self.telegram_chat_id is not None and self.telegram_bot_token is None:
            raise ValueError("TELEGRAM_CHAT_ID is set but TELEGRAM_BOT_TOKEN isn't")
        return self

    @property
    def configured(self) -> bool:
        """Whether alerts are really sent (a token and a chat), not just logged."""
        return self.telegram_bot_token is not None and self.telegram_chat_id is not None


def load_alerts_settings(env_file: str | None = ".env") -> AlertsSettings:
    """Load and validate the settings, or exit with a readable list of what's wrong.

    `env_file=None` skips the .env file (tests use this).
    """
    try:
        return AlertsSettings(_env_file=env_file)
    except ValidationError as exc:
        raise SystemExit(describe_errors(exc)) from None
