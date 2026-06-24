"""Typed settings loaded from environment. No secret is ever hardcoded; the
GitHub Actions cron injects these from repo secrets, local dev reads .env."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    youtube_api_key: str = ""
    openai_api_key: str = ""
    tracked_channels: str = "@nicksaraev,@LiamOttley"

    @property
    def channel_handles(self) -> list[str]:
        return [c.strip() for c in self.tracked_channels.split(",") if c.strip()]


settings = Settings()  # import this everywhere
