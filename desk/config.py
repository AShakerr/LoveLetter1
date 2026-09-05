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
    inbox_dir: Path = Field(default=REPO_ROOT / "inbox", alias="DESK_INBOX_DIR")
    archive_dir: Path = Field(default=REPO_ROOT / "archive", alias="DESK_ARCHIVE_DIR")
    seed_dir: Path = Field(default=REPO_ROOT / "docs" / "seed", alias="DESK_SEED_DIR")
    prompts_dir: Path = Field(default=REPO_ROOT / "prompts", alias="DESK_PROMPTS_DIR")
    tz: str = Field(default="Europe/Berlin", alias="TZ")

    # behaviour
    daily_job_hour: int = Field(default=7, alias="DESK_DAILY_HOUR")
    daily_job_minute: int = Field(default=0, alias="DESK_DAILY_MINUTE")
    backup_hour: int = Field(default=2, alias="DESK_BACKUP_HOUR")
    backups_to_keep: int = Field(default=30, alias="DESK_BACKUPS_KEEP")
    inbox_scan_minutes: int = Field(default=5, alias="DESK_INBOX_SCAN_MINUTES")
    price_lookback_days: int = Field(default=400, alias="DESK_PRICE_LOOKBACK_DAYS")
    alphavantage_daily_budget: int = Field(default=25, alias="DESK_ALPHAVANTAGE_BUDGET")
    http_timeout_s: float = Field(default=20.0, alias="DESK_HTTP_TIMEOUT")
    scheduler_enabled: bool = Field(default=True, alias="DESK_SCHEDULER_ENABLED")
    # The brief names claude-sonnet-4-5 "or whatever is current"; Sonnet 5 is the current Sonnet.
    claude_model: str = Field(default="claude-sonnet-5", alias="DESK_CLAUDE_MODEL")
    # execution (docs/BRIEF.md 8b): paper by default; live adapters are stubs and need DESK_LIVE=1 as well
    broker: str = Field(default="paper", alias="DESK_BROKER")
    live: bool = Field(default=False, alias="DESK_LIVE")
    fundamentals_weekday: int = Field(default=6, alias="DESK_FUNDAMENTALS_WEEKDAY")  # 6 = Sunday
    fundamentals_hour: int = Field(default=6, alias="DESK_FUNDAMENTALS_HOUR")
    screener_refresh_day: int = Field(default=1, alias="DESK_SCREENER_REFRESH_DAY")  # day of month
    # phase 4: weekly digest and optional Claude-written reasoning
    digest_weekday: int = Field(default=0, alias="DESK_DIGEST_WEEKDAY")  # 0 = Monday
    digest_hour: int = Field(default=7, alias="DESK_DIGEST_HOUR")
    digest_minute: int = Field(default=30, alias="DESK_DIGEST_MINUTE")
    digest_to: str | None = Field(default=None, alias="DESK_DIGEST_TO")
    digest_from: str | None = Field(default=None, alias="DESK_DIGEST_FROM")
    smtp_host: str | None = Field(default=None, alias="DESK_SMTP_HOST")
    smtp_port: int = Field(default=587, alias="DESK_SMTP_PORT")
    smtp_user: str | None = Field(default=None, alias="DESK_SMTP_USER")
    smtp_pass: str | None = Field(default=None, alias="DESK_SMTP_PASS")
    smtp_starttls: bool = Field(default=True, alias="DESK_SMTP_STARTTLS")
    llm_reasoning: bool = Field(default=False, alias="DESK_LLM_REASONING")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "desk.sqlite3"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def kill_file(self) -> Path:
        return self.data_dir / "KILL"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def reports_inbox(self) -> Path:
        return self.inbox_dir

    @property
    def portfolio_inbox(self) -> Path:
        return self.inbox_dir / "portfolio"

    @property
    def reports_archive(self) -> Path:
        return self.archive_dir / "reports"

    @property
    def portfolio_archive(self) -> Path:
        return self.archive_dir / "portfolio"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.cache_dir,
            self.backup_dir,
            self.reports_inbox,
            self.portfolio_inbox,
            self.reports_archive,
            self.portfolio_archive,
        ):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
