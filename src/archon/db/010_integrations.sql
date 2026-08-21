-- Migration 10: per-tenant integration credentials.
--
-- The single-user bot keeps ONE Google token at secrets/google/token.json. A
-- multi-user product needs one per tenant, so credentials move into the
-- database — which means they must be encrypted at rest: a refresh token is a
-- long-lived key to somebody's mailbox and calendar, and a stolen .db (or a
-- backup, or a careless SELECT) would otherwise expose every user at once.
--
-- `secret_envelope` holds the AES-256-GCM envelope from crypto.py, whose
-- associated data binds the record to (tenant_id, provider). Moving a row to
-- another tenant makes it undecryptable rather than usable, so the encryption
-- backs up the tenant_id column instead of trusting it.
--
-- The owner tenant is NOT migrated into this table: the personal bot keeps
-- using its existing token.json, so the live deployment is untouched.
CREATE TABLE IF NOT EXISTS integration_credentials (
    id              INTEGER PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES users(id) DEFAULT 0,
    provider        TEXT NOT NULL,              -- 'google' (telegram/whatsapp later)
    account_label   TEXT,                       -- e.g. the Google account email
    secret_envelope TEXT NOT NULL,              -- crypto.py AES-256-GCM envelope
    scopes          TEXT,                       -- space-separated granted scopes
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at      TEXT,
    UNIQUE (tenant_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_integration_creds_tenant
    ON integration_credentials (tenant_id, provider);

-- One-shot OAuth state values. Without these the callback would accept any
-- code for any tenant: `state` is the CSRF token AND the binding that says
-- which tenant an incoming authorization code belongs to, so it is created
-- when the authorize URL is issued and consumed exactly once at the callback.
CREATE TABLE IF NOT EXISTS oauth_states (
    state      TEXT PRIMARY KEY,                -- 256-bit URL-safe random
    tenant_id  INTEGER NOT NULL REFERENCES users(id),
    provider   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
