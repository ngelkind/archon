"""A Telethon client double for the owner userbot.

It implements exactly the surface ``platforms/telegram/userbot.py`` touches
(``on``, ``connect``, ``is_user_authorized``, ``get_me``, ``iter_dialogs``,
``get_entity``, ``send_message``, ``send_file``, ``run_until_disconnected``)
and dispatches synthetic events to the handlers the real ``wire_events``
registered. Events mirror Telethon's attribute surface — including the fact
that a ``MessageDeleted`` for a private chat or legacy group has NO peer, so
``chat_id`` and ``is_private`` are both ``None`` — because that is the case
the old code got wrong.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telethon import events

_KIND_FOR_BUILDER = {
    events.NewMessage: "new",
    events.MessageEdited: "edit",
    events.MessageDeleted: "delete",
}


@dataclass(slots=True)
class FakeUser:
    id: int
    first_name: str = "Test"
    last_name: str = ""
    username: str | None = None
    phone: str | None = None
    bot: bool = False


@dataclass(slots=True)
class FakeChat:
    id: int
    title: str = "Group"
    broadcast: bool = False


@dataclass(slots=True)
class FakeMedia:
    ttl_seconds: int | None = None


@dataclass(slots=True)
class FakeMessage:
    id: int
    message: str | None = None
    date: datetime = field(default_factory=lambda: datetime.now(UTC))
    out: bool = False
    photo: Any = None
    video: Any = None
    voice: Any = None
    audio: Any = None
    media: FakeMedia | None = None
    download_bytes: bytes | None = b"\x89PNG-fake"

    async def download_media(self, file: str | None = None) -> str | None:
        if self.download_bytes is None:
            return None
        path = Path(file or "downloaded.bin")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.download_bytes)
        return str(path)


@dataclass(slots=True)
class FakeEvent:
    """One of NewMessage / MessageEdited / MessageDeleted, by ``kind``."""

    kind: str
    chat_id: int | None
    chat: FakeChat | FakeUser | None = None
    sender: FakeUser | None = None
    message: FakeMessage | None = None
    deleted_ids: list[int] = field(default_factory=list)

    @property
    def is_private(self) -> bool | None:
        if self.chat_id is None:
            return None  # Telethon: no peer -> no answer, not False
        return isinstance(self.chat, FakeUser)

    @property
    def is_group(self) -> bool | None:
        if self.chat_id is None:
            return None
        return isinstance(self.chat, FakeChat) and not self.chat.broadcast

    @property
    def is_channel(self) -> bool | None:
        if self.chat_id is None:
            return None
        return isinstance(self.chat, FakeChat) and self.chat.broadcast

    @property
    def sender_id(self) -> int | None:
        return self.sender.id if self.sender else None

    async def get_chat(self) -> Any:
        return self.chat

    async def get_sender(self) -> Any:
        return self.sender


@dataclass(slots=True)
class FakeDialog:
    id: int
    name: str
    is_user: bool = False
    is_group: bool = False
    is_channel: bool = False


def new_message(chat: FakeChat | FakeUser, msg_id: int, text: str | None, *,
                sender: FakeUser | None = None, out: bool = False,
                ttl_seconds: int | None = None, photo: bool = False,
                date: datetime | None = None) -> FakeEvent:
    chat_id = chat.id
    msg = FakeMessage(id=msg_id, message=text, out=out,
                      photo=object() if (photo or ttl_seconds) else None,
                      media=FakeMedia(ttl_seconds=ttl_seconds) if ttl_seconds else None,
                      date=date or datetime.now(UTC))
    return FakeEvent(kind="new", chat_id=chat_id, chat=chat,
                     sender=sender or FakeUser(id=555, first_name="Dana"), message=msg)


def edited_message(chat: FakeChat | FakeUser, msg_id: int, new_text: str, *,
                   sender: FakeUser | None = None, out: bool = False) -> FakeEvent:
    ev = new_message(chat, msg_id, new_text, sender=sender, out=out)
    ev.kind = "edit"
    return ev


def deleted(chat: FakeChat | None, ids: list[int]) -> FakeEvent:
    """``chat=None`` reproduces Telethon's peerless UpdateDeleteMessages."""
    return FakeEvent(kind="delete", chat_id=chat.id if chat else None, chat=chat,
                     deleted_ids=list(ids))


class FakeTelethonClient:
    def __init__(self, *, authorized: bool = True, me: FakeUser | None = None,
                 dialogs: list[FakeDialog] | None = None) -> None:
        self.authorized = authorized
        self.me = me or FakeUser(id=1, first_name="Owner", username="owner")
        self.dialogs = list(dialogs or [])
        self.handlers: list[tuple[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.files: list[dict[str, Any]] = []
        self.entities: dict[Any, Any] = {}
        self.connected = False
        self._stop = asyncio.Event()
        self._ids = 1000

    # --- registration -------------------------------------------------------

    def on(self, builder: Any):
        kind = _KIND_FOR_BUILDER[type(builder)]

        def deco(fn):
            self.handlers.append((kind, fn))
            return fn

        return deco

    async def fire(self, event: FakeEvent) -> None:
        """Deliver ``event`` to every handler registered for its kind, in
        registration order, awaiting each (as Telethon's dispatcher does)."""
        for kind, fn in self.handlers:
            if kind == event.kind:
                await fn(event)

    # --- lifecycle ------------------------------------------------------------

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> FakeUser:
        return self.me

    async def run_until_disconnected(self) -> None:
        await self._stop.wait()

    def disconnect(self) -> None:
        """Telethon returns from run_until_disconnected when the connection is
        gone for good; tests call this to simulate that."""
        self.connected = False
        self._stop.set()

    async def iter_dialogs(self, limit: int = 500):
        for d in self.dialogs[:limit]:
            yield d

    # --- peers + sends ------------------------------------------------------

    async def get_entity(self, ref: Any) -> Any:
        if ref in self.entities:
            return self.entities[ref]
        if isinstance(ref, int):
            for d in self.dialogs:
                if d.id == ref:
                    return FakeUser(id=ref, first_name=d.name) if d.is_user else FakeChat(id=ref, title=d.name)
            return FakeChat(id=ref, title=f"chat {ref}") if ref < 0 else FakeUser(id=ref)
        raise ValueError(f"unknown entity {ref!r}")

    async def send_message(self, entity: Any, text: str, schedule: Any = None,
                           **kwargs: Any) -> FakeMessage:
        self._ids += 1
        self.sent.append({"entity": entity, "text": text, "schedule": schedule, **kwargs})
        return FakeMessage(id=self._ids, message=text, out=True)

    async def send_file(self, entity: Any, file: Any, caption: str | None = None,
                        **kwargs: Any) -> FakeMessage:
        self._ids += 1
        self.files.append({"entity": entity, "file": file, "caption": caption, **kwargs})
        return FakeMessage(id=self._ids, message=caption, out=True)
