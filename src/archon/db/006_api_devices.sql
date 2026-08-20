-- Control-app device pairing (Phase 0). Bearer tokens and one-time pair codes
-- are stored ONLY as hashes (HMAC-SHA256 with a server-side pepper); the
-- plaintext is returned to the client once at mint time and never persisted.
-- revoked_at gives instant remote kill without deleting the audit trail.
CREATE TABLE IF NOT EXISTS api_devices (
    id            INTEGER PRIMARY KEY,
    name          TEXT,
    token_hash    TEXT NOT NULL UNIQUE,          -- HMAC-SHA256(pepper, token)
    device_pubkey TEXT,                           -- optional client public key
    push_endpoint TEXT,                           -- ntfy/UnifiedPush topic (Part C)
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT,
    revoked_at    TEXT
);

-- One-time pairing codes issued by the owner-only /pair command in the control
-- bot. Single-use (used_at) and short-lived (expires_at); hashed like tokens.
CREATE TABLE IF NOT EXISTS api_pair_codes (
    code_hash  TEXT PRIMARY KEY,                  -- HMAC-SHA256(pepper, code)
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
