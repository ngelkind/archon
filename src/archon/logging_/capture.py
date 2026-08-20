"""View-once / self-destruct media capture → log channel.

Independent of the whitelist: a chat is "capture-enabled" when its
``capture_media`` flag is set, OR it is a private chat and the global
``capture.all_dms`` setting is on. When someone sends a one-time photo/video/
voice/file in such a chat, the platform layer downloads it to a local path and
calls :func:`send_capture`, which forwards the file to the configured log
channel with a card. The platform layer owns the download (neonize / Telethon /
Bot API differ); this module owns the gate + the send.
"""

from __future__ import annotations

import html
from pathlib import Path

from aiogram.types import FSInputFile

from ..db import repo
from ..runtime import Runtime

_PLATFORM_LABEL = {"wa": "WhatsApp", "tg": "Telegram"}


def capture_enabled(rt: Runtime, platform: str, chat_id: str, chat_kind: str) -> bool:
    row = repo.chat_get(rt.db, platform, chat_id)
    if row is not None and row["capture_media"]:
        return True
    if chat_kind == "private" and repo.setting_get(rt.db, "capture.all_dms", False):
        return True
    return False


def _log_channel(rt: Runtime) -> int | None:
    value = repo.setting_get(rt.db, "log.channel_id", rt.settings.tg_log_channel_id)
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


async def send_capture(rt: Runtime, *, platform: str, chat_id: str,
                       chat_name: str | None, sender_name: str, kind: str,
                       local_path: str) -> None:
    """Send a captured one-time media file to the log channel."""
    channel = _log_channel(rt)
    bot = rt.send_bot()
    if channel is None or bot is None:
        rt.audit.note("capture_no_channel", platform=platform, chat=chat_id)
        return
    caption = (
        f"👁 <b>One-time {html.escape(kind)} captured</b>\n"
        f"Platform: {_PLATFORM_LABEL.get(platform, platform)}\n"
        f"Chat: {html.escape(chat_name or chat_id)}\n"
        f"From: {html.escape(sender_name or 'unknown')}"
    )
    from .send import throttled_send

    def _do(b: Any):
        f = FSInputFile(local_path)
        if kind == "image":
            return b.send_photo(channel, f, caption=caption)
        if kind == "video":
            return b.send_video(channel, f, caption=caption)
        if kind in ("audio", "voice"):
            return b.send_audio(channel, f, caption=caption)
        return b.send_document(channel, f, caption=caption)

    try:
        result = await throttled_send(rt, _do)
        rt.audit.note("capture_sent" if result is not None else "capture_send_failed",
                      platform=platform, chat=chat_id, kind=kind)
    finally:
        # One-time media is transient; don't hoard it on disk after logging.
        try:
            Path(local_path).unlink(missing_ok=True)
        except OSError:
            pass
