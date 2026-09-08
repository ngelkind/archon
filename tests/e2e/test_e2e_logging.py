"""Message auditing: deleted/edited-message cards reach the log channel.

R3: on the live bot 27,444 edit/delete events produced 65 cards, most drops
untraced. Cards are now durable outbox rows drained by a worker; every skip is
audited with a reason. These scenarios drive the real pipeline + log worker and
assert on what lands in the log channel via the fake Bot API.
"""

from __future__ import annotations

import pytest

from archon.db import repo
from archon.logging_ import logworker
from archon.testing.harness import Harness
from archon.testing.scripted_llm import ScriptedProvider

from conftest import run_async

pytestmark = pytest.mark.e2e
LOG = -100777  # harness tg_log_channel_id


async def _start(tmp_path, monkeypatch, **settings) -> Harness:
    monkeypatch.setattr(logworker, "TICK_S", 0.02)
    s = {"log_coalesce_seconds": 0.05, **settings}
    # The seed (original) messages are private -> monitored -> triaged; a
    # standing "ignore" absorbs that. These scenarios are about the cards.
    h = await Harness.start(tmp_path, subsystems=("pipeline", "control_bot", "logworker"),
                            bot_api=True, settings=s,
                            script=ScriptedProvider().triage("ignore", once=False))
    await h.wait_for_audit("control_bot_started")
    return h


def _cards(h: Harness) -> list[str]:
    return h.bot_api.texts("sendMessage", chat_id=LOG)


@run_async
async def test_a_deleted_private_message_produces_a_card_with_its_text(tmp_path, monkeypatch):
    async with await _start(tmp_path, monkeypatch) as h:
        # DMs default to log_deletes on. Seed the original, then delete it.
        await h.publish(h.make_message(platform="tg", chat_id="4242", chat_kind="private",
                                       source="business", msg_id="m1",
                                       text="the secret plan", sender_name="Dana"))
        await h.wait_for(lambda: h.row("SELECT 1 FROM messages WHERE msg_id='m1'"),
                         what="message cached")
        await h.publish(h.make_message(platform="tg", chat_id="4242", chat_kind="private",
                                       source="business", msg_id="m1", is_delete=True))
        card = await h.bot_api.wait_for_call("sendMessage", chat_id=LOG)
        assert "Message deleted" in card.data["text"]
        assert "the secret plan" in card.data["text"]
        assert "Dana" in card.data["text"]
        assert h.audit("tglog_sent", chat="4242")


@run_async
async def test_an_edited_message_shows_before_and_after(tmp_path, monkeypatch):
    async with await _start(tmp_path, monkeypatch) as h:
        await h.publish(h.make_message(platform="tg", chat_id="4242", chat_kind="private",
                                       source="business", msg_id="e1", text="meet at 5"))
        await h.wait_for(lambda: h.row("SELECT 1 FROM messages WHERE msg_id='e1'"), what="cached")
        await h.publish(h.make_message(platform="tg", chat_id="4242", chat_kind="private",
                                       source="business", msg_id="e1", text="meet at 6",
                                       is_edit=True))
        card = await h.bot_api.wait_for_call("sendMessage", chat_id=LOG)
        assert "Before:" in card.data["text"] and "meet at 5" in card.data["text"]
        assert "After:" in card.data["text"] and "meet at 6" in card.data["text"]


@run_async
async def test_rapid_edits_of_one_message_coalesce_into_one_card(tmp_path, monkeypatch):
    async with await _start(tmp_path, monkeypatch, log_coalesce_seconds=2.0) as h:
        await h.publish(h.make_message(platform="tg", chat_id="4242", chat_kind="private",
                                       source="business", msg_id="c1", text="v0"))
        await h.wait_for(lambda: h.row("SELECT 1 FROM messages WHERE msg_id='c1'"), what="cached")
        for v in ("v1", "v2", "v3"):
            await h.publish(h.make_message(platform="tg", chat_id="4242", chat_kind="private",
                                           source="business", msg_id="c1", text=v, is_edit=True))
        await h.wait_for(lambda: h.row(
            "SELECT COUNT(*) FROM log_outbox WHERE msg_id='c1' AND after_text='v3'")[0] == 1,
            what="one coalesced row holding the latest edit")
        card = await h.bot_api.wait_for_call("sendMessage", chat_id=LOG, timeout=6)
        # First before (v0), latest after (v3); the middle edits merged.
        assert "v0" in card.data["text"] and "v3" in card.data["text"]
        assert "v1" not in card.data["text"] and "v2" not in card.data["text"]
        assert len(_cards(h)) == 1


@run_async
async def test_a_burst_becomes_a_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(logworker, "DIGEST_THRESHOLD", 3)
    async with await _start(tmp_path, monkeypatch) as h:
        h.chat("tg", "-100500", name="Busy Group", kind="group", log_deletes=True)
        for i in range(6):
            await h.publish(h.make_message(platform="tg", chat_id="-100500",
                                           source="userbot", msg_id=f"b{i}",
                                           text=f"orig {i}"))
        await h.wait_for(lambda: h.row("SELECT COUNT(*) FROM messages WHERE chat_id='-100500'")[0] == 6,
                         what="seeded")
        for i in range(6):
            await h.publish(h.make_message(platform="tg", chat_id="-100500", source="userbot",
                                           msg_id=f"b{i}", is_delete=True))
        digest = await h.bot_api.wait_for_call("sendMessage", chat_id=LOG)
        assert "message changes" in digest.data["text"] and "Busy Group" in digest.data["text"]
        assert h.audit("tglog_digest_sent", chat="-100500")


@run_async
async def test_a_group_without_logging_is_skipped_with_a_reason(tmp_path, monkeypatch):
    async with await _start(tmp_path, monkeypatch) as h:
        h.chat("tg", "-100600", name="Quiet", kind="group", log_deletes=False)
        await h.publish(h.make_message(platform="tg", chat_id="-100600", source="userbot",
                                       msg_id="s1", text="hi"))
        await h.wait_for(lambda: h.row("SELECT 1 FROM messages WHERE msg_id='s1'"), what="cached")
        await h.publish(h.make_message(platform="tg", chat_id="-100600", source="userbot",
                                       msg_id="s1", is_delete=True))
        skip = await h.wait_for_audit("tglog_skipped", chat="-100600")
        assert skip["reason"] == "log_deletes_off"
        assert _cards(h) == []


@run_async
async def test_a_flood_429_does_not_lose_the_card(tmp_path, monkeypatch):
    async with await _start(tmp_path, monkeypatch) as h:
        h.bot_api.refuse_next("sendMessage", status=429, retry_after=1)
        await h.publish(h.make_message(platform="tg", chat_id="4242", chat_kind="private",
                                       source="business", msg_id="f1", text="keep me"))
        await h.wait_for(lambda: h.row("SELECT 1 FROM messages WHERE msg_id='f1'"), what="cached")
        await h.publish(h.make_message(platform="tg", chat_id="4242", chat_kind="private",
                                       source="business", msg_id="f1", is_delete=True))
        card = await h.bot_api.wait_for_call("sendMessage", chat_id=LOG, timeout=6)
        assert "keep me" in card.data["text"]
        await h.wait_for(
            lambda: h.row("SELECT sent_at FROM log_outbox WHERE msg_id='f1'")[0] is not None,
            what="the card marked sent after the retry")


@run_async
async def test_redaction_failure_suppresses_the_card_rather_than_leaking(tmp_path, monkeypatch):
    from archon.logging_ import redact as redact_mod

    def boom(_text):
        raise RuntimeError("redactor broke")

    monkeypatch.setattr(redact_mod, "scrub", boom)
    async with await _start(tmp_path, monkeypatch) as h:
        repo.setting_set(h.rt.db, "log.redact_pii", True)
        await h.publish(h.make_message(platform="tg", chat_id="4242", chat_kind="private",
                                       source="business", msg_id="r1", text="0512345678 secret"))
        await h.wait_for(lambda: h.row("SELECT 1 FROM messages WHERE msg_id='r1'"), what="cached")
        await h.publish(h.make_message(platform="tg", chat_id="4242", chat_kind="private",
                                       source="business", msg_id="r1", is_delete=True))
        await h.wait_for_audit("tglog_suppressed_redaction", chat="4242")
        assert not any("secret" in c for c in _cards(h))
