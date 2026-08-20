"""/download handling across surfaces.

Flow the owner asked for: type `/download <url>` in a chat → the original
command message is deleted → the video is downloaded (YouTube/TikTok/anything)
→ it is re-sent AS THE OWNER into that same chat.

Three surfaces:
- control chat: the bot sends the video to the control chat (it cannot delete
  the owner's message in a private bot chat — Bot API doesn't allow that);
- private chat via the Business connection: delete the owner's command via the
  business connection, then send the video with business_connection_id;
- group via the userbot: delete + re-send as the owner over MTProto (up to 2GB).
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import FSInputFile

from ...db import repo
from ...platforms import downloader
from ...runtime import Runtime


def _caption(v: downloader.DownloadedVideo) -> str:
    return v.title[:900] if v.title else ""


async def _notify(rt: Runtime, text: str) -> None:
    bot = rt.clients.get("control_bot")
    if bot is not None:
        try:
            await bot.send_message(rt.settings.telegram_owner_id, text)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


async def handle_control(rt: Runtime, bot: Bot, chat_id: int, url: str) -> None:
    """/download in the control chat — send the video back to the control chat."""
    try:
        v = await downloader.download(url, rt.settings.media_dir)
    except downloader.DownloadError as exc:
        await bot.send_message(chat_id, f"⚠️ {exc}")
        return
    try:
        await bot.send_video(chat_id, FSInputFile(v.path), caption=_caption(v),
                             width=v.width, height=v.height, duration=v.duration_s or 0)
        rt.audit.note("download_sent", surface="control", extractor=v.extractor,
                      size=v.size_bytes)
    except Exception as exc:  # noqa: BLE001
        await bot.send_message(chat_id, f"⚠️ send failed: {type(exc).__name__}")


async def handle_business(rt: Runtime, bot: Bot, chat_id: int, message_id: int,
                          business_connection_id: str, url: str) -> None:
    """/download the owner typed in a private chat (seen via Business)."""
    v = None
    try:
        v = await downloader.download(url, rt.settings.media_dir)
    except downloader.DownloadError as exc:
        await _notify(rt, f"⚠️ /download failed: {exc}")
        return
    # Delete the owner's command message first, so it never lingers.
    try:
        await bot.delete_business_messages(business_connection_id, [message_id])
    except Exception as exc:  # noqa: BLE001 — deletion is best-effort
        rt.audit.note("download_delete_failed", error=repr(exc)[:150])
    try:
        await bot.send_video(chat_id, FSInputFile(v.path), caption=_caption(v),
                             width=v.width, height=v.height, duration=v.duration_s or 0,
                             business_connection_id=business_connection_id)
        rt.audit.note("download_sent", surface="business", extractor=v.extractor,
                      size=v.size_bytes)
    except Exception as exc:  # noqa: BLE001
        await _notify(rt, f"⚠️ Downloaded '{v.title}' but sending failed: "
                          f"{type(exc).__name__} (video may exceed 50MB for a bot send)")


async def handle_group_userbot(rt: Runtime, client, chat_id: str, message_id: int,
                               url: str) -> None:
    """/download the owner typed in a group (seen via the userbot)."""
    # MTProto allows large files, so no 50MB cap here.
    try:
        v = await downloader.download(url, rt.settings.media_dir, max_bytes=1900 * 1024 * 1024)
    except downloader.DownloadError as exc:
        await _notify(rt, f"⚠️ /download failed: {exc}")
        return
    try:
        entity = await client.get_entity(int(chat_id))
        await client.delete_messages(entity, [message_id])
    except Exception as exc:  # noqa: BLE001
        rt.audit.note("download_delete_failed", error=repr(exc)[:150])
    try:
        entity = await client.get_entity(int(chat_id))
        await client.send_file(entity, v.path, caption=_caption(v),
                               supports_streaming=True)
        rt.audit.note("download_sent", surface="userbot", extractor=v.extractor,
                      size=v.size_bytes)
    except Exception as exc:  # noqa: BLE001
        await _notify(rt, f"⚠️ Downloaded '{v.title}' but group send failed: "
                          f"{type(exc).__name__}")


def is_download_command(text: str | None) -> str | None:
    """Return the URL if text is a /download command, else None."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped.lower().startswith("/download"):
        return None
    rest = stripped[len("/download"):].strip()
    return downloader.extract_url(rest)
