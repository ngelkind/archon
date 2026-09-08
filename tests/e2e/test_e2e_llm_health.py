"""The LLM router and triage make failures VISIBLE, not silent.

Live, an exhausted budget or a bad key turned every triage into the same
verdict as "boring message", so inbound processing could be 100% dead while
every log line looked normal.
"""

from __future__ import annotations

import pytest

from archon.db import repo
from archon.llm.base import BudgetExhausted, LLMResult, ProviderError, Truncated, Usage
from archon.llm.router import Router
from archon.testing.harness import Harness
from archon.testing.scripted_llm import FlakyProvider, ScriptedProvider

from conftest import run_async
from test_m2 import make_rt

pytestmark = pytest.mark.e2e


def _install(rt, provider):
    rt.router = Router(rt)
    rt.router._providers["scripted"] = provider
    repo.setting_set(rt.db, "llm.active_provider", "scripted")
    repo.setting_set(rt.db, "llm.model.scripted.cheap", "scripted:cheap")
    repo.setting_set(rt.db, "llm.model.scripted.strong", "scripted:strong")


@run_async
async def test_budget_exhaustion_is_typed_recorded_and_alerts(tmp_path):
    rt = make_rt(tmp_path)
    _install(rt, ScriptedProvider().triage("ignore"))
    repo.setting_set(rt.db, "llm.daily_budget_usd", 0.01)
    repo.llm_call_record(rt.db, purpose="agent", provider="scripted", model="m",
                         in_tokens=0, out_tokens=0, cost_usd=1.0, ok=True)
    with pytest.raises(BudgetExhausted):
        await rt.router.complete(purpose="triage", system="s", messages=[])
    # Recorded as a failed call and audited, not raised into a void.
    blocked = [r for r in repo.audit_query(rt.db, limit=10) if r["action"] == "llm_blocked"]
    assert blocked
    assert rt.health["llm"].startswith("failing")


@run_async
async def test_a_pre_flight_failure_writes_a_failed_call_row(tmp_path):
    rt = make_rt(tmp_path)
    _install(rt, ScriptedProvider())
    repo.setting_set(rt.db, "llm.model.scripted.cheap", "")  # no model configured
    before = repo.llm_cost_since(rt.db, "-1 day")["calls"]
    with pytest.raises(ProviderError):
        await rt.router.complete(purpose="triage", system="s", messages=[])
    after = repo.llm_cost_since(rt.db, "-1 day")["calls"]
    assert after == before + 1  # the refusal is a recorded call, ok=0


@run_async
async def test_truncation_is_raised_not_returned_as_empty(tmp_path):
    rt = make_rt(tmp_path)

    class Cut(ScriptedProvider):
        async def complete(self, **kw):
            return LLMResult(text="", tool_calls=[], usage=Usage(), model=kw["model"],
                             provider="scripted", stop_reason="max_tokens")

    _install(rt, Cut())
    with pytest.raises(Truncated):
        await rt.router.complete(purpose="agent", system="s", messages=[])


@run_async
async def test_triage_outage_is_surfaced_and_alerts_after_three(tmp_path):
    async with await Harness.start(tmp_path, subsystems=("pipeline", "control_bot"),
                                   bot_api=True, script=FlakyProvider()) as h:
        await h.wait_for_audit("control_bot_started")
        for i in range(3):
            await h.publish(h.make_message(platform="tg", chat_id=f"outage{i}",
                                           chat_kind="private", source="business",
                                           text="anything"))
            await h.wait_for_audit("triage_unavailable", chat=f"outage{i}")
        # No message was filed as a real "ignore"; the outage is distinct.
        assert h.audit("triage") == []
        assert h.audit("triage_unavailable")
        # The owner is warned — by the router's "LLM calls are failing" alert
        # and/or triage's own "Triage keeps failing"; either reaches them.
        await h.bot_api.wait_for_call("sendMessage", chat_id=1)
        texts = " ".join(h.bot_api.texts("sendMessage", chat_id=1))
        assert "failing" in texts


@run_async
async def test_recovery_clears_the_failure_streak(tmp_path):
    rt = make_rt(tmp_path)
    flaky = FlakyProvider()
    _install(rt, flaky)
    for _ in range(2):
        with pytest.raises(ProviderError):
            await rt.router.complete(purpose="triage", system="s", messages=[])
    assert rt.health["llm"].startswith("failing")
    # Swap in a working provider; a success clears the health flag.
    rt.router._providers["scripted"] = ScriptedProvider().reply("ok", once=False)
    await rt.router.complete(purpose="triage", system="s", messages=[])
    assert rt.health["llm"] == "ok"
