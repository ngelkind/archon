-- Migration 12: treat the Telegram Business connection as a SECRET.
--
-- A business_connection_id is not an identifier, it is a capability: whoever
-- holds it can send messages as that user. Migration 011 stored it in the
-- clear, which put a send-as-them credential for every tenant in the database
-- next to everything else. It now gets the same treatment as a Google refresh
-- token.
--
-- It is also the ROUTING key for inbound updates, so it cannot simply be
-- encrypted — we would not be able to look it up. Same split as device tokens:
--   connection_hash  HMAC-SHA256(pepper, id)  -> indexed, used to route
--   secret_envelope  AES-256-GCM, tenant-bound -> used to send
-- The hash is not reversible and the envelope's associated data binds it to
-- its tenant, so a row copied between tenants routes nowhere and decrypts to
-- nothing.
--
-- rights_json records the BusinessBotRights the connection granted, so a send
-- can refuse up front when the user did not grant reply permission.
--
-- Rebuilt rather than altered: the plaintext column is dropped, not retained,
-- and the table has no production data (the feature has never been deployed).
-- Anyone who linked on this branch re-links; there is nothing to preserve.

PRAGMA foreign_keys=off;

BEGIN;

DROP INDEX IF EXISTS idx_tg_links_tenant;
DROP INDEX IF EXISTS idx_tg_links_user;
DROP INDEX IF EXISTS idx_tg_links_connection;

CREATE TABLE telegram_links_new (
    id              INTEGER PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES users(id) DEFAULT 0,
    tg_user_id      TEXT NOT NULL,
    tg_username     TEXT,
    tg_name         TEXT,
    connection_hash TEXT,          -- HMAC-SHA256(pepper, business_connection_id)
    secret_envelope TEXT,          -- AES-256-GCM envelope holding the id itself
    rights_json     TEXT,          -- BusinessBotRights granted by the user
    is_enabled      INTEGER NOT NULL DEFAULT 0,
    linked_at       TEXT NOT NULL DEFAULT (datetime('now')),
    connected_at    TEXT,
    revoked_at      TEXT
);

INSERT INTO telegram_links_new
    (id, tenant_id, tg_user_id, tg_username, tg_name, is_enabled, linked_at,
     connected_at, revoked_at)
SELECT id, tenant_id, tg_user_id, tg_username, tg_name, 0, linked_at,
       NULL, revoked_at
FROM telegram_links;

DROP TABLE telegram_links;
ALTER TABLE telegram_links_new RENAME TO telegram_links;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_links_tenant
    ON telegram_links (tenant_id) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_links_user
    ON telegram_links (tg_user_id) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_links_conn_hash
    ON telegram_links (connection_hash)
    WHERE connection_hash IS NOT NULL AND revoked_at IS NULL;

COMMIT;

PRAGMA foreign_keys=on;
