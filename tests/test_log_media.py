"""Deleted/edited media re-attaches to the log card (commit 16).

A deleted PHOTO used to log as a text-only card, losing the whole point of
message auditing for media. The card now re-sends the cached file with the card
as its caption when the file is still on disk, and falls back to text when it
is gone.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from test_m2 import make_rt

from archon.db import repo
from archon.logging_ import logworker, tglog
from archon.models import InboundMessage, MediaRef

_CHANNEL = -100999


class _FakeBot:
    def __init__(self):
        self.photos, self.messages, self.documents = [], [], []

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.photos.append({"chat": chat_id, "caption": caption})
        return object()

    async def send_message(self, chat_id, text, **kw):
        self.messages.append({"chat": chat_id, "text": text})
        return object()

    async def send_document(self, chat_id, document, caption=None, **kw):
        self.documents.append({"chat": chat_id, "caption": caption})
        return object()


def _logged_chat(rt):
    repo.setting_set(rt.db, "log.channel_id", _CHANNEL)
    pk = repo.chat_upsert(rt.db, "tg", "4242", "Dana", "private")
    repo.chat_set_field(rt.db, pk, "log_deletes", 1)
    return pk


def _msg(**kw) -> InboundMessage:
    base = {"platform": "tg", "source": "business", "chat_id": "4242",
            "chat_kind": "private", "msg_id": "m1", "sender_id": "s",
            "sender_name": "Dana", "ts": datetime.now(UTC), "text": "look at this"}
    base.update(kw)
    return InboundMessage(**base)


def test_deleted_photo_reattaches_the_cached_file(tmp_path):
    rt = make_rt(tmp_path)
    pk = _logged_chat(rt)
    photo = tmp_path / "shot.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0jpeg-ish")
    repo.message_upsert(rt.db, _msg(media=[MediaRef(kind="image", local_path=str(photo))]), pk)
    before = repo.message_get(rt.db, "tg", "4242", "m1")
    assert before["media_path"] == str(photo)

    asyncio.run(tglog.log_change(rt, _msg(text=None, is_delete=True), before))
    row = rt.db.query_one("SELECT * FROM log_outbox WHERE msg_id='m1'")
    assert row["media_path"] == str(photo)

    fake = _FakeBot()
    rt.clients["notifier"] = fake
    asyncio.run(logworker._drain_once(rt))

    assert len(fake.photos) == 1
    assert fake.photos[0]["chat"] == _CHANNEL
    assert "deleted" in (fake.photos[0]["caption"] or "").lower()
    assert fake.messages == [], "a photo card must not also post a text card"


def test_deleted_photo_falls_back_to_text_when_the_file_is_gone(tmp_path):
    rt = make_rt(tmp_path)
    pk = _logged_chat(rt)
    # cached path points at a file that no longer exists on disk
    repo.message_upsert(
        rt.db, _msg(media=[MediaRef(kind="image", local_path=str(tmp_path / "gone.jpg"))]), pk)
    before = repo.message_get(rt.db, "tg", "4242", "m1")

    asyncio.run(tglog.log_change(rt, _msg(text=None, is_delete=True), before))
    row = rt.db.query_one("SELECT * FROM log_outbox WHERE msg_id='m1'")
    assert row["media_path"] is None, "a vanished file must not be carried on the card"

    fake = _FakeBot()
    rt.clients["notifier"] = fake
    asyncio.run(logworker._drain_once(rt))
    assert fake.photos == []
    assert len(fake.messages) == 1  # the text card
    assert "the file is gone" not in fake.messages[0]["text"]  # just a normal card


def test_a_text_only_delete_still_posts_a_text_card(tmp_path):
    rt = make_rt(tmp_path)
    pk = _logged_chat(rt)
    repo.message_upsert(rt.db, _msg(text="no media here"), pk)
    before = repo.message_get(rt.db, "tg", "4242", "m1")
    asyncio.run(tglog.log_change(rt, _msg(text=None, is_delete=True), before))
    fake = _FakeBot()
    rt.clients["notifier"] = fake
    asyncio.run(logworker._drain_once(rt))
    assert fake.photos == [] and len(fake.messages) == 1
    assert "no media here" in fake.messages[0]["text"]
