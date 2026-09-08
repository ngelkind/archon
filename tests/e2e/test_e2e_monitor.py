"""Monitor-everything: the R1 root cause. On the live bot 0 of 267 Telegram
chats were ever whitelisted, so nothing from Telegram reached triage. The
default is now to monitor all private chats (own messages included, never
auto-replied to) while groups stay whitelist-gated for triage; every group is
logged for edits/deletes.
"""

from __future__ import annotations

import pytest

from archon.db import repo
from archon.db.migrations import backfill_monitor_defaults
from archon.testing.fake_telethon import FakeChat, FakeDialog, FakeTelethonClient, new_message
from archon.testing.harness import Harness
from archon.testing.scripted_llm import ScriptedProvider

from conftest import run_async

pytestmark = pytest.mark.e2e

GROUP = FakeChat(id=-1001000, title="Family")


async def _userbot(tmp_path, script=None, **settings):
    tg = FakeTelethonClient(dialogs=[FakeDialog(id=GROUP.id, name=GROUP.title, is_group=True)])
    h = await Harness.start(tmp_path, subsystems=("pipeline", "tg_userbot"),
                            telethon=tg, script=script, settings=settings or None)
    await h.wait_for_audit("tg_dialogs_synced")
    return h, tg


@run_async
async def test_a_private_business_message_is_triaged_with_no_whitelisting(tmp_path):
    """The headline fix: a private chat reaches triage out of the box."""
    script = ScriptedProvider().triage("calendar", when="dentist") \
        .tool_call("calendar_create_event", title="Dentist", start_iso="2030-05-01T15:00:00") \
        .final("Added it.")
    async with await Harness.start(tmp_path, subsystems=("pipeline",), script=script) as h:
        # No chat row exists yet — exactly the live state. The gate must still
        # admit it (it upserts the row itself upstream in run()).
        await h.publish(h.make_message(platform="tg", chat_id="55501", chat_kind="private",
                                       source="business", text="dentist tomorrow 15:00"))
        note = await h.wait_for_audit("triage", chat="55501")
        assert note["verdict"] == "calendar"
        gate = h.gate_rows(chat="55501")[-1]
        assert gate["allowed"] is True and gate["action"] == "monitored_private"
        await h.wait_for_audit("confirm_requested")


@run_async
async def test_a_group_still_needs_whitelisting_for_triage(tmp_path):
    h, tg = await _userbot(tmp_path)
    try:
        await tg.fire(new_message(GROUP, 1, "lunch at noon?"))
        await h.wait_for_audit("not_whitelisted", chat=str(GROUP.id))
        assert h.llm.requests == []
    finally:
        await h.stop()


@run_async
async def test_monitor_groups_all_opens_groups_up(tmp_path):
    h, tg = await _userbot(tmp_path, ScriptedProvider().triage("ignore"),
                           monitor_groups="all")
    try:
        await tg.fire(new_message(GROUP, 2, "anyone around?"))
        note = await h.wait_for_audit("triage", chat=str(GROUP.id))
        assert note["verdict"] == "ignore"
    finally:
        await h.stop()


@run_async
async def test_whitelist_mode_restores_the_old_behaviour(tmp_path):
    async with await Harness.start(tmp_path, subsystems=("pipeline",),
                                   settings={"monitor_private_chats": "whitelist"}) as h:
        await h.publish(h.make_message(platform="tg", chat_id="55502", chat_kind="private",
                                       source="business", text="hi"))
        await h.wait_for_audit("not_whitelisted", chat="55502")
        assert h.llm.requests == []


@run_async
async def test_an_own_message_is_triaged_but_never_auto_replied(tmp_path):
    script = ScriptedProvider().triage("respond").reply("SHOULD NOT SEND")
    async with await Harness.start(tmp_path, subsystems=("pipeline",), script=script) as h:
        pk = h.chat("tg", "55503", kind="private")
        repo.chat_set_field(h.rt.db, pk, "auto_reply", 1)
        await h.publish(h.make_message(platform="tg", chat_id="55503", chat_kind="private",
                                       source="business", text="note to self",
                                       is_from_me=True))
        # It is triaged (admitted as monitored_private)...
        note = await h.wait_for_audit("triage", chat="55503")
        assert note["verdict"] == "respond"
        # ...but the auto-reply loop skips the owner's own message.
        done = await h.wait_for_audit("auto_reply_done", chat="55503")
        assert done["replied"] == 0


def test_backfill_enables_group_logging_once_and_is_idempotent(tmp_path):
    from test_m2 import make_rt

    rt = make_rt(tmp_path)
    g = repo.chat_upsert(rt.db, "tg", "-100900", "G", "group")
    ch = repo.chat_upsert(rt.db, "tg", "-100901", "C", "channel")
    dm = repo.chat_upsert(rt.db, "tg", "77", "D", "private")
    for pk in (g, ch, dm):
        repo.chat_set_field(rt.db, pk, "log_deletes", 0)

    assert backfill_monitor_defaults(rt) == 2  # the group and the channel
    assert rt.db.query_one("SELECT log_deletes FROM chats WHERE chat_id='-100900'")[0] == 1
    assert rt.db.query_one("SELECT log_deletes FROM chats WHERE chat_id='-100901'")[0] == 1
    assert rt.db.query_one("SELECT log_deletes FROM chats WHERE chat_id='77'")[0] == 0  # DM untouched here
    assert backfill_monitor_defaults(rt) == 0  # marker set: never runs twice


def test_backfill_never_runs_in_product_mode(tmp_path):
    from test_m2 import make_rt

    rt = make_rt(tmp_path)
    rt.settings.multitenant_enabled = True
    repo.chat_upsert(rt.db, "tg", "-100902", "G", "group")
    repo.chat_set_field(rt.db, repo.chat_get(rt.db, "tg", "-100902")["id"], "log_deletes", 0)
    assert backfill_monitor_defaults(rt) == 0
    assert rt.db.query_one("SELECT log_deletes FROM chats WHERE chat_id='-100902'")[0] == 0
