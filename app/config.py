"""Settings. Every secret arrives from the environment — never from a file in git."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://company:company@company-postgres:5432/company"

    # Notification adapter. Empty token disables delivery; events are still persisted.
    telegram_bot_token: str = ""
    telegram_admin_chat_ids: str = ""

    # Deduplication window for identical alerts, mirroring youtube-auto's own 6h rule.
    alert_dedupe_hours: int = 6

    environment: str = "production"
    timezone_display: str = "Europe/Istanbul"

    @property
    def chat_ids(self) -> list[str]:
        return [c.strip() for c in self.telegram_admin_chat_ids.split(",") if c.strip()]


settings = Settings()
