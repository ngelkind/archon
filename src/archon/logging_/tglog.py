"""Deleted/edited-message cards → the configured Telegram log channel.

Format: before / after / platform. The channel id lives in the settings table
(log.channel_id; falls back to the TG_LOG_CHANNEL_ID env default) so the bot
can retarget it at runtime (log_channel_set tool). Optional PII redaction via
the vendored redact module (log.redact_pii)."""

from __future__ import annotations

import html
import sqlite3

from ..db import repo
from ..models import InboundMessage
from ..runtime import Runtime

_PLATFORM_LABEL = {"wa": "WhatsApp", "tg": "Telegram", "gmail": "Email"}


def _channel_id(rt: Runtime) -> int | None:
    value = repo.setting_get(rt.db, "log.channel_id", rt.settings.tg_log_channel_id)
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _maybe_redact(rt: Runtime, text: str) -> str:
    if not repo.setting_get(rt.db, "log.redact_pii", False):
        return text
    try:
        from . import redact as redact_mod

        scrubbed = redact_mod.scrub(text)
        return scrubbed.text if hasattr(scrubbed, "text") else str(scrubbed)
    except Exception:  # noqa: BLE001 — redaction must never block logging
        return text


def _card(kind: str, platform: str, chat_label: str, sender: str,
          before: str | None, after: str | None) -> str:
    icon = "🗑" if kind == "deleted" else "✏️"
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
                     before_row: sqlite3.Row | None) -> None:
    """Called by the pipeline for every edit/delete event."""
    chat_row = repo.chat_get(rt.db, msg.platform, msg.chat_id)
    # Off by default: log only when the chat's log_deletes flag is on. DMs get
    # it on at creation (repo.chat_upsert); groups are opt-in via
    # chat_log_policy_set. An unknown chat is treated as off.
    if chat_row is None or not chat_row["log_deletes"]:
        return
    channel = _channel_id(rt)
    if channel is None or rt.send_bot() is None:
        return

    chat_label = (chat_row["name"] if chat_row and chat_row["name"] else msg.chat_id)
    before_text = before_row["text"] if before_row else None
    if before_row and before_row["edited_text"] and msg.is_delete:
        before_text = before_row["edited_text"]  # deleted after an edit: show latest
    sender = msg.sender_name or msg.sender_id
    if before_row and (not sender or sender == "unknown"):
        sender = before_row["sender_name"] or before_row["sender_id"] or "unknown"

    kind = "deleted" if msg.is_delete else "edited"
    before_clean = _maybe_redact(rt, before_text) if before_text else None
    after_clean = _maybe_redact(rt, msg.text) if (msg.is_edit and msg.text) else None
    card = _card(kind, msg.platform, chat_label, sender or "unknown",
                 before_clean, after_clean)
    from .send import throttled_send
    result = await throttled_send(rt, lambda b: b.send_message(channel, card), kind="log_card")
    rt.audit.note("tglog_sent" if result is not None else "tglog_dropped",
                  platform=msg.platform, chat=msg.chat_id, kind=kind)
