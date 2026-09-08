"""The owner-facing commands that make the monitor/whitelist policy reachable
from the phone — driven through the real polling loop and the fake Bot API.
"""

from __future__ import annotations

import pytest

from archon.db import repo
from archon.testing.harness import Harness

from conftest import run_async

pytestmark = pytest.mark.e2e
OWNER = 1


async def _start(tmp_path) -> Harness:
    h = await Harness.start(tmp_path, subsystems=("control_bot",), bot_api=True)
    await h.wait_for_audit("control_bot_started")
    return h


@run_async
async def test_whitelist_by_name_flips_the_gate_flag(tmp_path):
    async with await _start(tmp_path) as h:
        h.chat("tg", "-1001", name="Family Group", kind="group")
        reply = await h.owner_says("/whitelist family")
        assert "Whitelisted" in reply and "Family Group" in reply
        assert h.row("SELECT is_whitelisted FROM chats WHERE chat_id='-1001'")[0] == 1
        assert h.audit("whitelist_add", chat="-1001", via="command")

        off = await h.owner_says("/unwhitelist family")
        assert "Removed" in off
        assert h.row("SELECT is_whitelisted FROM chats WHERE chat_id='-1001'")[0] == 0


@run_async
async def test_whitelist_by_exact_id(tmp_path):
    async with await _start(tmp_path) as h:
        h.chat("tg", "-100777001", name=None, kind="group")
        reply = await h.owner_says("/whitelist -100777001")
        assert "Whitelisted" in reply
        assert h.row("SELECT is_whitelisted FROM chats WHERE chat_id='-100777001'")[0] == 1


@run_async
async def test_whitelist_ambiguous_lists_candidates(tmp_path):
    async with await _start(tmp_path) as h:
        h.chat("tg", "-1001", name="Work Team", kind="group")
        h.chat("tg", "-1002", name="Work Friends", kind="group")
        reply = await h.owner_says("/whitelist work")
        assert "Several chats match" in reply
        assert "-1001" in reply and "-1002" in reply
        assert h.row("SELECT SUM(is_whitelisted) FROM chats")[0] in (0, None)


@run_async
async def test_whitelist_unknown_name_is_reported(tmp_path):
    async with await _start(tmp_path) as h:
        reply = await h.owner_says("/whitelist nonexistent")
        assert "No chat matches" in reply


@run_async
async def test_monitor_command_reads_and_sets(tmp_path):
    async with await _start(tmp_path) as h:
        shown = await h.owner_says("/monitor")
        assert "private chats: <b>all</b>" in shown and "groups: <b>whitelist</b>" in shown
        set_groups = await h.owner_says("/monitor all groups")
        assert "monitor.groups" in set_groups
        assert repo.setting_get(h.rt.db, "monitor.groups", "x") == "all"
        set_priv = await h.owner_says("/monitor whitelist")
        assert repo.setting_get(h.rt.db, "monitor.private_chats", "x") == "whitelist"


@run_async
async def test_logall_bulk_toggles_group_logging(tmp_path):
    async with await _start(tmp_path) as h:
        for cid in ("-100a", "-100b"):
            pk = h.chat("tg", cid, kind="group")
            repo.chat_set_field(h.rt.db, pk, "log_deletes", 0)
        h.chat("tg", "99", kind="private")  # a DM, must be untouched by /logall
        reply = await h.owner_says("/logall on")
        assert "turned <b>on</b> for 2" in reply
        assert h.row("SELECT SUM(log_deletes) FROM chats WHERE kind='group'")[0] == 2
        assert repo.setting_get(h.rt.db, "log.groups_default", None) is True
        await h.owner_says("/logall off")
        assert h.row("SELECT SUM(log_deletes) FROM chats WHERE kind='group'")[0] == 0


@run_async
async def test_chats_lists_flags_and_counts(tmp_path):
    async with await _start(tmp_path) as h:
        pk = h.chat("tg", "-1001", name="Family", kind="group")
        repo.chat_set_field(h.rt.db, pk, "is_whitelisted", 1)
        h.chat("tg", "-1002", name="Strangers", kind="group")
        reply = await h.owner_says("/chats tg")
        assert "2, 1 whitelisted" in reply
        assert "Family" in reply and "Strangers" in reply


@run_async
async def test_help_lists_every_command(tmp_path):
    async with await _start(tmp_path) as h:
        reply = await h.owner_says("/help")
        for cmd in ("/whitelist", "/monitor", "/logall", "/chats", "/status", "/wa_pair"):
            assert cmd in reply, cmd
        assert "plain words" in reply


@run_async
async def test_command_menu_matches_help(tmp_path):
    async with await _start(tmp_path) as h:
        cmds = {c["command"] for c in h.bot_api.calls_of("setMyCommands")[-1].json("commands")}
        assert {"whitelist", "monitor", "logall", "chats", "approvals"} <= cmds


@run_async
async def test_approvals_lists_pending(tmp_path):
    from archon.pipeline import confirm

    async with await _start(tmp_path) as h:
        empty = await h.owner_says("/approvals")
        assert "No pending approvals" in empty
        await confirm.request_confirmation(h.rt, kind="event.create",
                                           payload={"title": "X"}, description="d")
        reply = await h.owner_says("/approvals")
        assert "1 pending" in reply and "event.create" in reply
