-- Migration 8: multi-tenancy — every per-user row belongs to exactly one tenant.
--
-- Numbered 008, not 007: 007_users.sql (the parked auth layer) already created
-- `users`. That table IS the tenant identity here — tenant_id is a users.id —
-- so auth and tenancy cannot disagree about who someone is.
--
-- WHY SIX TABLES ARE REBUILT RATHER THAN ALTERED
-- Adding a tenant_id column is the easy half. The dangerous half is that six
-- tables carry UNIQUE/PRIMARY KEY constraints that are *globally* scoped, and
-- SQLite cannot drop or redefine the implicit index behind a UNIQUE/PK
-- constraint ("index associated with UNIQUE or PRIMARY KEY constraint cannot be
-- dropped"). Left alone they are silent cross-tenant corruption, not cosmetic:
--   settings.key PK          -> tenant B's llm.active_provider overwrites A's
--   gmail_state.key PK       -> tenant B's sync cursor overwrites A's
--   chats UNIQUE(platform,chat_id)          -> two tenants cannot both be in
--                               the same group; chat_upsert's ON CONFLICT DO
--                               UPDATE would rewrite the OTHER tenant's row
--   messages UNIQUE(platform,chat_id,msg_id)-> same message id collides
--   contacts UNIQUE(name,phone)             -> two tenants cannot know one person
--   personas.name UNIQUE                    -> one "work" persona per PRODUCT
-- So those six are rebuilt with the tenant in the key. The rest only gain a
-- column + index.
--
-- tenant_id is NOT NULL REFERENCES users(id) DEFAULT 0, and 0 is deliberately
-- not a real user: an INSERT that forgets the tenant hits a foreign-key
-- violation and fails LOUDLY, rather than silently filing the row under the
-- owner. Pre-existing single-user rows are backfilled to tenant 1.
--
-- FK enforcement is disabled only for the rebuild window (the standard SQLite
-- table-rebuild procedure) and restored at the end.

PRAGMA foreign_keys=off;

BEGIN;

-- Tenant 1 = the original single-user owner; all pre-existing data is theirs.
-- It is not a login: the password hash is unverifiable by construction and
-- disabled_at is set, and both user lookups filter on disabled_at IS NULL.
INSERT OR IGNORE INTO users (id, email, password_hash, display_name, disabled_at)
VALUES (1, 'owner@archon.local', 'x-no-login-single-user-owner',
        'Owner (single-user)', datetime('now'));


-- ---------------------------------------------------------------- settings --
CREATE TABLE settings_new (
    tenant_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, key)
);
INSERT INTO settings_new (tenant_id, key, value_json, updated_at)
    SELECT 1, key, value_json, updated_at FROM settings;
DROP TABLE settings;
ALTER TABLE settings_new RENAME TO settings;


-- ------------------------------------------------------------- gmail_state --
CREATE TABLE gmail_state_new (
    tenant_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, key)
);
INSERT INTO gmail_state_new (tenant_id, key, value)
    SELECT 1, key, value FROM gmail_state;
DROP TABLE gmail_state;
ALTER TABLE gmail_state_new RENAME TO gmail_state;


-- ---------------------------------------------------------------- personas --
-- Rebuilt before chats so the FK target exists with its final shape.
CREATE TABLE personas_new (
    id             INTEGER PRIMARY KEY,
    tenant_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    system_prompt  TEXT NOT NULL,
    model_override TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, name)
);
INSERT INTO personas_new (id, tenant_id, name, system_prompt, model_override, created_at)
    SELECT id, 1, name, system_prompt, model_override, created_at FROM personas;
DROP TABLE personas;
ALTER TABLE personas_new RENAME TO personas;
CREATE INDEX IF NOT EXISTS idx_personas_tenant ON personas (tenant_id);


-- ------------------------------------------------------------------- chats --
CREATE TABLE chats_new (
    id                INTEGER PRIMARY KEY,
    tenant_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform          TEXT NOT NULL,             -- 'wa' | 'tg' | 'gmail'
    chat_id           TEXT NOT NULL,             -- JID / tg id / email address
    name              TEXT,
    kind              TEXT NOT NULL,             -- 'private' | 'group' | 'channel' | 'email'
    is_whitelisted    INTEGER NOT NULL DEFAULT 0,
    auto_reply        INTEGER NOT NULL DEFAULT 0,
    image_recognition INTEGER NOT NULL DEFAULT 0,
    send_policy       TEXT NOT NULL DEFAULT 'confirm',  -- 'free' | 'confirm'
    delay_policy_json TEXT,
    persona_id        INTEGER REFERENCES personas(id) ON DELETE SET NULL,
    log_deletes       INTEGER NOT NULL DEFAULT 1,
    last_seen_at      TEXT,
    capture_media     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (tenant_id, platform, chat_id)
);
INSERT INTO chats_new (id, tenant_id, platform, chat_id, name, kind, is_whitelisted,
                       auto_reply, image_recognition, send_policy, delay_policy_json,
                       persona_id, log_deletes, last_seen_at, capture_media)
    SELECT id, 1, platform, chat_id, name, kind, is_whitelisted, auto_reply,
           image_recognition, send_policy, delay_policy_json, persona_id,
           log_deletes, last_seen_at, capture_media FROM chats;
DROP TABLE chats;
ALTER TABLE chats_new RENAME TO chats;
CREATE INDEX IF NOT EXISTS idx_chats_tenant ON chats (tenant_id);


-- ---------------------------------------------------------------- messages --
CREATE TABLE messages_new (
    id           INTEGER PRIMARY KEY,
    tenant_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chat_pk      INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    platform     TEXT NOT NULL,
    chat_id      TEXT NOT NULL,                  -- denormalized for the unique index
    msg_id       TEXT NOT NULL,
    source       TEXT NOT NULL,
    sender_id    TEXT,
    sender_name  TEXT,
    is_from_me   INTEGER NOT NULL DEFAULT 0,
    ts           TEXT NOT NULL,
    text         TEXT,
    media_path   TEXT,
    edited_text  TEXT,
    edited_at    TEXT,
    deleted_at   TEXT,
    raw_json     TEXT,
    UNIQUE (tenant_id, platform, chat_id, msg_id)
);
INSERT INTO messages_new (id, tenant_id, chat_pk, platform, chat_id, msg_id, source,
                          sender_id, sender_name, is_from_me, ts, text, media_path,
                          edited_text, edited_at, deleted_at, raw_json)
    SELECT id, 1, chat_pk, platform, chat_id, msg_id, source, sender_id, sender_name,
           is_from_me, ts, text, media_path, edited_text, edited_at, deleted_at,
           raw_json FROM messages;
DROP TABLE messages;
ALTER TABLE messages_new RENAME TO messages;
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages (chat_pk, ts);
CREATE INDEX IF NOT EXISTS idx_messages_tenant ON messages (tenant_id);


-- ---------------------------------------------------------------- contacts --
CREATE TABLE contacts_new (
    id         INTEGER PRIMARY KEY,
    tenant_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL,
    norm       TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'import',   -- 'import' | 'alias'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, name, phone)
);
INSERT INTO contacts_new (id, tenant_id, name, phone, norm, source, created_at)
    SELECT id, 1, name, phone, norm, source, created_at FROM contacts;
DROP TABLE contacts;
ALTER TABLE contacts_new RENAME TO contacts;
CREATE INDEX IF NOT EXISTS idx_contacts_norm   ON contacts (norm);
CREATE INDEX IF NOT EXISTS idx_contacts_phone  ON contacts (phone);
CREATE INDEX IF NOT EXISTS idx_contacts_tenant ON contacts (tenant_id);


-- === Column-only tables: no globally-scoped UNIQUE/PK to re-key ============
-- DEFAULT 0 is an intentional tripwire: 0 is not a users.id, so a write that
-- forgets the tenant fails the foreign key instead of landing in tenant 1.

ALTER TABLE context_messages  ADD COLUMN tenant_id INTEGER NOT NULL REFERENCES users(id) DEFAULT 0;
ALTER TABLE pending_actions   ADD COLUMN tenant_id INTEGER NOT NULL REFERENCES users(id) DEFAULT 0;
ALTER TABLE scheduled_messages ADD COLUMN tenant_id INTEGER NOT NULL REFERENCES users(id) DEFAULT 0;
ALTER TABLE pending_replies   ADD COLUMN tenant_id INTEGER NOT NULL REFERENCES users(id) DEFAULT 0;
ALTER TABLE llm_calls         ADD COLUMN tenant_id INTEGER NOT NULL REFERENCES users(id) DEFAULT 0;
ALTER TABLE events_created    ADD COLUMN tenant_id INTEGER NOT NULL REFERENCES users(id) DEFAULT 0;
ALTER TABLE sub_bots          ADD COLUMN tenant_id INTEGER NOT NULL REFERENCES users(id) DEFAULT 0;
ALTER TABLE api_devices       ADD COLUMN tenant_id INTEGER NOT NULL REFERENCES users(id) DEFAULT 0;

UPDATE context_messages   SET tenant_id = 1;
UPDATE pending_actions    SET tenant_id = 1;
UPDATE scheduled_messages SET tenant_id = 1;
UPDATE pending_replies    SET tenant_id = 1;
UPDATE llm_calls          SET tenant_id = 1;
UPDATE events_created     SET tenant_id = 1;
UPDATE sub_bots           SET tenant_id = 1;
UPDATE api_devices        SET tenant_id = 1;

CREATE INDEX IF NOT EXISTS idx_context_tenant   ON context_messages (tenant_id);
CREATE INDEX IF NOT EXISTS idx_pending_actions_tenant ON pending_actions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_sched_tenant     ON scheduled_messages (tenant_id);
CREATE INDEX IF NOT EXISTS idx_pending_replies_tenant ON pending_replies (tenant_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_tenant ON llm_calls (tenant_id);
CREATE INDEX IF NOT EXISTS idx_events_tenant    ON events_created (tenant_id);
CREATE INDEX IF NOT EXISTS idx_sub_bots_tenant  ON sub_bots (tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_devices_tenant ON api_devices (tenant_id);

-- Deliberately NOT tenanted:
--   schema_version, users, refresh_tokens (already carries user_id),
--   api_pair_codes (single-owner device pairing; hashes are globally unique),
--   audit (process-wide system log; in multitenant mode audit_store_content
--          defaults to False so third-party message text is not written).

COMMIT;

PRAGMA foreign_keys=on;
