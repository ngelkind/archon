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


# TODO(TOS-REVIEW): All platforms — captures view-once / self-destruct media intended to be ephemeral — review before launch
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


async def send_protected_notice(rt: Runtime, *, platform: str, chat_id: str,
                                chat_name: str | None, sender_name: str) -> None:
    """A one-time item arrived but its content was withheld from this device
    (WhatsApp delivers companions only an ``unavailable type="view_once"``
    stub). Log who sent it and when — the metadata we DO get — so a one-time
    send is never silent, even when the pixels can't be recovered."""
    channel = _log_channel(rt)
    bot = rt.send_bot()
    if channel is None or bot is None:
        rt.audit.note("capture_no_channel", platform=platform, chat=chat_id)
        return
    caption = (
        f"👁 <b>One-time media detected</b> (content protected)\n"
        f"Platform: {_PLATFORM_LABEL.get(platform, platform)}\n"
        f"Chat: {html.escape(chat_name or chat_id)}\n"
        f"From: {html.escape(sender_name or 'unknown')}\n"
        f"<i>WhatsApp withheld the media from this device; only the fact of it is visible.</i>"
    )
    from .send import throttled_send

    result = await throttled_send(rt, lambda b: b.send_message(channel, caption),
                                  kind="capture_notice")
    rt.audit.note("capture_notice_sent" if result is not None else "capture_notice_failed",
                  platform=platform, chat=chat_id)


async def send_capture(rt: Runtime, *, platform: str, chat_id: str,
                       chat_name: str | None, sender_name: str, kind: str,
                       local_path: str) -> None:
    """Send a captured one-time media file to the log channel."""
    channel = _log_channel(rt)
    bot = rt.send_bot()
    if channel is None or bot is None:
        # Keep the file: it is the only copy of a one-time item. The note
        # carries the path so the owner can recover it by hand.
        rt.audit.note("capture_no_channel", platform=platform, chat=chat_id,
                      kind=kind, kept=local_path)
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

    result = await throttled_send(rt, _do, kind="capture")
    if result is None:
        # The post failed after a successful download+decrypt. Deleting the
        # file here would destroy the one thing the feature exists to recover,
        # so it stays on disk and the failure names it.
        rt.audit.note("capture_send_failed", platform=platform, chat=chat_id,
                      kind=kind, kept=local_path)
        return
    rt.audit.note("capture_sent", platform=platform, chat=chat_id, kind=kind)
    # One-time media is transient; don't hoard it on disk after logging.
    try:
        Path(local_path).unlink(missing_ok=True)
    except OSError:
        pass
