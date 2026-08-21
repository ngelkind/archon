-- Migration 11: per-tenant Telegram linking.
--
-- Product model (differs from Google's): there is ONE product bot, and each
-- user connects it as *their* Telegram Business chatbot. Telegram then sends us
-- that user's private-chat messages tagged with a `business_connection_id`, and
-- lets us reply as them. So the mapping we need is
-- business_connection_id -> tenant, and it is Telegram that issues the id.
--
-- Establishing WHICH tenant a connection belongs to needs one hop, because the
-- Business connection update only tells us a Telegram user id — nothing about
-- our accounts. So: the app issues a short code, the user sends it to the bot
-- (proving control of that Telegram account), and that binds tg_user_id ->
-- tenant. When they later connect the bot in Business settings, the incoming
-- user id resolves through that binding.
--
-- The same row is also the Bot API "front door": a linked user DMing the
-- product bot is recognised by tg_user_id without any Business connection.
CREATE TABLE IF NOT EXISTS telegram_links (
    id                     INTEGER PRIMARY KEY,
    tenant_id              INTEGER NOT NULL REFERENCES users(id) DEFAULT 0,
    tg_user_id             TEXT NOT NULL,        -- numeric Telegram user id
    tg_username            TEXT,
    tg_name                TEXT,
    business_connection_id TEXT,                 -- set once they connect the bot
    is_enabled             INTEGER NOT NULL DEFAULT 0,
    linked_at              TEXT NOT NULL DEFAULT (datetime('now')),
    connected_at           TEXT,
    revoked_at             TEXT
);

-- One live Telegram identity per tenant, and one tenant per Telegram identity:
-- partial uniques so a revoked link can be replaced by a fresh one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_links_tenant
    ON telegram_links (tenant_id) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_links_user
    ON telegram_links (tg_user_id) WHERE revoked_at IS NULL;
-- The routing key for every inbound business message.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_links_connection
    ON telegram_links (business_connection_id)
    WHERE business_connection_id IS NOT NULL AND revoked_at IS NULL;

-- Short-lived, single-use codes handed to the app and typed into the bot.
-- Stored only as hashes, like device tokens and pair codes.
CREATE TABLE IF NOT EXISTS telegram_link_codes (
    code_hash  TEXT PRIMARY KEY,
    tenant_id  INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
