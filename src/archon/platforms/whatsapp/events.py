"""Adapter from neonize's MessageEv to Archon's InboundMessage.

The only module that touches neonize protobuf shapes. Carries over the
hard-won gotchas from your-other-project/wa_events.py:

* timestamps arrive in MILLISECONDS (magnitude-tested, not blindly /1000);
* payload kinds are enumerated via ListFields(), never guessed;
* container flags (IsEdit, IsViewOnce, IsEphemeral, ...) live on the EVENT,
  not the message — neonize unwraps containers so the inner text looks normal;
* LID addressing: Sender + SenderAlt both checked.

New here vs the original: revoke (delete) and edit events are MAPPED rather
than dropped, because Archon logs before/after diffs; media payloads are
surfaced (kind + caption) for optional download by the client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ...models import InboundMessage, MediaRef

CONTAINER_FLAGS = (
    "IsViewOnce", "IsViewOnceV2", "IsViewOnceV2Extension",
    "IsEphemeral", "IsEdit", "IsDocumentWithCaption", "IsLottieSticker",
)

_MEDIA_KINDS = {
    "imageMessage": "image",
    "videoMessage": "video",
    "audioMessage": "audio",
    "documentMessage": "document",
    "stickerMessage": "sticker",
}

# protobuf ProtocolMessage.Type: 0 = REVOKE, 14 = MESSAGE_EDIT
_REVOKE = 0
_EDIT = 14

_VO_CONTAINERS = ("viewOnceMessageV2", "viewOnceMessageV2Extension", "viewOnceMessage")


def unwrap_view_once(message: Any) -> tuple[Any, bool]:
    """If ``message`` is a view-once container, return (inner_message, True);
    otherwise (message, False). The inner message holds the real media."""
    for cont in _VO_CONTAINERS:
        try:
            if message.HasField(cont):
                return getattr(message, cont).message, True
        except (ValueError, AttributeError):
            continue
    return message, False


def jid_str(value: Any) -> str | None:
    if value is None:
        return None
    user = getattr(value, "User", "")
    server = getattr(value, "Server", "")
    if not user or not server:
        return None
    return f"{user}@{server}"


def epoch_seconds(raw: Any) -> float | None:
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 1e14:  # microseconds
        return value / 1_000_000
    if value > 1e11:  # milliseconds
        return value / 1000
    return value


def _extract_text(message: Any) -> str | None:
    try:
        if message.HasField("extendedTextMessage"):
            return message.extendedTextMessage.text or None
    except ValueError:
        pass
    text = message.conversation or None
    if text:
        return text
    # media captions
    for kind in ("imageMessage", "videoMessage", "documentMessage"):
        try:
            if message.HasField(kind):
                caption = getattr(message, kind).caption
                if caption:
                    return caption
        except ValueError:
            continue
    return None


def from_message_event(event: Any) -> InboundMessage | None:
    """Convert a neonize MessageEv. Returns None when unparseable (fail closed)."""
    try:
        info = event.Info
        source = info.MessageSource
        message = event.Message

        payload_kinds = tuple(field.name for field, _ in message.ListFields())
        flags = [name for name in CONTAINER_FLAGS if getattr(event, name, False)]
        if jid_str(getattr(source, "BroadcastListOwner", None)):
            return None  # broadcast lists are never processed

        chat = jid_str(source.Chat)
        sender = jid_str(source.Sender) or jid_str(getattr(source, "SenderAlt", None))
        if not chat or not sender:
            return None

        ts_epoch = epoch_seconds(getattr(info, "Timestamp", None))
        ts = datetime.fromtimestamp(ts_epoch, tz=UTC) if ts_epoch else datetime.now(UTC)

        msg_id = getattr(info, "ID", None) or ""
        is_edit = "IsEdit" in flags
        is_delete = False
        target_id = msg_id

        # Deletions (revokes) and some edits arrive as protocolMessage.
        if "protocolMessage" in payload_kinds:
            proto = message.protocolMessage
            proto_type = int(getattr(proto, "type", -1))
            key_id = getattr(getattr(proto, "key", None), "ID", "") or ""
            if proto_type == _REVOKE:
                is_delete = True
                target_id = key_id or msg_id
            elif proto_type == _EDIT:
                is_edit = True
                target_id = key_id or msg_id
                edited = getattr(proto, "editedMessage", None)
                if edited is not None:
                    message = edited
            else:
                return None  # app-state / history-sync noise
        elif is_edit:
            # neonize unwrapped the edit; original id rides on the event if present
            target_id = getattr(event, "OrigMessageID", "") or msg_id

        # Unwrap a view-once container to its inner media message (the real
        # imageMessage/videoMessage/audioMessage lives inside). The media IS
        # delivered to companion devices; only the official clients refuse to
        # display it ("open on your phone"). We can still download it.
        inner, is_vo_container = unwrap_view_once(message)
        media_message = inner if is_vo_container else message
        media_kinds = (
            tuple(f.name for f, _ in inner.ListFields()) if is_vo_container else payload_kinds
        )

        media: list[MediaRef] = []
        media_view_once = False
        for kind, mapped in _MEDIA_KINDS.items():
            if kind in media_kinds:
                media.append(MediaRef(kind=mapped, local_path=None))  # type: ignore[arg-type]
                try:
                    if getattr(getattr(media_message, kind), "viewOnce", False):
                        media_view_once = True
                except (AttributeError, ValueError):
                    pass

        view_once = (
            is_vo_container
            or media_view_once
            or any(f.startswith("IsViewOnce") for f in flags)
            or any("viewonce" in pk.lower() for pk in payload_kinds)
        )

        return InboundMessage(
            platform="wa",
            source="wa",
            chat_id=chat,
            chat_kind="group" if source.IsGroup else "private",
            msg_id=target_id,
            sender_id=sender,
            sender_name=getattr(info, "Pushname", "") or None,
            ts=ts,
            is_from_me=bool(source.IsFromMe),
            text=_extract_text(media_message),
            media=media,
            is_edit=is_edit,
            is_delete=is_delete,
            is_ephemeral_media=view_once and bool(media),
            raw={"payload_kinds": list(payload_kinds), "flags": flags,
                 "event_msg_id": msg_id, "view_once": view_once},
        )
    except Exception:  # noqa: BLE001 — any parse failure fails closed
        return None
