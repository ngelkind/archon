-- Migration 9: tenant-scope the audit log.
--
-- The audit trail records gate decisions and every tool execution, and for
-- `sensitive` tools it records the ARGUMENTS — recipients, message bodies,
-- contact details. In a single-user deployment that is the owner's own data in
-- the owner's own log. In a multi-user product it is one shared table holding
-- everybody's, which is a data-protection problem no matter what
-- `audit_store_content` is set to (that flag governs message text, not tool
-- args).
--
-- tenant_id is NULLABLE here, unlike every other tenanted table, and the NULL
-- is meaningful rather than sloppy: some audit rows genuinely belong to the
-- process rather than to a person — startup, subsystem_crash, migration notes.
-- Those are written as SYSTEM rows (NULL) and are readable by the owner, not by
-- product tenants. A DEFAULT-0-plus-FK tripwire would be wrong here: it would
-- make legitimate system logging fail.
--
-- Existing rows backfill to tenant 1: on the live single-user box every audit
-- row that exists today IS the owner's.

ALTER TABLE audit ADD COLUMN tenant_id INTEGER REFERENCES users(id);

UPDATE audit SET tenant_id = 1;

CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit (tenant_id, id);
