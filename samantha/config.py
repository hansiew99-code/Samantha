"""Settings loaded from environment / .env.

Every integration is optional: an unset credential disables that service and the
rest of the daemon runs normally. Only Anthropic + Telegram are needed day one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from dotenv import load_dotenv


def _parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


def _parse_quiet_hours(value: str) -> tuple[time, time]:
    start, end = value.split("-")
    return _parse_hhmm(start), _parse_hhmm(end)


@dataclass
class Settings:
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: int = 0

    google_credentials_path: Path = Path("google_credentials.json")
    google_token_path: Path = Path("google_token.json")
    slack_bot_token: str = ""
    slack_app_token: str = ""
    clickup_api_token: str = ""
    clickup_team_id: str = ""

    db_path: Path = Path("samantha.db")
    timezone: str = "Asia/Kuala_Lumpur"
    daily_budget_usd: float = 0.177
    quiet_hours: tuple[time, time] = field(default_factory=lambda: (time(23, 0), time(7, 0)))
    morning_digest: time = field(default_factory=lambda: time(7, 30))
    evening_digest: time = field(default_factory=lambda: time(21, 30))
    dry_run: bool = False

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def anthropic_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def google_enabled(self) -> bool:
        return self.google_token_path.exists()

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_bot_token and self.slack_app_token)

    @property
    def clickup_enabled(self) -> bool:
        return bool(self.clickup_api_token and self.clickup_team_id)

    def summary(self) -> str:
        lines = [
            f"db: {self.db_path}",
            f"timezone: {self.timezone}",
            f"daily budget: ${self.daily_budget_usd:.3f}",
            f"dry run: {self.dry_run}",
            f"anthropic: {'ok' if self.anthropic_enabled else 'MISSING'}",
            f"telegram: {'ok' if self.telegram_enabled else 'MISSING'}",
            f"google: {'ok' if self.google_enabled else 'disabled'}",
            f"slack: {'ok' if self.slack_enabled else 'disabled'}",
            f"clickup: {'ok' if self.clickup_enabled else 'disabled'}",
        ]
        return "\n".join(lines)


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file)
    env = os.environ
    return Settings(
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
        telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=int(env.get("TELEGRAM_CHAT_ID", "0") or 0),
        google_credentials_path=Path(env.get("GOOGLE_CREDENTIALS_PATH", "google_credentials.json")),
        google_token_path=Path(env.get("GOOGLE_TOKEN_PATH", "google_token.json")),
        slack_bot_token=env.get("SLACK_BOT_TOKEN", ""),
        slack_app_token=env.get("SLACK_APP_TOKEN", ""),
        clickup_api_token=env.get("CLICKUP_API_TOKEN", ""),
        clickup_team_id=env.get("CLICKUP_TEAM_ID", ""),
        db_path=Path(env.get("SAMANTHA_DB_PATH", "samantha.db")),
        timezone=env.get("SAMANTHA_TIMEZONE", "Asia/Kuala_Lumpur"),
        daily_budget_usd=float(env.get("SAMANTHA_DAILY_BUDGET_USD", "0.177")),
        quiet_hours=_parse_quiet_hours(env.get("SAMANTHA_QUIET_HOURS", "23:00-07:00")),
        morning_digest=_parse_hhmm(env.get("SAMANTHA_MORNING_DIGEST", "07:30")),
        evening_digest=_parse_hhmm(env.get("SAMANTHA_EVENING_DIGEST", "21:30")),
        dry_run=env.get("SAMANTHA_DRY_RUN", "0") in ("1", "true", "yes"),
    )
