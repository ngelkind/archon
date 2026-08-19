"""SQLite access.

One process, one connection, WAL mode, guarded by a lock (the pattern proven
in your-other-project's store.py: neonize callbacks arrive on Go
threads, so ``check_same_thread=False`` + an explicit lock is required even in
a mostly-asyncio program).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class Db:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=60.0
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=60000")
            self._conn.execute("PRAGMA foreign_keys=ON")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def executescript(self, script: str) -> None:
        with self._lock:
            self._conn.executescript(script)
            self._conn.commit()

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def backup_to(self, dest: str | Path) -> None:
        with self._lock, sqlite3.connect(str(dest)) as out:
            self._conn.backup(out)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
