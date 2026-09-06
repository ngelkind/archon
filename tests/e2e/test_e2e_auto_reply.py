"""Auto-reply: the cheap "answering agent" writes a reply to each message in
an opted-in chat and it is SENT through the same executors the confirm gate
uses. Scenario M — this FAILED on the branch before the fix: the executors had
grown a third ``store`` parameter and the immediate send path still called
them with two, raising TypeError that was swallowed as auto_reply_send_failed.
"""

from __future__ import annotations

import pytest

from archon.db import repo
from archon.pipeline import ingest
from archon.testing import fake_neonize as wa
from archon.testing.fake_telethon import FakeChat, FakeDialog, FakeTelethonClient, new_message
from archon.testing.harness import Harness
from archon.testing.scripted_llm import ScriptedProvider

from conftest import run_async

pytestmark = pytest.mark.e2e

GROUP = FakeChat(id=-1001000, title="Family")
WA_GROUP = "120363418193527511@g.us"


@run_async
async def test_immediate_telegram_auto_reply_is_sent_as_the_owner(tmp_path):
    tg = FakeTelethonClient(dialogs=[FakeDialog(id=GROUP.id, name=GROUP.title, is_group=True)])
    script = (
        ScriptedProvider()
        .triage("respond", reason="a question for the owner")
        .reply("Sure, 7pm works for me.")
    )
    async with await Harness.start(tmp_path, subsystems=("pipeline", "tg_userbot"),
                                   telethon=tg, script=script) as h:
        await h.wait_for_audit("tg_dialogs_synced")
        h.chat("tg", str(GROUP.id), whitelisted=True, auto_reply=True)
        await tg.fire(new_message(GROUP, 901, "does 7pm work for you?"))
        done = await h.wait_for_audit("auto_reply_done", chat=str(GROUP.id))
        assert done["replied"] == 1 and done["queued"] is False
        assert tg.sent and tg.sent[-1]["text"] == "Sure, 7pm works for me."
        assert tg.sent[-1]["reply_to"] == 901
        assert h.audit("auto_reply_send_failed") == []
        assert h.audit("tool_bug") == []


@run_async
async def test_immediate_whatsapp_auto_reply_is_sent(tmp_path):
    client = wa.FakeAClient(groups=[wa.FakeGroup(WA_GROUP, "kkk")])
    script = ScriptedProvider().triage("respond").reply("On my way!")
    async with await Harness.start(tmp_path, subsystems=("pipeline", "whatsapp"),
                                   neonize=client, script=script) as h:
        await h.wait_for(lambda: client.connect_task is not None, what="connect() called")
        await client.go_online()
        await h.wait_for_audit("wa_groups_synced")
        repo.setting_set(h.rt.db, "wa.send_delay_min_s", 0.0)
        repo.setting_set(h.rt.db, "wa.send_delay_max_s", 0.0)
        h.chat("wa", WA_GROUP, whitelisted=True, auto_reply=True)
        await client.fire(wa.text_message(WA_GROUP, "where are you?"))
        await h.wait_for_audit("auto_reply_done", chat=WA_GROUP, timeout=15)
        assert client.sent and client.sent[-1]["message"] == "On my way!"
        assert client.sent[-1]["to"] == WA_GROUP
        # The outbound message is cached under the tenant (the raw INSERT that
        # omitted tenant_id used to be skipped by INSERT OR IGNORE).
        row = h.row("SELECT text, is_from_me, tenant_id FROM messages WHERE msg_id=?",
                    (client.sent[-1]["id"],))
        assert row is not None and row["is_from_me"] == 1 and row["tenant_id"] == 1


@run_async
async def test_own_messages_are_never_auto_replied_to(tmp_path):
    tg = FakeTelethonClient(dialogs=[FakeDialog(id=GROUP.id, name=GROUP.title, is_group=True)])
    script = ScriptedProvider().triage("respond").reply("should not be sent")
    async with await Harness.start(tmp_path, subsystems=("pipeline",), script=script) as h:
        h.chat("tg", str(GROUP.id), whitelisted=True, auto_reply=True)
        await h.publish(h.make_message(chat_id=str(GROUP.id), text="note to self",
                                       is_from_me=True))
        await h.wait_for_audit("from_me", chat=str(GROUP.id))
        assert h.llm.requests == []
        assert tg.sent == []


@run_async
async def test_a_gmail_auto_reply_is_refused_loudly(tmp_path):
    """The old code had no branch for gmail: it returned None and the caller
    counted a successful send."""
    from archon.db.tenancy import TenantScope

    async with await Harness.start(tmp_path, subsystems=()) as h:
        with pytest.raises(NotImplementedError):
            await ingest._send_reply_now(h.rt, TenantScope(h.rt.db, 1), "gmail",
                                         "a@example.com", "email", "hi")


@run_async
async def test_the_old_two_argument_call_was_the_bug(tmp_path):
    """Mutation check: calling the executors the old way raises TypeError."""
    from archon.tools.telegram import _send_group_executor

    async with await Harness.start(tmp_path, subsystems=()) as h:
        with pytest.raises(TypeError):
            await _send_group_executor(h.rt, {"chat_id": "-1", "text": "x", "reply_to": None})
