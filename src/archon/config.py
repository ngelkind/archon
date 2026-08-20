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

from pydantic import Field, field_validator
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

    # --- Control API (Android app; off by default) ---
    # Bind to the loopback/tunnel interface only — NEVER 0.0.0.0. On the VM this
    # is the WireGuard interface address so the API is unreachable off-tunnel.
    api_enabled: bool = False
    api_bind_host: str = "127.0.0.1"
    api_port: int = 8787
    # Server-side pepper mixed into every device-token / pair-code hash so a
    # stolen DB yields no usable tokens. MUST be overridden with a strong random
    # value in production (API_TOKEN_PEPPER); the default is a dev placeholder.
    api_token_pepper: str = "dev-insecure-pepper-change-me"

    # --- Push (self-hosted ntfy / UnifiedPush; off by default) ---
    # Base URL of the ntfy instance, e.g. http://10.8.0.1:8080. Empty = push
    # disabled and every push call is a cheap no-op. Payloads are content-free
    # by design: the app fetches details over the tunnel.
    ntfy_base_url: str = ""

    @field_validator("tg_log_channel_id", "telegram_api_id", "telethon_session",
                     "telegram_api_hash", "anthropic_api_key", "openai_api_key",
                     "gemini_api_key", "openrouter_api_key", mode="before")
    @classmethod
    def _empty_env_is_none(cls, v):
        # ``KEY=`` lines in .env arrive as empty strings; treat them as unset.
        if isinstance(v, str) and not v.strip():
            return None
        return v

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
