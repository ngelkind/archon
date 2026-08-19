"""Scheduler loop: fires due scheduled_messages and pending_replies every 15s.

Telegram-group schedules are delegated to Telegram's native scheduler at
creation time (status delegated_native) and never touch this loop. Everything
else — WhatsApp, Telegram private, email — is sent from here via the same
executors the confirm gate uses, so policy code stays in one place."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from ..db import repo
from ..pipeline.confirm import _execute  # same executor registry as the confirm gate
from ..runtime import Runtime

_TICK_S = 15

_KIND_FOR_PLATFORM = {"wa": "wa.send", "tg": "tg.send_private", "gmail": "email.send"}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


async def _fire_scheduled(rt: Runtime) -> None:
    rows = rt.db.query(
        "SELECT s.*, c.platform AS c_platform, c.chat_id AS c_chat_id, c.name AS c_name "
        "FROM scheduled_messages s JOIN chats c ON c.id = s.chat_pk "
        "WHERE s.status = 'pending' AND s.due_at <= ?", (_now(),),
    )
    for row in rows:
        kind = _KIND_FOR_PLATFORM.get(row["platform"])
        try:
            if kind == "email.send":
                payload = {"to": row["c_chat_id"], "subject": "(scheduled)",
                           "body": row["text"] or ""}
                extra = json.loads(row["result"] or "{}")
                payload.update({k: v for k, v in extra.items() if k in ("subject",)})
            else:
                payload = {"chat_id": row["c_chat_id"], "chat_jid": row["c_chat_id"],
                           "text": row["text"], "chat_name": row["c_name"],
                           "image_path": row["media_path"]}
            result = await _execute(rt, kind or "", payload)
            rt.db.execute(
                "UPDATE scheduled_messages SET status = 'sent', result = ? WHERE id = ?",
                (str(result)[:300], row["id"]),
            )
            rt.audit.note("scheduled_sent", id=row["id"], platform=row["platform"])
        except Exception as exc:  # noqa: BLE001
            rt.db.execute(
                "UPDATE scheduled_messages SET status = 'failed', result = ? WHERE id = ?",
                (repr(exc)[:300], row["id"]),
            )
            rt.audit.note("scheduled_failed", id=row["id"], error=repr(exc)[:200])


async def _fire_pending_replies(rt: Runtime) -> None:
    rows = rt.db.query(
        "SELECT p.*, c.platform AS c_platform, c.chat_id AS c_chat_id, c.name AS c_name "
        "FROM pending_replies p JOIN chats c ON c.id = p.chat_pk "
        "WHERE p.status = 'pending' AND p.due_at <= ?", (_now(),),
    )
    for row in rows:
        kind = _KIND_FOR_PLATFORM.get(row["c_platform"])
        if row["c_platform"] == "tg" and row["c_chat_id"].startswith("-"):
            kind = "tg.send_group"
        try:
            payload = {"chat_id": row["c_chat_id"], "chat_jid": row["c_chat_id"],
                       "text": row["draft_text"], "chat_name": row["c_name"]}
            await _execute(rt, kind or "", payload)
            rt.db.execute("UPDATE pending_replies SET status = 'sent' WHERE id = ?",
                          (row["id"],))
            rt.audit.note("delayed_reply_sent", id=row["id"])
        except Exception as exc:  # noqa: BLE001
            rt.db.execute("UPDATE pending_replies SET status = 'cancelled' WHERE id = ?",
                          (row["id"],))
            rt.audit.note("delayed_reply_failed", id=row["id"], error=repr(exc)[:200])


async def run(rt: Runtime) -> None:
    rt.health["scheduler"] = "running"
    while True:
        try:
            await _fire_scheduled(rt)
            await _fire_pending_replies(rt)
        except Exception as exc:  # noqa: BLE001
            rt.audit.note("scheduler_error", error=repr(exc)[:300])
        await asyncio.sleep(_TICK_S)
