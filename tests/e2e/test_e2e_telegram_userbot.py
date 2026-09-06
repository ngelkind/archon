"""Telegram group ingestion through the REAL userbot handlers.

The handlers used to be closures inside ``userbot.run`` and were unreachable
without a live MTProto session — zero tests ever fired one. These scenarios
drive them with a fake Telethon client and assert on what the pipeline then
did (gate rows, cached messages, LLM calls, deletion attribution).
"""

from __future__ import annotations

import pytest

from archon.db import repo
from archon.platforms.telegram import userbot
from archon.testing.fake_telethon import (
    FakeChat, FakeDialog, FakeTelethonClient, FakeUser, deleted, edited_message, new_message,
)
from archon.testing.harness import Harness
from archon.testing.scripted_llm import ScriptedProvider

from conftest import run_async

pytestmark = pytest.mark.e2e

GROUP = FakeChat(id=-1001000, title="Family")
STRANGERS = FakeChat(id=-1002000, title="Strangers")
LOG_CHANNEL = FakeChat(id=-100777, title="Archon log", broadcast=True)


def _client() -> FakeTelethonClient:
    return FakeTelethonClient(dialogs=[
        FakeDialog(id=GROUP.id, name=GROUP.title, is_group=True),
        FakeDialog(id=STRANGERS.id, name=STRANGERS.title, is_group=True),
        FakeDialog(id=42, name="Dana", is_user=True),
    ])


async def _start(tmp_path, tg, script=None) -> Harness:
    h = await Harness.start(tmp_path, subsystems=("pipeline", "tg_userbot"),
                            telethon=tg, script=script)
    await h.wait_for_audit("tg_dialogs_synced")
    return h


@run_async
async def test_userbot_boots_syncs_dialogs_and_registers_the_client(tmp_path):
    tg = _client()
    async with await _start(tmp_path, tg) as h:
        assert h.audit("tg_dialogs_synced")[-1]["count"] == 3
        assert h.rt.clients["tg_userbot"] is tg
        assert h.rt.health["tg_userbot"] == "connected"
        kinds = {r["chat_id"]: r["kind"] for r in h.rows("SELECT chat_id, kind FROM chats")}
        assert kinds[str(GROUP.id)] == "group" and kinds["42"] == "private"
        assert {k for k, _ in tg.handlers} == {"new", "edit", "delete"}


@run_async
async def test_group_message_in_unwhitelisted_chat_never_reaches_the_llm(tmp_path):
    """Scenario B, through the adapter this time: the message IS cached (so a
    later deletion can be diffed) but the gate stops it before triage."""
    tg = _client()
    async with await _start(tmp_path, tg) as h:
        await tg.fire(new_message(STRANGERS, 501, "dentist tomorrow at 15:00"))
        gate = await h.wait_for_audit("not_whitelisted", chat=str(STRANGERS.id))
        assert gate["allowed"] is False
        cached = h.row("SELECT text, source FROM messages WHERE chat_id=? AND msg_id='501'",
                       (str(STRANGERS.id),))
        assert cached["text"] == "dentist tomorrow at 15:00" and cached["source"] == "userbot"
        assert h.llm.requests == []


@run_async
async def test_whitelisted_group_message_is_triaged(tmp_path):
    tg = _client()
    script = ScriptedProvider().triage("ignore", reason="small talk")
    async with await _start(tmp_path, tg, script) as h:
        h.chat("tg", str(GROUP.id), whitelisted=True)
        await tg.fire(new_message(GROUP, 502, "how are you all"))
        note = await h.wait_for_audit("triage", chat=str(GROUP.id))
        assert note["verdict"] == "ignore" and note["tenant_id"] == 1
        assert len(h.llm.calls("cheap")) == 1
        assert "Dana" in h.llm.calls("cheap")[0].user_text


@run_async
async def test_private_chats_are_left_to_the_business_connection(tmp_path):
    tg = _client()
    async with await _start(tmp_path, tg) as h:
        await tg.fire(new_message(FakeUser(id=42, first_name="Dana"), 503, "hi"))
        await tg.fire(new_message(GROUP, 504, "group hello"))
        await h.wait_for_audit("not_whitelisted", chat=str(GROUP.id))
        assert h.gate_rows(chat="42") == []
        assert h.row("SELECT 1 FROM messages WHERE chat_id='42'") is None


@run_async
async def test_log_channel_is_never_re_ingested_even_after_retargeting(tmp_path):
    """The channel id is read per event: retarget at runtime, and the NEW
    channel is ignored while the old one is ingested again."""
    tg = _client()
    async with await _start(tmp_path, tg) as h:
        await tg.fire(new_message(LOG_CHANNEL, 601, "card"))
        await tg.fire(new_message(GROUP, 602, "marker"))
        await h.wait_for_audit("not_whitelisted", chat=str(GROUP.id))
        assert h.gate_rows(chat=str(LOG_CHANNEL.id)) == []

        other = FakeChat(id=-100999, title="New log", broadcast=True)
        repo.setting_set(h.rt.db, "log.channel_id", other.id)
        await tg.fire(new_message(other, 603, "card"))
        await tg.fire(new_message(LOG_CHANNEL, 604, "now a normal channel"))
        await h.wait_for_audit("not_whitelisted", chat=str(LOG_CHANNEL.id))
        assert h.gate_rows(chat=str(other.id)) == []


@run_async
async def test_group_edit_updates_the_cache(tmp_path):
    tg = _client()
    async with await _start(tmp_path, tg) as h:
        await tg.fire(new_message(GROUP, 701, "meet at 5"))
        await h.wait_for_audit("not_whitelisted", chat=str(GROUP.id))
        await tg.fire(edited_message(GROUP, 701, "meet at 6"))
        await h.wait_for_audit("edit_or_delete_event", chat=str(GROUP.id))
        row = h.row("SELECT text, edited_text FROM messages WHERE msg_id='701'")
        assert (row["text"], row["edited_text"]) == ("meet at 5", "meet at 6")


@run_async
async def test_supergroup_delete_carries_its_chat(tmp_path):
    tg = _client()
    async with await _start(tmp_path, tg) as h:
        await tg.fire(new_message(GROUP, 801, "oops"))
        await h.wait_for_audit("not_whitelisted", chat=str(GROUP.id))
        await tg.fire(deleted(GROUP, [801]))
        await h.wait_for_audit("edit_or_delete_event", chat=str(GROUP.id))
        assert h.row("SELECT deleted_at FROM messages WHERE msg_id='801'")["deleted_at"]


@run_async
async def test_peerless_delete_never_stamps_an_unrelated_chat(tmp_path):
    """Scenario Q. A private-chat row (Business) and a supergroup row share
    message id 45231. A peerless delete must touch NEITHER: the supergroup's
    deletes always carry a peer, and the private chat's delete arrives through
    the Business connection. Only a legacy-group row written by this userbot
    is an unambiguous match."""
    tg = _client()
    async with await _start(tmp_path, tg) as h:
        await h.publish(h.make_message(platform="tg", chat_id="777001", chat_kind="private",
                                       source="business", msg_id="45231", text="private"))
        await h.publish(h.make_message(platform="tg", chat_id="-1001234567890",
                                       source="userbot", msg_id="45231", text="super"))
        await h.wait_for_audit("not_whitelisted", chat="-1001234567890")
        await h.wait_for_audit("not_whitelisted", chat="777001")

        await tg.fire(deleted(None, [45231]))
        await h.wait_for_audit("tg_delete_unresolved", msg_id="45231")
        stamped = h.rows(
            "SELECT chat_id FROM messages WHERE msg_id='45231' AND deleted_at IS NOT NULL")
        assert stamped == [], f"peerless delete stamped {[r['chat_id'] for r in stamped]}"

        legacy = FakeChat(id=-4321, title="Old group")
        await tg.fire(new_message(legacy, 45232, "legacy"))
        await h.wait_for_audit("not_whitelisted", chat=str(legacy.id))
        await tg.fire(deleted(None, [45232]))
        await h.wait_for_audit("edit_or_delete_event", chat=str(legacy.id))
        assert h.row("SELECT deleted_at FROM messages WHERE msg_id='45232'")["deleted_at"]


def test_the_old_resolution_would_have_stamped_the_wrong_chat(tmp_path):
    """Mutation check: the pre-fix lookup (any tg row with that id, newest
    first) picks the supergroup row for a peerless delete; the new one refuses."""
    from datetime import UTC, datetime

    from archon.models import InboundMessage

    from test_m2 import make_rt

    rt = make_rt(tmp_path)
    pk = repo.chat_upsert(rt.db, "tg", "-1001234567890", "Super", "group")
    ppk = repo.chat_upsert(rt.db, "tg", "777001", "Dana", "private")

    def msg(chat_id, kind, source):
        return InboundMessage(platform="tg", source=source, chat_id=chat_id, chat_kind=kind,
                              msg_id="45231", sender_id="9", ts=datetime.now(UTC), text="x")

    repo.message_upsert(rt.db, msg("777001", "private", "business"), ppk)
    repo.message_upsert(rt.db, msg("-1001234567890", "group", "userbot"), pk)
    old = repo.message_search(
        rt.db, "platform = 'tg' AND msg_id = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
        ("45231",))
    assert old[0]["chat_id"] == "-1001234567890"  # the bug: a supergroup gets stamped
    assert userbot.resolve_peerless_delete(rt, "45231") is None


@run_async
async def test_invalid_session_raises_so_the_supervisor_retries_and_no_client_is_left(tmp_path):
    tg = FakeTelethonClient(authorized=False)
    async with await Harness.start(tmp_path, subsystems=("pipeline", "tg_userbot"),
                                   telethon=tg) as h:
        await h.wait_for_audit("subsystem_crash", subsystem="tg_userbot")
        assert "tg_userbot" not in h.rt.clients
        assert h.rt.health["tg_userbot"].startswith("crashed")
        assert h.audit("tg_userbot_unauthorized")


@run_async
async def test_a_dropped_connection_is_a_crash_not_a_quiet_exit(tmp_path):
    tg = _client()
    async with await _start(tmp_path, tg) as h:
        assert "tg_userbot" in h.rt.clients
        tg.disconnect()
        await h.wait_for_audit("tg_userbot_disconnected")
        await h.wait_for_audit("subsystem_crash", subsystem="tg_userbot")
        assert "tg_userbot" not in h.rt.clients
