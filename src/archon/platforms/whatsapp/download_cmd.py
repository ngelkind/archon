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
    bot = rt.send_bot()
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

    # @lid addressing on send/revoke is the usual cause of error 479. If the
    # chat is a LID, convert it to the phone-number JID and prefer that.
    chat_candidates: list[str] = []
    if inbound.chat_id.endswith("@lid"):
        try:
            pn = await client.get_pn_from_lid(_to_jid(inbound.chat_id))
            pn_str = f"{pn.User}@{pn.Server}" if pn and getattr(pn, "User", None) else None
            if pn_str:
                chat_candidates.append(pn_str)
        except Exception as exc:  # noqa: BLE001
            rt.audit.note("wa_lid_convert_failed", error=repr(exc)[:120])
    chat_candidates.append(inbound.chat_id)
    chat = _to_jid(chat_candidates[0])

    # Delete the /download command (revoke = delete for everyone). Best-effort;
    # error 479 here does not block the re-send.
    try:
        await client.revoke_message(chat, _to_jid(inbound.sender_id), inbound.msg_id)
    except Exception as exc:  # noqa: BLE001
        rt.audit.note("wa_download_revoke_failed", error=repr(exc)[:150])

    # Re-send the video as the owner, trying each candidate JID until one works.
    last_err: Exception | None = None
    for cand in chat_candidates:
        try:
            target = _to_jid(cand)
            if v.size_bytes <= _WA_VIDEO_LIMIT:
                await client.send_video(target, v.path, caption=(v.title or "")[:900])
            else:
                await client.send_document(target, v.path, filename=f"{v.title[:60]}.mp4")
            rt.audit.note("download_sent", surface="wa", extractor=v.extractor,
                          size=v.size_bytes, jid=cand)
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            rt.audit.note("wa_download_send_failed", jid=cand, error=repr(exc)[:200])
    await _notify(rt, f"⚠️ Downloaded '{v.title}' but the WhatsApp send failed "
                      f"({type(last_err).__name__}: {str(last_err)[:120]}).")
