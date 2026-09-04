"""Runtime settings. Everything comes from the environment (or .env); nothing market-related lives here."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # secrets / keys
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    fred_api_key: str | None = Field(default=None, alias="FRED_API_KEY")
    alphavantage_api_key: str | None = Field(default=None, alias="ALPHAVANTAGE_API_KEY")
    basic_auth_user: str | None = Field(default=None, alias="DESK_BASIC_AUTH_USER")
    basic_auth_pass: str | None = Field(default=None, alias="DESK_BASIC_AUTH_PASS")

    # locations
    data_dir: Path = Field(default=REPO_ROOT / "data", alias="DESK_DATA_DIR")
    config_dir: Path = Field(default=REPO_ROOT / "config", alias="DESK_CONFIG_DIR")
    tz: str = Field(default="Europe/Berlin", alias="TZ")

    # behaviour
    daily_job_hour: int = Field(default=7, alias="DESK_DAILY_HOUR")
    daily_job_minute: int = Field(default=0, alias="DESK_DAILY_MINUTE")
    backup_hour: int = Field(default=2, alias="DESK_BACKUP_HOUR")
    backups_to_keep: int = Field(default=30, alias="DESK_BACKUPS_KEEP")
    price_lookback_days: int = Field(default=400, alias="DESK_PRICE_LOOKBACK_DAYS")
    alphavantage_daily_budget: int = Field(default=25, alias="DESK_ALPHAVANTAGE_BUDGET")
    http_timeout_s: float = Field(default=20.0, alias="DESK_HTTP_TIMEOUT")
    scheduler_enabled: bool = Field(default=True, alias="DESK_SCHEDULER_ENABLED")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "desk.sqlite3"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive" / "reports"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.cache_dir,
            self.backup_dir,
            self.inbox_dir,
            self.inbox_dir / "portfolio",
            self.archive_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
