-- Durable outbox for deleted/edited-message cards.
--
-- tglog.log_change used to render a card and send it INLINE inside the single
-- ingest consumer, behind the 1.1s global send lock — so a burst of edits
-- stalled the whole pipeline, and a flood-control 429 or a dead notifier
-- dropped the card with no way to retry. Cards are now rows here; a supervised
-- worker (logging_/logworker.py) drains them: it coalesces repeated edits of
-- one message, batches bursts into a digest, retries, and records the outcome.

CREATE TABLE IF NOT EXISTS log_outbox (
    id            INTEGER PRIMARY KEY,
    tenant_id     INTEGER NOT NULL REFERENCES users(id) DEFAULT 0,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    chat_label    TEXT,
    msg_id        TEXT NOT NULL,
    kind          TEXT NOT NULL,           -- 'edited' | 'deleted'
    sender        TEXT,
    before_text   TEXT,
    after_text    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    -- edits of the same message merge into the still-unsent row until this
    -- time, so a rapid back-and-forth becomes one card (first before, last
    -- after) instead of five.
    coalesce_until TEXT,
    sent_at       TEXT,                     -- NULL = still pending
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);

-- The worker's hot query: pending rows, oldest first.
CREATE INDEX IF NOT EXISTS ix_log_outbox_pending
    ON log_outbox (sent_at, id);
-- Coalescing lookup: an unsent card for this exact message.
CREATE INDEX IF NOT EXISTS ix_log_outbox_coalesce
    ON log_outbox (tenant_id, platform, chat_id, msg_id, sent_at);
