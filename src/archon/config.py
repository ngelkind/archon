"""Central configuration.

Everything comes from the environment (or a local ``.env`` during development).
Secrets are files/env only — never in git, never in the database except where a
feature explicitly needs runtime-managed keys (sub-bot tokens, provider keys
added through the bot; those live in the DB on the VM only).

Paths default to ``./data`` next to the repo for Windows development and are
overridden by env vars on the VM (``/opt/archon/data`` etc.).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram control bot (required for anything to work) ---
    telegram_bot_token: str = ""
    telegram_owner_id: int = 0  # numeric Telegram user id of the owner
    tg_log_channel_id: int | None = None  # configurable at runtime via settings table too

    # --- Telegram userbot (M6) ---
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telethon_session: str | None = None  # StringSession

    # --- LLM provider keys (only the active provider's key must be present) ---
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    openrouter_api_key: str | None = None

    # --- Paths ---
    archon_data: Path = Field(default=_REPO_ROOT / "data")
    archon_secrets: Path = Field(default=_REPO_ROOT / "secrets")

    # --- Behavior defaults (runtime-overridable via the settings DB table) ---
    llm_active_provider: str = "gemini"  # anthropic | openai | gemini | openrouter | claude_code
    llm_daily_budget_usd: float = 3.0
    audit_store_content: bool = True
    timezone: str = "Asia/Jerusalem"
    gmail_poll_seconds: int = 90

    @property
    def db_path(self) -> Path:
        return self.archon_data / "archon.db"

    @property
    def media_dir(self) -> Path:
        return self.archon_data / "media"

    @property
    def audit_log_path(self) -> Path:
        return self.archon_data / "audit.jsonl"

    @property
    def wa_session_path(self) -> Path:
        return self.archon_secrets / "wa" / "session.db"

    @property
    def google_client_secret_path(self) -> Path:
        return self.archon_secrets / "google" / "client_secret.json"

    @property
    def google_token_path(self) -> Path:
        return self.archon_secrets / "google" / "token.json"

    def ensure_dirs(self) -> None:
        for p in (self.archon_data, self.media_dir, self.archon_secrets):
            p.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
