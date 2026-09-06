"""A neonize (whatsmeow) client double built on the REAL protobufs.

``platforms/whatsapp/events.py`` is written entirely against protobuf
semantics (``HasField``, ``ListFields``, container unwrapping), so the events
here are genuine ``neonize.proto`` messages — a ``SimpleNamespace`` could not
reproduce them and would let the parser drift unnoticed.

The client keeps neonize's real contract, warts included: ``connect()``
returns a *task* that lives for the session (it does not mean "connected"),
and ``is_connected`` is a property that yields an awaitable on the async
client. Both are documented gotchas the production code has been bitten by.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from neonize import events as ne
from neonize.proto import Neonize_pb2 as npb
from neonize.proto.waE2E import WAWebProtobufsE2E_pb2 as pb

OWNER_JID = "972500000001@s.whatsapp.net"
_ids = itertools.count(1)


# --- JIDs -------------------------------------------------------------------

def jid(raw: str) -> npb.JID:
    user, _, server = raw.partition("@")
    return npb.JID(User=user, Server=server or "s.whatsapp.net")


def jid_str(j: Any) -> str:
    return f"{j.User}@{j.Server}"


# --- event builders ---------------------------------------------------------

def _info(chat: str, sender: str, *, msg_id: str | None = None, from_me: bool = False,
          pushname: str = "Tester", ts_ms: int | None = None) -> npb.MessageInfo:
    source = npb.MessageSource(
        Chat=jid(chat), Sender=jid(sender), IsFromMe=from_me,
        IsGroup=chat.endswith("@g.us"),
    )
    # Milliseconds, as whatsmeow delivers them (events.py magnitude-tests this).
    return npb.MessageInfo(MessageSource=source, ID=msg_id or f"3EB0{next(_ids):08X}",
                           Timestamp=ts_ms if ts_ms is not None else int(time.time() * 1000),
                           Pushname=pushname)


def text_message(chat: str, text: str, *, sender: str = "972500000002@s.whatsapp.net",
                 msg_id: str | None = None, from_me: bool = False,
                 pushname: str = "Tester") -> npb.Message:
    return npb.Message(Info=_info(chat, sender, msg_id=msg_id, from_me=from_me,
                                  pushname=pushname),
                       Message=pb.Message(conversation=text))


def image_message(chat: str, *, caption: str | None = None,
                  sender: str = "972500000002@s.whatsapp.net", msg_id: str | None = None,
                  view_once: bool = False, keys: bool = True) -> npb.Message:
    img = pb.ImageMessage(mimetype="image/jpeg", caption=caption or "",
                          viewOnce=view_once)
    if keys:
        img.mediaKey = b"\x01" * 32
        img.directPath = "/v/t62.7118-24/fake"
    inner = pb.Message(imageMessage=img)
    if view_once:
        outer = pb.Message(viewOnceMessageV2=pb.FutureProofMessage(message=inner))
        return npb.Message(Info=_info(chat, sender, msg_id=msg_id), Message=outer,
                           IsViewOnceV2=True)
    return npb.Message(Info=_info(chat, sender, msg_id=msg_id), Message=inner)


def quoting_message(chat: str, text: str, quoted: pb.Message, *,
                    sender: str = OWNER_JID, from_me: bool = True,
                    msg_id: str | None = None) -> npb.Message:
    """A reply whose contextInfo carries ``quoted`` (the view-once recovery route)."""
    ext = pb.ExtendedTextMessage(text=text)
    ext.contextInfo.quotedMessage.CopyFrom(quoted)
    return npb.Message(Info=_info(chat, sender, msg_id=msg_id, from_me=from_me),
                       Message=pb.Message(extendedTextMessage=ext))


def revoke_message(chat: str, target_msg_id: str, *,
                   sender: str = "972500000002@s.whatsapp.net") -> npb.Message:
    proto = pb.ProtocolMessage(type=pb.ProtocolMessage.REVOKE)
    proto.key.ID = target_msg_id
    return npb.Message(Info=_info(chat, sender), Message=pb.Message(protocolMessage=proto))


def edit_message(chat: str, target_msg_id: str, new_text: str, *,
                 sender: str = "972500000002@s.whatsapp.net") -> npb.Message:
    proto = pb.ProtocolMessage(type=pb.ProtocolMessage.MESSAGE_EDIT)
    proto.key.ID = target_msg_id
    proto.editedMessage.CopyFrom(pb.Message(conversation=new_text))
    return npb.Message(Info=_info(chat, sender), Message=pb.Message(protocolMessage=proto))


def view_once_stub(chat: str, *, sender: str = "972500000002@s.whatsapp.net",
                   msg_id: str | None = None) -> ne.UndecryptableMessageEv:
    """What a companion receives when WhatsApp withholds a view-once item."""
    return ne.UndecryptableMessageEv(Info=_info(chat, sender, msg_id=msg_id),
                                     IsUnavailable=True)


def logged_out() -> ne.LoggedOutEv:
    return ne.LoggedOutEv(OnConnect=False)


def connected() -> ne.ConnectedEv:
    return ne.ConnectedEv()


# --- the client ----------------------------------------------------------------

@dataclass(slots=True)
class FakeGroup:
    jid: str
    name: str


@dataclass(slots=True)
class _GroupInfo:
    JID: npb.JID
    GroupName: Any


@dataclass(slots=True)
class _Name:
    Name: str


@dataclass(slots=True)
class _SendResponse:
    ID: str
    Timestamp: int = 0


@dataclass(slots=True)
class _Me:
    JID: npb.JID


class FakeAClient:
    """Mirrors ``neonize.aioze.client.NewAClient`` at the seam Archon uses."""

    def __init__(self, *, groups: list[FakeGroup] | None = None,
                 me: str = OWNER_JID, media: bytes = b"\xff\xd8fake-jpeg",
                 lid_to_phone: dict[str, str] | None = None) -> None:
        self.groups = list(groups or [])
        self.me_jid = me
        self.media = media
        self.lid_to_phone = dict(lid_to_phone or {})
        self.handlers: dict[type, list[Callable[..., Awaitable[None]]]] = {}
        self.sent: list[dict[str, Any]] = []
        self.revoked: list[dict[str, Any]] = []
        self.read: list[dict[str, Any]] = []
        self.presence: list[Any] = []
        self.downloads: list[Any] = []
        self.connected_flag = False
        self.stopped = False
        self.connect_task: asyncio.Task | None = None
        self._session_over = asyncio.Event()
        self._ids = itertools.count(1)

    # --- registration ------------------------------------------------------------

    def event(self, ev_type: type):
        def deco(fn):
            self.handlers.setdefault(ev_type, []).append(fn)
            return fn
        return deco

    async def fire(self, ev: Any) -> None:
        for fn in self.handlers.get(type(ev), []):
            await fn(self, ev)

    async def go_online(self) -> None:
        """The server accepted the session: what a real ConnectedEv means."""
        self.connected_flag = True
        await self.fire(connected())

    # --- lifecycle (neonize's real shape) -------------------------------------------

    async def connect(self) -> asyncio.Task:
        """Returns a TASK that runs for the whole session, exactly like
        neonize's ``connect_with_proxy``; the Go client is not ready yet."""
        self.connect_task = asyncio.create_task(self._session())
        return self.connect_task

    async def _session(self) -> None:
        await self._session_over.wait()

    @property
    def is_connected(self) -> Awaitable[bool]:
        async def _get() -> bool:
            return self.connected_flag
        return _get()  # an unawaited coroutine — always truthy, as in neonize

    async def disconnect(self) -> None:
        self.connected_flag = False

    async def stop(self) -> None:
        self.stopped = True
        self.connected_flag = False
        self._session_over.set()

    # --- data the handlers ask for ------------------------------------------------------

    async def get_joined_groups(self) -> list[_GroupInfo]:
        return [_GroupInfo(JID=jid(g.jid), GroupName=_Name(g.name)) for g in self.groups]

    async def get_me(self) -> _Me:
        return _Me(JID=jid(self.me_jid))

    async def get_pn_from_lid(self, lid: npb.JID) -> npb.JID | None:
        phone = self.lid_to_phone.get(jid_str(lid))
        return jid(phone) if phone else None

    async def download_any(self, message: pb.Message) -> bytes:
        self.downloads.append(message)
        return self.media

    # --- sends -----------------------------------------------------------------------

    async def send_message(self, to: npb.JID, message: Any, **kw: Any) -> _SendResponse:
        mid = f"BAE5{next(self._ids):08X}"
        self.sent.append({"to": jid_str(to), "message": message, "id": mid, **kw})
        return _SendResponse(ID=mid)

    async def send_image(self, to: npb.JID, path: str, caption: str = "", **kw: Any) -> _SendResponse:
        mid = f"BAE5{next(self._ids):08X}"
        self.sent.append({"to": jid_str(to), "image": path, "caption": caption, "id": mid})
        return _SendResponse(ID=mid)

    async def send_video(self, to: npb.JID, path: str, caption: str = "", **kw: Any) -> _SendResponse:
        mid = f"BAE5{next(self._ids):08X}"
        self.sent.append({"to": jid_str(to), "video": path, "caption": caption, "id": mid})
        return _SendResponse(ID=mid)

    async def send_document(self, to: npb.JID, path: str, **kw: Any) -> _SendResponse:
        mid = f"BAE5{next(self._ids):08X}"
        self.sent.append({"to": jid_str(to), "document": path, "id": mid, **kw})
        return _SendResponse(ID=mid)

    async def send_chat_presence(self, to: npb.JID, state: Any, media: Any) -> None:
        self.presence.append((jid_str(to), state))

    async def revoke_message(self, chat: npb.JID, sender: npb.JID, message_id: str) -> _SendResponse:
        self.revoked.append({"chat": jid_str(chat), "sender": jid_str(sender), "id": message_id})
        return _SendResponse(ID=f"REV{next(self._ids)}")

    async def mark_read(self, *message_ids: str, chat: npb.JID, sender: npb.JID,
                        receipt: Any, timestamp: int | None = None) -> None:
        """The real 0.4.3 signature: ids are varargs, chat/sender/receipt are
        keyword-only. A caller passing a list positionally or omitting sender
        fails here as it would against neonize."""
        if not all(isinstance(m, str) for m in message_ids):
            raise TypeError("message_ids must be strings")
        self.read.append({"ids": list(message_ids), "chat": jid_str(chat),
                          "sender": jid_str(sender), "receipt": receipt})

    async def is_on_whatsapp(self, *phones: str) -> list[Any]:
        @dataclass
        class _R:
            IsIn: bool
            Query: str
        return [_R(IsIn=p.lstrip("+").startswith("972"), Query=p) for p in phones]
