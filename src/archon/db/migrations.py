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
