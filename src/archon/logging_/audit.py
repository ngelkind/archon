"""Local, append-only audit log (JSONL + DB mirror).

Adapted from your-other-project/src/wa_helper/audit.py. Every gate
decision and every tool execution is recorded — including the drops — so
"only whitelisted chats ever reach the LLM" is checkable, not a promise.

Message text is recorded only when ``store_content`` is enabled, and never
for dropped messages: content that was refused must not be written to disk by
the thing that refused it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..db import Db
from ..db.repo import audit_add


class AuditLog:
    def __init__(self, path: str | Path, db: Db | None, *, store_content: bool) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = db
        self.store_content = store_content
        self._harden()

    def _harden(self) -> None:
        try:
            self.path.touch(exist_ok=True)
            if os.name == "posix":
                self.path.chmod(0o600)
        except OSError:
            pass  # Best effort; VM dirs are 700 via install.sh.

    def _write(self, record: dict[str, Any]) -> None:
        record["ts"] = time.time()
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            print(f"[audit] WARNING: could not write to {self.path}")
        if self.db is not None:
            try:
                audit_add(self.db, record.get("event", "note"), record.get("action", ""), record)
            except Exception:
                pass  # DB mirror is best-effort; JSONL is the source of truth.

    def gate(
        self,
        *,
        platform: str,
        chat_id: str | None,
        sender_id: str | None,
        allowed: bool,
        reason: str,
        text: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "event": "gate",
            "action": reason,
            "platform": platform,
            "chat": chat_id,
            "sender": sender_id,
            "allowed": allowed,
        }
        if allowed and self.store_content and text is not None:
            record["text"] = text
        self._write(record)

    def tool(self, *, name: str, args: dict[str, Any], ok: bool, result_summary: str) -> None:
        self._write(
            {
                "event": "tool",
                "action": name,
                "args": args,
                "ok": ok,
                "result": result_summary[:500],
            }
        )

    def note(self, message: str, **fields: Any) -> None:
        self._write({"event": "note", "action": message, **fields})
