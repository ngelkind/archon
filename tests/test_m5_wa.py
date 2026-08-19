"""WhatsApp event-adapter tests with fake protobuf-shaped objects."""

from __future__ import annotations

from types import SimpleNamespace as NS

from archon.platforms.whatsapp.events import epoch_seconds, from_message_event


class FakeMessage:
    def __init__(self, fields: dict, **attrs):
        self._fields = fields
        self.conversation = attrs.pop("conversation", "")
        for k, v in attrs.items():
            setattr(self, k, v)

    def ListFields(self):
        return [(NS(name=name), value) for name, value in self._fields.items()]

    def HasField(self, name):
        if name in self._fields:
            return True
        # protobuf raises ValueError for fields not in the schema; mimic
        # "known but unset" as False for names we know about.
        known = {"extendedTextMessage", "imageMessage", "videoMessage",
                 "documentMessage", "contextInfo"}
        if name in known:
            return False
        raise ValueError(name)


def _jid(user="972500000000", server="s.whatsapp.net"):
    return NS(User=user, Server=server)


def _event(message, *, is_group=False, from_me=False, ts=1_755_600_000_000,
           msg_id="ABC123", broadcast=None, **flags):
    source = NS(
        Chat=_jid("120363000000000000", "g.us") if is_group else _jid(),
        Sender=_jid("972555000005"),
        SenderAlt=None,
        IsFromMe=from_me,
        IsGroup=is_group,
        BroadcastListOwner=broadcast,
    )
    event = NS(
        Info=NS(MessageSource=source, Timestamp=ts, ID=msg_id, Pushname="Dana"),
        Message=message,
    )
    for flag, value in flags.items():
        setattr(event, flag, value)
    return event


def test_epoch_magnitudes():
    assert epoch_seconds(1_755_600_000) == 1_755_600_000            # seconds
    assert epoch_seconds(1_755_600_000_000) == 1_755_600_000        # ms
    assert epoch_seconds(1_755_600_000_000_000) == 1_755_600_000    # µs
    assert epoch_seconds(0) is None and epoch_seconds("x") is None


def test_plain_text_message():
    msg = FakeMessage({"conversation": "hello"}, conversation="hello")
    inbound = from_message_event(_event(msg))
    assert inbound is not None
    assert inbound.platform == "wa" and inbound.chat_kind == "private"
    assert inbound.text == "hello" and inbound.sender_name == "Dana"
    assert inbound.ts.year >= 2025  # ms timestamp converted, not year 57k
    assert not inbound.is_edit and not inbound.is_delete


def test_group_extended_text():
    ext = NS(text="see you at 15:00", contextInfo=None)
    msg = FakeMessage({"extendedTextMessage": ext}, extendedTextMessage=ext)
    inbound = from_message_event(_event(msg, is_group=True))
    assert inbound.chat_kind == "group" and inbound.text == "see you at 15:00"
    assert inbound.chat_id.endswith("@g.us")


def test_revoke_maps_to_delete_with_original_id():
    proto = NS(type=0, key=NS(ID="ORIG42"), editedMessage=None)
    msg = FakeMessage({"protocolMessage": proto}, protocolMessage=proto)
    inbound = from_message_event(_event(msg, msg_id="REVOKEMSG"))
    assert inbound is not None and inbound.is_delete
    assert inbound.msg_id == "ORIG42"


def test_edit_protocol_message_carries_new_text():
    edited = FakeMessage({"conversation": "fixed text"}, conversation="fixed text")
    proto = NS(type=14, key=NS(ID="ORIG7"), editedMessage=edited)
    msg = FakeMessage({"protocolMessage": proto}, protocolMessage=proto)
    inbound = from_message_event(_event(msg))
    assert inbound.is_edit and inbound.msg_id == "ORIG7"
    assert inbound.text == "fixed text"


def test_broadcast_dropped():
    msg = FakeMessage({"conversation": "hi"}, conversation="hi")
    inbound = from_message_event(_event(msg, broadcast=_jid("972", "s.whatsapp.net")))
    assert inbound is None


def test_image_with_caption_surfaces_media():
    img = NS(caption="party invite")
    msg = FakeMessage({"imageMessage": img}, imageMessage=img)
    inbound = from_message_event(_event(msg))
    assert inbound.media and inbound.media[0].kind == "image"
    assert inbound.text == "party invite"


def test_garbage_fails_closed():
    assert from_message_event(object()) is None
