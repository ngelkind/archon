"""WhatsApp /download: the owner types /download <url> in any WhatsApp chat →
the command message is revoked (deleted for everyone) → the video is
downloaded → re-sent into that same chat as the owner via neonize.
"""

from __future__ import annotations

from typing import Any

from ...platforms import downloader
from ...runtime import Runtime

# WhatsApp accepts fairly large media; cap videos at 60MB, larger go as docs.
_WA_VIDEO_LIMIT = 60 * 1024 * 1024


def is_wa_download(text: str | None) -> str | None:
    if not text:
        return None
    stripped = text.strip()
    if not stripped.lower().startswith("/download"):
        return None
    return downloader.extract_url(stripped[len("/download"):].strip())


def _to_jid(raw: str) -> Any:
    from neonize.utils import build_jid

    user, _, server = raw.partition("@")
    return build_jid(user, server or "s.whatsapp.net")


async def _notify(rt: Runtime, text: str) -> None:
    bot = rt.clients.get("control_bot")
    if bot is not None:
        try:
            await bot.send_message(rt.settings.telegram_owner_id, text)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


async def handle_wa_download(rt: Runtime, client: Any, inbound, url: str) -> None:
    try:
        v = await downloader.download(url, rt.settings.media_dir, max_bytes=_WA_VIDEO_LIMIT)
    except downloader.DownloadError as exc:
        await _notify(rt, f"⚠️ WhatsApp /download failed: {exc}")
        return

    chat = _to_jid(inbound.chat_id)
    # Delete the /download command (revoke = delete for everyone).
    try:
        await client.revoke_message(chat, _to_jid(inbound.sender_id), inbound.msg_id)
    except Exception as exc:  # noqa: BLE001 — deletion is best-effort
        rt.audit.note("wa_download_revoke_failed", error=repr(exc)[:150])
    # Re-send the video as the owner.
    try:
        if v.size_bytes <= _WA_VIDEO_LIMIT:
            await client.send_video(chat, v.path, caption=(v.title or "")[:900])
        else:
            await client.send_document(chat, v.path, filename=f"{v.title[:60]}.mp4")
        rt.audit.note("download_sent", surface="wa", extractor=v.extractor,
                      size=v.size_bytes)
    except Exception as exc:  # noqa: BLE001
        await _notify(rt, f"⚠️ Downloaded '{v.title}' but the WhatsApp send failed: "
                          f"{type(exc).__name__}")
