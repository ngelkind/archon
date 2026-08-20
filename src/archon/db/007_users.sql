-- Multi-tenant accounts (feature-flagged behind settings.multitenant_enabled).
-- Separate from api_devices: those bearer tokens authenticate the SINGLE owner's
-- personal bot; these users are the multi-user product's accounts. Passwords are
-- stored ONLY as argon2id hashes (the hash embeds its own salt + params).
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,           -- normalized lowercase in the app layer
    password_hash TEXT NOT NULL,                  -- argon2id PHC string (salt embedded)
    display_name  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT,
    disabled_at   TEXT                             -- soft-disable without deleting history
);

-- Opaque refresh tokens, stored ONLY as hashes (HMAC-SHA256 with the server
-- pepper, same scheme as device tokens). Single-use rotation: /auth/refresh
-- revokes the presented token and issues a fresh one, so a leaked-then-reused
-- token is detectable (already revoked) and dead. revoked_at also covers logout.
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash TEXT PRIMARY KEY,                   -- HMAC-SHA256(pepper, token)
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens (user_id);
