-- Contact directory: name <-> phone, imported from a Google Contacts CSV plus
-- owner-taught aliases. One person may have several rows (aliases in different
-- languages / multiple numbers). `norm` is the transliterated match key.
CREATE TABLE IF NOT EXISTS contacts (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL,
    norm       TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'import',   -- 'import' | 'alias'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name, phone)
);
CREATE INDEX IF NOT EXISTS idx_contacts_norm  ON contacts(norm);
CREATE INDEX IF NOT EXISTS idx_contacts_phone ON contacts(phone);
