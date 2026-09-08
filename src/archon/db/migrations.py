"""Schema migrations.

Migration 1 is ``schema.sql``. Later migrations are ``NNN_*.sql`` files in
this directory, applied in order at service start. Never edit an applied
migration; add a new file.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import Db

_HERE = Path(__file__).parent


def _current_version(db: Db) -> int:
    try:
        row = db.query_one("SELECT version FROM schema_version LIMIT 1")
    except Exception:
        return 0
    return int(row["version"]) if row else 0


def _migration_files() -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = [(1, _HERE / "schema.sql")]
    for f in sorted(_HERE.glob("[0-9][0-9][0-9]_*.sql")):
        m = re.match(r"^(\d{3})_", f.name)
        if m:
            out.append((int(m.group(1)), f))
    return sorted(out)


def backfill_monitor_defaults(rt) -> int:
    """One-time: turn on edit/delete logging for the owner's existing group and
    channel chats, so the monitor-everything default reaches rows already on
    record (new rows get it from chat_upsert once that default flips).

    Single-user only and idempotent. It is NOT a SQL migration on purpose: on a
    product database tenant 1 is a real signed-up user, and the safety ruling is
    that product tenants must never be silently switched to monitor-everything.
    A settings marker records that it ran; ``log.groups_default=false`` skips it.
    Returns how many rows were flipped.
    """
    from ..db import repo
    from ..db.tenancy import OWNER_TENANT_ID, TenantScope

    if rt.settings.multitenant_enabled:
        return 0
    store = TenantScope(rt.db, OWNER_TENANT_ID)
    if repo.setting_get(store, "_monitor_backfill_done", False):
        return 0
    flipped = 0
    if repo.setting_get(store, "log.groups_default", rt.settings.log_groups_default):
        cur = rt.db.execute(
            "UPDATE chats SET log_deletes = 1 "
            "WHERE tenant_id = ? AND kind IN ('group', 'channel') AND log_deletes = 0",
            (OWNER_TENANT_ID,),
        )
        flipped = cur.rowcount or 0
    repo.setting_set(store, "_monitor_backfill_done", True)
    rt.audit.note("monitor_backfill", groups_logging_enabled=flipped)
    return flipped


def migrate(db: Db) -> int:
    """Apply pending migrations; return the resulting schema version."""
    version = _current_version(db)
    for num, path in _migration_files():
        if num <= version:
            continue
        db.executescript(path.read_text(encoding="utf-8"))
        if version == 0:
            db.execute("INSERT INTO schema_version (version) VALUES (?)", (num,))
        else:
            db.execute("UPDATE schema_version SET version = ?", (num,))
        version = num
    return version
