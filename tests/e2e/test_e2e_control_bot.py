"""The owner's control bot, driven through a real aiogram polling loop
against the fake Bot API server — the same dispatcher, filters and handlers
the live bot runs, with only the HTTP endpoint swapped.
"""

from __future__ import annotations

import pytest

from archon.testing.harness import Harness
from archon.testing.scripted_llm import ScriptedProvider

from conftest import run_async

pytestmark = pytest.mark.e2e

OWNER = 1
STRANGER = 4242


async def _start(tmp_path, script=None) -> Harness:
    h = await Harness.start(tmp_path, subsystems=("pipeline", "control_bot"),
                            bot_api=True, script=script)
    await h.wait_for_audit("control_bot_started")
    await h.bot_api.wait_for_call("getUpdates")
    return h


@run_async
async def test_boot_registers_commands_and_polls(tmp_path):
    async with await _start(tmp_path) as h:
        assert h.bot_api.calls_of("getMe")
        cmds = h.bot_api.calls_of("setMyCommands")[-1].json("commands")
        assert {c["command"] for c in cmds} >= {"status", "help", "ask", "costs"}
        assert h.rt.clients["control_bot"] is not None
        assert h.rt.clients["notifier"] is not None
        assert h.rt.health["control_bot"] == "polling"


@run_async
async def test_status_answers_with_subsystem_health(tmp_path):
    async with await _start(tmp_path) as h:
        reply = await h.owner_says("/status")
        assert "Archon status" in reply
        assert "pipeline: running" in reply
        assert "control_bot: polling" in reply
        assert "llm provider: scripted" in reply


@run_async
async def test_costs_answers_even_with_no_spend(tmp_path):
    async with await _start(tmp_path) as h:
        reply = await h.owner_says("/costs")
        assert "LLM costs" in reply


@run_async
async def test_unknown_command_gets_a_pointer_to_help(tmp_path):
    """Scenario P. A guessed command used to fall through every filter and
    vanish — no reply, no audit row."""
    async with await _start(tmp_path) as h:
        reply = await h.owner_says("/frobnicate now")
        assert "/help" in reply
        assert h.audit("unknown_command")[-1]["text"] == "/frobnicate now"


@run_async
async def test_unsupported_content_is_answered_not_dropped(tmp_path):
    async with await _start(tmp_path) as h:
        msg = h.bot_api._message(OWNER, OWNER, None)
        msg["sticker"] = {"file_id": "s1", "file_unique_id": "s1", "type": "regular",
                          "width": 1, "height": 1, "is_animated": False, "is_video": False}
        h.bot_api.inject({"message": msg})
        call = await h.bot_api.wait_for_call("sendMessage", chat_id=OWNER)
        assert "not supported" in call.data["text"]
        assert h.audit("unsupported_owner_message")[-1]["content_type"] == "sticker"


@run_async
async def test_strangers_are_ignored_and_audited(tmp_path):
    async with await _start(tmp_path) as h:
        h.bot_api.owner_message("/status", chat_id=STRANGER, from_id=STRANGER, first_name="Eve")
        h.bot_api.owner_message("hello?", chat_id=STRANGER, from_id=STRANGER, first_name="Eve")
        await h.wait_for_audit("non_owner_message", sender=STRANGER)
        # A marker from the owner proves both stranger updates were processed first.
        await h.owner_says("/costs")
        assert h.bot_api.calls_of("sendMessage", chat_id=STRANGER) == []


@run_async
async def test_free_text_runs_the_owner_agent_and_keeps_memory(tmp_path):
    script = ScriptedProvider().final("All good — nothing pending.")
    async with await _start(tmp_path, script) as h:
        reply = await h.owner_says("anything I should know?")
        assert reply == "All good — nothing pending."
        req = h.llm.calls("strong")[-1]
        assert "whitelist_add" in req.tool_names, "the owner turn must carry the full toolset"
        rows = h.rows("SELECT role, content FROM context_messages ORDER BY id")
        assert [(r["role"], r["content"]) for r in rows] == [
            ("user", "anything I should know?"),
            ("assistant", "All good — nothing pending."),
        ]


@run_async
async def test_owner_turn_that_uses_a_tool_reports_the_result(tmp_path):
    """The owner asks to whitelist a chat by name: chat_find → whitelist_add →
    the gate row flips, all through the real dispatcher and registry."""
    script = (
        ScriptedProvider()
        .tool_call("chat_find", approx_name="family")
        .tool_call("whitelist_add", platform="tg", chat_id="-1001000")
        .final("Done — Family is whitelisted.")
    )
    async with await _start(tmp_path, script) as h:
        h.chat("tg", "-1001000", name="Family")
        reply = await h.owner_says("whitelist the family group")
        assert reply == "Done — Family is whitelisted."
        assert h.row("SELECT is_whitelisted FROM chats WHERE chat_id='-1001000'")[0] == 1
        found = h.llm.calls("strong")[1].last_tool_result
        assert found and "-1001000" in found
        assert [t["action"] for t in h.tool_rows()] == ["chat_find", "whitelist_add"]


@run_async
async def test_a_handler_exception_reaches_the_owner(tmp_path, monkeypatch):
    """Before the errors observer, aiogram logged the exception and the owner
    saw a command do nothing."""
    from archon.platforms.telegram import control

    def explode(*_a, **_k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(control.repo, "llm_cost_since", explode)
    async with await _start(tmp_path) as h:
        reply = await h.owner_says("/costs")
        assert reply.startswith("⚠️ That failed: RuntimeError: db exploded")
        err = h.audit("handler_error")[-1]
        assert "db exploded" in err["error"]
        assert h.rt.health["control_bot"].startswith("degraded")


@run_async
async def test_an_empty_model_reply_is_still_a_reply(tmp_path):
    """Scenario O: range(0, 0) used to send nothing at all."""
    async with await _start(tmp_path, ScriptedProvider().final("")) as h:
        reply = await h.owner_says("hello?")
        assert "returned no text" in reply
        rows = h.rows("SELECT role FROM context_messages")
        assert [r["role"] for r in rows] == ["user"], "an empty assistant turn is not memory"


@run_async
async def test_a_strangers_button_tap_is_refused(tmp_path):
    async with await _start(tmp_path) as h:
        h.bot_api.callback(data="pa:1:ok", from_id=STRANGER)
        await h.wait_for_audit("non_owner_callback", sender=STRANGER)
        call = await h.bot_api.wait_for_call("answerCallbackQuery")
        assert call.data["text"] == "Not yours."


@run_async
async def test_selftest_rejects_unknown_steps_instead_of_reporting_zero_of_zero(tmp_path):
    async with await _start(tmp_path) as h:
        reply = await h.owner_says("/selftest whatsapp")
        assert reply.startswith("Unknown step(s): whatsapp")
        assert "wa_text" in reply
        assert h.audit("selftest_start") == []
