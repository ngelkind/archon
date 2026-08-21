-- Migration 14: per-tenant Telegram USERBOT (BYO account, consent-gated).
--
-- Distinct from `telegram_links` (migration 011/012), which is the official
-- Business-bot integration and stays in place. This is the MTProto/Telethon
-- path: the user logs in with their own phone number, giving full access to
-- groups, channels and history — everything the Business bot cannot reach.
--
-- RISK, STATED ACCURATELY (verified against core.telegram.org, not assumed):
-- Telegram *welcomes* third-party clients — "We welcome all developers to use
-- our API and source code to create Telegram-like messaging applications on
-- our platform free of charge" (API ToS). So this is NOT a terms violation the
-- way the WhatsApp linked-device client is. What IS documented:
--   * "all accounts that log in using unofficial Telegram API clients are
--     automatically put under observation to avoid violations of the Terms of
--     Service"
--   * "If you use the Telegram API for flooding, spamming, faking subscriber
--     and view counters of channels, you will be banned forever"
-- The consent text reflects exactly that and no more. Overstating it would
-- devalue the WhatsApp warning, which is the one that really does describe a
-- ToS breach.
--
-- The StringSession IS the account: whoever holds it can read and send as that
-- person, and it is not scoped or revocable per-app. It is therefore stored
-- ONLY as a tenant-bound AEAD envelope, never in the clear.
CREATE TABLE IF NOT EXISTS telegram_userbot_links (
    id                      INTEGER PRIMARY KEY,
    tenant_id               INTEGER NOT NULL REFERENCES users(id) DEFAULT 0,
    phone                   TEXT,               -- E.164, as entered by the user
    tg_user_id              TEXT,               -- filled in once login succeeds
    tg_username             TEXT,
    session_envelope        TEXT,               -- AES-256-GCM StringSession
    -- Telethon's phone_code_hash from the send-code step; needed to complete
    -- the login and useless afterwards, so it is cleared on success.
    login_hash_envelope     TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending',
                            -- pending | code_sent | active | password_required
                            -- | failed | logged_out | banned
    consent_version         TEXT NOT NULL,
    consent_acknowledged_at TEXT NOT NULL,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    logged_in_at            TEXT,
    last_status_at          TEXT,
    last_error              TEXT,
    revoked_at              TEXT
);

-- One live userbot link per tenant; revoked rows are kept as consent history.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_userbot_tenant
    ON telegram_userbot_links (tenant_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_tg_userbot_status
    ON telegram_userbot_links (status);
