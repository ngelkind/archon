-- Archon schema v1. Applied by migrations.py; never edit applied migrations,
-- add numbered follow-ups instead.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

-- Runtime-changeable settings (the bot is the only settings UI).
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every chat Archon has ever seen, with its per-chat policies.
CREATE TABLE IF NOT EXISTS chats (
    id                INTEGER PRIMARY KEY,
    platform          TEXT NOT NULL,             -- 'wa' | 'tg' | 'gmail'
    chat_id           TEXT NOT NULL,             -- JID / tg id / email address
    name              TEXT,
    kind              TEXT NOT NULL,             -- 'private' | 'group' | 'channel' | 'email'
    is_whitelisted    INTEGER NOT NULL DEFAULT 0,
    auto_reply        INTEGER NOT NULL DEFAULT 0,
    image_recognition INTEGER NOT NULL DEFAULT 0,
    send_policy       TEXT NOT NULL DEFAULT 'confirm',  -- 'free' | 'confirm'
    delay_policy_json TEXT,                      -- {"mode":"none"|"fixed"|"random","min_s":..,"max_s":..}
    persona_id        INTEGER REFERENCES personas(id) ON DELETE SET NULL,
    log_deletes       INTEGER NOT NULL DEFAULT 1,
    last_seen_at      TEXT,
    UNIQUE (platform, chat_id)
);

-- Message cache: enables deleted/edited before-after diffs and history tools.
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    chat_pk      INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    platform     TEXT NOT NULL,
    chat_id      TEXT NOT NULL,                  -- denormalized for the unique index
    msg_id       TEXT NOT NULL,
    source       TEXT NOT NULL,                  -- 'business' | 'userbot' | 'wa' | 'gmail' | 'subbot'
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
    UNIQUE (platform, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages (chat_pk, ts);

-- Calendar events Archon created, linked to the message that triggered them.
CREATE TABLE IF NOT EXISTS events_created (
    id            INTEGER PRIMARY KEY,
    chat_pk       INTEGER REFERENCES chats(id) ON DELETE SET NULL,
    source_msg_id TEXT,
    gcal_event_id TEXT NOT NULL,
    calendar_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    start_ts      TEXT,
    end_ts        TEXT,
    status        TEXT NOT NULL DEFAULT 'created', -- 'created' | 'cancelled'
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS personas (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    system_prompt TEXT NOT NULL,
    model_override TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Rolling agent conversation history per (chat, persona). persona_id NULL =
-- the chat's default context.
CREATE TABLE IF NOT EXISTS context_messages (
    id         INTEGER PRIMARY KEY,
    chat_pk    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    persona_id INTEGER REFERENCES personas(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,                    -- 'user' | 'assistant' | 'tool'
    content    TEXT NOT NULL,
    ts         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_context_chat ON context_messages (chat_pk, persona_id, id);

-- Confirm/disambiguate flow: one row per inline-keyboard question to the owner.
CREATE TABLE IF NOT EXISTS pending_actions (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,                  -- 'send' | 'event' | 'setting' | ...
    payload_json TEXT NOT NULL,
    chat_pk      INTEGER REFERENCES chats(id) ON DELETE CASCADE,
    owner_msg_id INTEGER,                        -- control-bot message carrying the keyboard
    status       TEXT NOT NULL DEFAULT 'pending',-- 'pending' | 'approved' | 'rejected' | 'expired'
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_messages (
    id           INTEGER PRIMARY KEY,
    platform     TEXT NOT NULL,
    chat_pk      INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    text         TEXT,
    media_path   TEXT,
    due_at       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending', -- pending|delegated_native|sent|failed|cancelled
    tg_native_id INTEGER,                         -- Telethon scheduled-message id when delegated
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    result       TEXT
);
CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_messages (status, due_at);

-- Replies held back by a per-chat delay policy.
CREATE TABLE IF NOT EXISTS pending_replies (
    id         INTEGER PRIMARY KEY,
    chat_pk    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    draft_text TEXT NOT NULL,
    due_at     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending'   -- pending|sent|cancelled|superseded
);

-- Every LLM call ever made, with cost. claude_code rows carry cost_usd = 0.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                 INTEGER PRIMARY KEY,
    ts                 TEXT NOT NULL DEFAULT (datetime('now')),
    purpose            TEXT NOT NULL,            -- 'triage' | 'agent' | 'vision' | 'persona_chat' | 'debug'
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    in_tokens          INTEGER NOT NULL DEFAULT 0,
    out_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    tool_call_count    INTEGER NOT NULL DEFAULT 0,
    latency_ms         INTEGER,
    ok                 INTEGER NOT NULL DEFAULT 1,
    chat_pk            INTEGER REFERENCES chats(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls (ts);

CREATE TABLE IF NOT EXISTS sub_bots (
    id             INTEGER PRIMARY KEY,
    token          TEXT NOT NULL,
    bot_username   TEXT NOT NULL,
    platform_scope TEXT NOT NULL,                -- 'wa' | 'tg' | 'gmail'
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only mirror of the JSONL audit log for queryability from bot tools.
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    actor       TEXT NOT NULL,                   -- 'gate' | 'tool' | 'owner' | 'system'
    action      TEXT NOT NULL,
    detail_json TEXT
);

-- Gmail incremental sync state (last historyId etc.).
CREATE TABLE IF NOT EXISTS gmail_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
