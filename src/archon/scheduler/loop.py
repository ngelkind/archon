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
from ..db.tenancy import TenantScope
from ..pipeline.confirm import _execute  # same executor registry as the confirm gate
from ..runtime import Runtime

_TICK_S = 15

_KIND_FOR_PLATFORM = {"wa": "wa.send", "tg": "tg.send_private", "gmail": "email.send"}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


async def _fire_scheduled(rt: Runtime) -> None:
    # Process-wide: every tenant's due messages, each written back under its own
    # tenant scope (see repo.schedule_due_all_tenants).
    rows = repo.schedule_due_all_tenants(rt.db, _now())
    for row in rows:
        scope = TenantScope(rt.db, row["tenant_id"])
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
            result = await _execute(rt, kind or "", payload, scope)
            repo.schedule_set_status(scope, row["id"], "sent", str(result)[:300])
            rt.audit.note("scheduled_sent", id=row["id"], platform=row["platform"])
        except Exception as exc:  # noqa: BLE001
            repo.schedule_set_status(scope, row["id"], "failed", repr(exc)[:300])
            rt.audit.note("scheduled_failed", id=row["id"], error=repr(exc)[:200])


async def _fire_pending_replies(rt: Runtime) -> None:
    rows = repo.pending_reply_due_all_tenants(rt.db, _now())
    for row in rows:
        scope = TenantScope(rt.db, row["tenant_id"])
        kind = _KIND_FOR_PLATFORM.get(row["c_platform"])
        if row["c_platform"] == "tg" and row["c_chat_id"].startswith("-"):
            kind = "tg.send_group"
        try:
            payload = {"chat_id": row["c_chat_id"], "chat_jid": row["c_chat_id"],
                       "text": row["draft_text"], "chat_name": row["c_name"],
                       "reply_to": row["reply_to"]}
            await _execute(rt, kind or "", payload, scope)
            repo.pending_reply_set_status(scope, row["id"], "sent")
            rt.audit.note("delayed_reply_sent", id=row["id"])
        except Exception as exc:  # noqa: BLE001
            # 'failed', not 'cancelled': the draft is kept and the error is
            # visible, so pending_replies_list can show what went wrong instead
            # of the reply silently vanishing.
            repo.pending_reply_set_status(scope, row["id"], "failed")
            rt.audit.note("delayed_reply_failed", id=row["id"], error=repr(exc)[:200])


async def _expire_stale_approvals(rt: Runtime) -> None:
    expired = repo.pending_actions_expire_due(rt.db, _now())
    for row in expired:
        rt.events.publish("approval.resolved", action_id=int(row["id"]),
                          action_kind=row["kind"], status="expired", actor="sweep")
        rt.audit.note("confirm_expired", action_id=int(row["id"]), kind=row["kind"],
                      tenant_id=int(row["tenant_id"]))


async def _sweep_idle_sessions(rt: Runtime) -> None:
    sweep = getattr(getattr(rt, "sessions", None), "sweep", None)
    if sweep is not None:
        await sweep()


async def run(rt: Runtime) -> None:
    rt.health["scheduler"] = "running"
    while True:
        try:
            await _fire_scheduled(rt)
            await _fire_pending_replies(rt)
            await _expire_stale_approvals(rt)
            await _sweep_idle_sessions(rt)
        except Exception as exc:  # noqa: BLE001
            rt.audit.note("scheduler_error", error=repr(exc)[:300])
        await asyncio.sleep(_TICK_S)
