"""Normalized cross-platform message model.

Every platform adapter (WhatsApp / Telegram business / Telegram userbot /
Gmail / sub-bots) converts its native event into an ``InboundMessage`` before
anything else sees it. Downstream code (cache, gate, triage, agent, logging)
never touches provider payload shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Platform = Literal["wa", "tg", "gmail"]
Source = Literal["business", "userbot", "wa", "gmail", "subbot", "control"]
ChatKind = Literal["private", "group", "channel", "email"]


@dataclass(slots=True)
class MediaRef:
    """A media item attached to a message, downloaded to local storage."""

    kind: Literal["image", "video", "audio", "document", "sticker", "other"]
    local_path: str | None  # None until (unless) downloaded
    mime: str | None = None
    caption: str | None = None


@dataclass(slots=True)
class InboundMessage:
    platform: Platform
    source: Source
    chat_id: str  # WA JID / TG chat id (str) / gmail thread id
    chat_kind: ChatKind
    msg_id: str
    sender_id: str
    ts: datetime
    chat_name: str | None = None
    sender_name: str | None = None
    is_from_me: bool = False
    text: str | None = None
    media: list[MediaRef] = field(default_factory=list)
    is_edit: bool = False
    is_delete: bool = False
    reply_to: str | None = None
    business_connection_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def chat_key(self) -> tuple[str, str]:
        return (self.platform, self.chat_id)


@dataclass(slots=True)
class OutboundIntent:
    """A message the agent wants to send; goes through the send-policy gate."""

    platform: Platform
    chat_id: str
    text: str | None = None
    media_path: str | None = None
    reply_to: str | None = None
    # 'business' = send as owner via Bot API business_connection_id,
    # 'userbot' = send as owner via MTProto, 'wa' = WhatsApp, 'gmail' = email
    via: Source = "wa"
    business_connection_id: str | None = None
