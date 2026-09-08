"""Deleted/edited-message cards: enqueue here, send in logworker.

log_change is called by the pipeline for every edit/delete. It no longer sends:
it writes a row to the log_outbox (under the message's tenant scope) and
returns immediately, so the single ingest consumer is never blocked on a
rate-limited network send. logworker.py drains the outbox — coalescing,
digesting bursts, retrying — and renders the card (:func:`render_card`).

Every early return is audited with a reason (tglog_skipped), because the live
symptom was 27,444 edit/delete events producing 65 cards with no trace of why
the rest were dropped.
"""

from __future__ import annotations

import html
import sqlite3

from ..db import repo
from ..db.tenancy import TenantScope
from ..models import InboundMessage
from ..runtime import Runtime

_PLATFORM_LABEL = {"wa": "WhatsApp", "tg": "Telegram", "gmail": "Email"}
#: Per (chat, reason) skip notes are sampled to this many per process run, so a
#: high-volume unmonitored group does not flood the audit log with identical
#: "log_deletes_off" lines while still making the drop visible at least once.
_skip_seen: set[tuple] = set()


def channel_id(rt: Runtime) -> int | None:
    value = repo.setting_get(rt.db, "log.channel_id", rt.settings.tg_log_channel_id)
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _note_skip(rt: Runtime, msg: InboundMessage, reason: str) -> None:
    key = (msg.platform, msg.chat_id, reason)
    if key in _skip_seen:
        return
    _skip_seen.add(key)
    rt.audit.note("tglog_skipped", tenant_id=msg.tenant_id, platform=msg.platform,
                  chat=msg.chat_id, reason=reason)


def maybe_redact(rt: Runtime, text: str) -> str:
    if not repo.setting_get(rt.db, "log.redact_pii", False):
        return text
    from . import redact as redact_mod

    scrubbed = redact_mod.scrub(text)
    return scrubbed.text if hasattr(scrubbed, "text") else str(scrubbed)


def render_card(rt: Runtime, *, kind: str, platform: str, chat_label: str,
                sender: str, before: str | None, after: str | None) -> str | None:
    """The card text, PII-redacted per setting. Returns None only if redaction
    was requested and FAILED — the caller then suppresses the card rather than
    posting unredacted content."""
    try:
        before = maybe_redact(rt, before) if before else before
        after = maybe_redact(rt, after) if after else after
    except Exception as exc:  # noqa: BLE001
        rt.audit.note("redact_failed", platform=platform, error=repr(exc)[:120])
        return None
    icon = "\U0001f5d1" if kind == "deleted" else "✏️"
    lines = [
        f"{icon} <b>Message {kind}</b>",
        f"Platform: {_PLATFORM_LABEL.get(platform, platform)}",
        f"Chat: {html.escape(chat_label)}",
        f"From: {html.escape(sender)}",
    ]
    if before is not None:
        lines.append(f"\n<b>Before:</b>\n{html.escape(before[:1500])}")
    if after is not None:
        lines.append(f"\n<b>After:</b>\n{html.escape(after[:1500])}")
    if before is None and kind == "deleted":
        lines.append("\n<i>(content was not in the cache)</i>")
    return "\n".join(lines)


async def log_change(rt: Runtime, msg: InboundMessage,
                     before_row: sqlite3.Row | None, store=None) -> None:
    """Queue an edit/delete card under the message's tenant. Never sends."""
    scope = store if store is not None else TenantScope(rt.db, msg.tenant_id)
    chat_row = repo.chat_get(scope, msg.platform, msg.chat_id)
    if chat_row is None:
        _note_skip(rt, msg, "unknown_chat")
        return
    if not chat_row["log_deletes"]:
        _note_skip(rt, msg, "log_deletes_off")
        return
    if channel_id(rt) is None:
        _note_skip(rt, msg, "no_log_channel")
        return

    chat_label = chat_row["name"] if chat_row["name"] else msg.chat_id
    before_text = before_row["text"] if before_row else None
    if before_row and before_row["edited_text"] and msg.is_delete:
        before_text = before_row["edited_text"]  # deleted after an edit: show latest
    sender = msg.sender_name or msg.sender_id
    if before_row and (not sender or sender == "unknown"):
        sender = before_row["sender_name"] or before_row["sender_id"] or "unknown"

    kind = "deleted" if msg.is_delete else "edited"
    coalesce_s = float(repo.setting_get(scope, "log.coalesce_seconds",
                                        rt.settings.log_coalesce_seconds))
    repo.log_outbox_add(
        scope, platform=msg.platform, chat_id=msg.chat_id, chat_label=chat_label,
        msg_id=msg.msg_id, kind=kind, sender=sender or "unknown",
        before_text=before_text,
        after_text=msg.text if (msg.is_edit and msg.text) else None,
        coalesce_s=coalesce_s,
    )
    rt.audit.note("tglog_queued", tenant_id=msg.tenant_id, platform=msg.platform,
                  chat=msg.chat_id, kind=kind)
