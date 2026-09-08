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
    # Bot API base URL override. Empty = https://api.telegram.org. The e2e
    # harness points every Bot at a local fake server it then polls for real;
    # never set this in production.
    telegram_api_base: str = ""
    telegram_owner_id: int = 0  # numeric Telegram user id of the owner
    tg_log_channel_id: int | None = None  # configurable at runtime via settings table too

    # --- Telegram userbot (M6) ---
    # SINGLE-USER: all three together are the owner's own MTProto session.
    # MULTITENANT: api_id/api_hash become APP-level credentials that every
    # tenant's login rides on (one app registration, many user accounts), and
    # are REQUIRED for the per-tenant userbot integration. telethon_session
    # stays owner-only and must NOT be set on a product deployment — it is one
    # person's account, not app configuration.
    # NOTE: Telegram rate-limits and flags at the api_id level, so a single
    # shared id across many tenants is a correlated-failure surface — see
    # integrations/telegram_userbot.py.
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telethon_session: str | None = None  # StringSession — OWNER ONLY

    # --- WhatsApp off switch ---
    # False stops the WhatsApp subsystem cleanly and hides every wa_* tool from
    # the agent; the runtime setting ``whatsapp.enabled`` can do the same
    # without a restart. The PoC must be able to turn this off in one line.
    whatsapp_enabled: bool = True

    # --- Userbot outbound pacing (see pacing.py) ---
    # Ban-avoidance budgets for per-tenant userbot sends. The GLOBAL pair is the
    # one that protects the shared TELEGRAM_API_ID: Telegram flags at the api_id
    # level, so N tenants each within their own per-tenant budget still present
    # N times that volume to one app. Deliberately conservative — a userbot
    # answering one person's chats needs very little throughput, and the cost of
    # being wrong is every tenant banned at once. 0 disables a dimension.
    userbot_sends_per_min_per_tenant: int = 8
    userbot_sends_per_hour_per_tenant: int = 60
    userbot_sends_per_min_global: int = 20
    userbot_sends_per_hour_global: int = 240
    userbot_sends_per_min_per_peer: int = 5
    # Breadth, not volume: contacting many NEW chats in an hour is the
    # bulk/broadcast signature Telegram documents as ban-worthy, and it reads as
    # spam even at a low message rate.
    userbot_new_peers_per_hour_per_tenant: int = 10
    # Randomised gap slept before every send, so replies look like typing.
    # Never refuses — the budgets above are the guard, this is the texture.
    userbot_send_gap_s_min: float = 2.0
    userbot_send_gap_s_max: float = 6.0

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

    # --- What the bot watches (owner decision 2026-09-06: monitor
    # everything by default). These are env defaults; each is also a
    # runtime setting (monitor.private_chats, monitor.groups,
    # monitor.include_own_messages, log.groups_default, gmail.include_self)
    # the owner flips from the control bot without a restart. ---
    # "all": every private chat is triaged. "whitelist": only whitelisted
    # ones (the old behaviour, which left 0 of 267 tg chats reachable).
    monitor_private_chats: str = "all"
    # Groups stay whitelist-gated for triage by default (a group is
    # noisy and every message would be an LLM call); "all" triages them.
    monitor_groups: str = "whitelist"
    # Triage the owner's OWN messages too (never auto-replied to).
    monitor_include_own_messages: bool = True
    # New/backfilled groups log edits & deletes by default.
    log_groups_default: bool = True
    # Include mail the owner sent themselves as an event source; the
    # old "-from:me" query excluded it and broke the "email yourself"
    # smoke test.
    gmail_include_self: bool = True
    # How long edits of one message merge into a single outbox card
    # before the log worker posts it (logging_/logworker.py).
    log_coalesce_seconds: float = 20.0
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
    # Also peppers multi-tenant refresh-token hashes (see api/accounts.py).
    api_token_pepper: str = "dev-insecure-pepper-change-me"

    # --- Multi-tenant accounts (product mode; off by default so the live
    # single-user deploy is unaffected). When on, server.py mounts /auth/*. ---
    multitenant_enabled: bool = False
    # HS256 signing key for short-lived access-token JWTs. MUST be overridden
    # with a strong random value in production (JWT_SECRET); dev placeholder.
    jwt_secret: str = "dev-insecure-jwt-secret-change-me"
    # Access tokens are short-lived (re-minted via refresh); refresh tokens are
    # long-lived and single-use-rotated. Both overridable via env.
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    # Rate limits applied when multitenant_enabled (the tunnel used to be the
    # rate limiter; a public listener has none). Per calling IP and per tenant,
    # over a sliding window. 0 disables that dimension.
    rate_limit_per_ip_per_min: int = 120
    rate_limit_per_tenant_per_min: int = 300
    # Unauthenticated endpoints (/auth/signup, /auth/login, /pair) get a much
    # tighter per-IP budget — these are the credential-guessing surfaces.
    rate_limit_auth_per_ip_per_min: int = 10
    # Wraps every per-tenant integration credential at rest (see crypto.py).
    # MUST be a generated 32-byte key in production (`openssl rand -hex 32`);
    # the fail-closed check refuses to boot multitenant without it.
    credential_encryption_key: str = ""

    # --- Google OAuth (per-tenant Gmail + Calendar linking) ---
    # From a Google Cloud project's OAuth client (Web application). The consent
    # screen starts as an unverified test app (<=100 users), which is fine for
    # beta; production verification + CASA is needed before general release.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # Must exactly match a redirect URI registered on that OAuth client.
    google_oauth_redirect_uri: str = ""

    # --- Telegram product bot (per-tenant Business linking) ---
    # A SEPARATE bot from the owner's personal control bot: product users
    # connect this one as their Business chatbot. Keeping them apart means a
    # product incident (rate limits, a ban, a token rotation) cannot take the
    # owner's own assistant down, and the owner's bot never appears in a
    # stranger's chat list. Empty = the product bot does not run.
    product_telegram_bot_token: str = ""
    # Its @username, used to build t.me deep links.
    telegram_bot_username: str = ""

    # --- Push (self-hosted ntfy / UnifiedPush; off by default) ---
    # Base URL of the ntfy instance, e.g. http://10.8.0.1:8080 — a tunnel-only
    # address; the broker should never be publicly reachable. Empty = push
    # disabled and every push call is a cheap no-op. Payloads carry no message
    # content by design: the app fetches details over the tunnel.
    ntfy_base_url: str = ""
    # Optional ntfy access token, sent as `Authorization: Bearer …` when the
    # instance requires auth for publishing.
    ntfy_auth_token: str = ""

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

    @property
    def store_audit_content(self) -> bool:
        """Whether message text may be written to the audit log.

        Single-user: the owner's own data on the owner's own box — keep the
        configured value. Multi-tenant: this log is shared across tenants, so
        third-party message content must not land in it. Default OFF, and an
        operator has to say ``AUDIT_STORE_CONTENT=true`` on purpose to change
        that for a multi-user deployment.
        """
        if self.multitenant_enabled and "audit_store_content" not in self.model_fields_set:
            return False
        return self.audit_store_content


#: Secrets that must not keep their in-repo placeholder once the product is
#: publicly reachable. name -> (attribute, insecure default)
_REQUIRED_MULTITENANT_SECRETS = (
    ("API_TOKEN_PEPPER", "api_token_pepper", "dev-insecure-pepper-change-me"),
    ("JWT_SECRET", "jwt_secret", "dev-insecure-jwt-secret-change-me"),
    # No placeholder: an unset value must fail too, because every tenant's
    # Google refresh token is encrypted with it.
    ("CREDENTIAL_ENCRYPTION_KEY", "credential_encryption_key", None),
)


class InsecureConfigError(RuntimeError):
    """A placeholder secret was left in place on a publicly-reachable config."""


def check_production_secrets(s: Settings) -> None:
    """Fail CLOSED when multi-tenant mode is on but secrets are still defaults.

    In single-user mode the API is bound to loopback/WireGuard, so a placeholder
    pepper is survivable. Multi-tenant means a public listener, where a known
    pepper lets anyone forge a device token and a known JWT secret lets anyone
    mint an access token for any account. Refusing to boot is the only safe
    behaviour — a warning would be ignored exactly once, in production.
    """
    if not s.multitenant_enabled:
        return
    bad = [
        env for env, attr, placeholder in _REQUIRED_MULTITENANT_SECRETS
        if not str(getattr(s, attr) or "").strip()
        or str(getattr(s, attr)).strip() == placeholder
    ]
    if bad:
        raise InsecureConfigError(
            "multitenant_enabled=true requires strong secrets; still unset or at "
            f"the in-repo default: {', '.join(bad)}. Generate with "
            "`openssl rand -hex 32` and set them in the environment."
        )


def load_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    check_production_secrets(s)
    return s
