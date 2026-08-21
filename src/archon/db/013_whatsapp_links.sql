-- Migration 13: per-tenant WhatsApp (BYO, consent-gated).
--
-- WhatsApp has no official API for personal accounts. This connects as a linked
-- companion device (whatsmeow/neonize), which violates WhatsApp's Terms of
-- Service and carries a REAL risk of the user's number being permanently
-- banned. The product owner accepted that risk deliberately; the job of this
-- schema is to make the risk *recorded and consented*, not to make it look
-- safe.
--
-- Hence consent is a first-class column, not a boolean flag: we store WHICH
-- version of the warning text the user acknowledged and when. If the warning is
-- ever reworded, existing rows still say what that user actually agreed to,
-- which is the only thing that makes a consent record worth keeping.
--
-- session_envelope holds the linked-device session, encrypted per tenant with
-- the same AEAD as Google/Telegram credentials. A WhatsApp session is a
-- full account credential: whoever holds it can read and send as that person.
-- It is written back encrypted whenever the session is evicted, and the
-- plaintext working file is removed. NOTE: while a session is LIVE the working
-- file is necessarily plaintext on disk, because the Go client owns the handle
-- — volume encryption is the mitigation for that window, not this column.
CREATE TABLE IF NOT EXISTS whatsapp_links (
    id                      INTEGER PRIMARY KEY,
    tenant_id               INTEGER NOT NULL REFERENCES users(id) DEFAULT 0,
    phone_jid               TEXT,              -- filled in once pairing succeeds
    status                  TEXT NOT NULL DEFAULT 'pending',
                            -- pending | paired | logged_out | banned | failed
    session_envelope        TEXT,              -- AES-256-GCM, tenant-bound
    consent_version         TEXT NOT NULL,     -- which warning they accepted
    consent_acknowledged_at TEXT NOT NULL,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    paired_at               TEXT,
    last_status_at          TEXT,
    last_error              TEXT,
    revoked_at              TEXT
);

-- One live WhatsApp link per tenant; a revoked row is kept for the consent
-- history and does not block re-linking.
CREATE UNIQUE INDEX IF NOT EXISTS idx_wa_links_tenant
    ON whatsapp_links (tenant_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_wa_links_status ON whatsapp_links (status);
