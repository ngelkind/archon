"""The harness itself must be trustworthy before scenarios lean on it.

These tests prove: the harness runs the PRODUCTION wiring (every tool module on
disk is registered), the scripted LLM refuses to improvise, the fast debounce
is confined to the harness (the production default is pinned), and a message
published on the bus really travels gate → debounce → triage → agent → tool →
confirm gate with only the LLM faked.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

from archon import tools as tools_pkg
from archon.db import repo
from archon.llm.base import ChatMessage, ProviderError
from archon.pipeline import ingest
from archon.testing.harness import PRODUCTION_DEBOUNCE_S, Harness
from archon.testing.repo_ledger import Ledger, recording_repo
from archon.testing.scripted_llm import (
    CHEAP_MODEL, STRONG_MODEL, FlakyProvider, ScriptedProvider, UnscriptedLLMCall,
)
from archon.tools.registry import Registry

from conftest import run_async


def test_production_debounce_is_what_the_harness_restores():
    """The harness lowers the debounce; this pins the value the deploy runs."""
    assert ingest._DEBOUNCE_S == PRODUCTION_DEBOUNCE_S


@run_async
async def test_harness_registers_every_tool_module_on_disk(tmp_path):
    expected: set[str] = set()
    for info in pkgutil.iter_modules(tools_pkg.__path__):
        mod = importlib.import_module(f"archon.tools.{info.name}")
        if not hasattr(mod, "register") or not inspect.isfunction(mod.register):
            continue
        reg = Registry()
        mod.register(reg)
        expected |= set(reg._tools)
    assert expected, "no tool modules found — the scan is broken"
    async with await Harness.start(tmp_path) as h:
        registered = set(h.rt.registry._tools)  # type: ignore[union-attr]
        missing = expected - registered
        assert not missing, f"app._wire_llm_and_tools does not register: {sorted(missing)}"
        # And the production debounce is back once the harness stops.
    assert ingest._DEBOUNCE_S == PRODUCTION_DEBOUNCE_S


@run_async
async def test_scripted_provider_refuses_to_improvise():
    llm = ScriptedProvider().triage("calendar")
    first = await llm.complete(model=CHEAP_MODEL, system="s",
                               messages=[ChatMessage(role="user", text="dentist at 3")])
    assert '"calendar"' in first.text
    with pytest.raises(UnscriptedLLMCall):
        await llm.complete(model=CHEAP_MODEL, system="s",
                           messages=[ChatMessage(role="user", text="again")])
    assert len(llm.unscripted) == 1
    assert not isinstance(UnscriptedLLMCall("x"), ProviderError), (
        "an unscripted call must not look like a provider outage — the pipeline "
        "swallows ProviderError into a silent 'ignore'"
    )


@run_async
async def test_harness_stop_fails_the_scenario_on_unscripted_calls(tmp_path):
    h = await Harness.start(tmp_path)
    h.chat("tg", "-1001", whitelisted=True)
    await h.publish(h.make_message(chat_id="-1001", text="meeting at 5"))
    await h.wait_for(lambda: h.llm.unscripted, what="the unscripted triage call")
    with pytest.raises(AssertionError, match="unscripted LLM call"):
        await h.stop()


@run_async
async def test_message_travels_gate_triage_agent_tool_confirm(tmp_path):
    """Scenario A's spine with only the LLM faked: a whitelisted group message
    reaches triage, the agent is offered the calendar tools, it calls
    calendar_create_event, and the inbound scope routes that through the
    confirm gate — a pending_actions row of kind event.create."""
    script = (
        ScriptedProvider()
        .triage("calendar", when=r"dentist")
        .tool_call("calendar_create_event", title="Dentist", start_iso="2030-05-01T15:00:00")
        .final("Created a pending event for the dentist.")
    )
    async with await Harness.start(tmp_path, script=script, record_repo=True) as h:
        h.chat("tg", "-1001", name="Family", whitelisted=True)
        await h.publish(h.make_message(chat_id="-1001", text="dentist tomorrow at 15:00"))
        await h.wait_for_audit("inbound_agent_done", chat="-1001")

        gate = h.gate_rows(chat="-1001")
        assert gate and gate[-1]["allowed"] is True and gate[-1]["action"] == "whitelisted"
        triage = h.audit("triage")
        assert triage and triage[-1]["verdict"] == "calendar"
        assert "calendar_create_event" in h.llm.offered_tools()
        assert "whitelist_add" not in h.llm.offered_tools(), "inbound runs must not get admin tools"
        tool = h.tool_rows("calendar_create_event")
        assert tool and tool[-1]["ok"] is True
        pending = h.rows("SELECT kind, status FROM pending_actions")
        assert [(r["kind"], r["status"]) for r in pending] == [("event.create", "pending")]
        assert h.audit("confirm_requested")
        assert h.ledger is not None and not h.ledger.offenders
        h.assert_non_vacuous()


@run_async
async def test_not_whitelisted_never_reaches_the_llm(tmp_path):
    """Pins the live diagnosis: 0 of 267 Telegram chats whitelisted → zero
    triage calls. If the gate ever changed by accident, this notices."""
    async with await Harness.start(tmp_path) as h:
        h.chat("tg", "-1002", name="Strangers")
        await h.publish(h.make_message(chat_id="-1002", text="dentist tomorrow at 15:00"))
        await h.wait_for_audit("not_whitelisted", chat="-1002")
        assert h.llm.requests == []


@run_async
async def test_flaky_provider_is_a_provider_error():
    with pytest.raises(ProviderError):
        await FlakyProvider().complete(model=STRONG_MODEL, system="", messages=[])


def test_recording_repo_restores_the_module(tmp_path):
    from archon.db import Db
    from archon.db.migrations import migrate

    db = Db(tmp_path / "x.db")
    migrate(db)
    original = repo.chat_get
    with recording_repo() as led:
        repo.chat_get(db, "tg", "nope")
        assert led.names() == ["chat_get"] and led.tenants() == set()
        assert not led.offenders
        assert repo.chat_get is not original
    assert repo.chat_get is original
    assert isinstance(led, Ledger)
