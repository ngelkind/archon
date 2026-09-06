"""The supervisor tells the owner when something dies (and only then).

Scenarios H2/H3 from the plan. Both FAILED against the previous supervisor,
which wrote an audit note, set a health string and never told anyone; the
mutation check at the bottom keeps that fact executable.
"""

from __future__ import annotations

import asyncio

import pytest

from archon import alerts
from archon.testing.harness import Harness

from conftest import run_async

pytestmark = pytest.mark.e2e

OWNER = 1


async def _start(tmp_path) -> Harness:
    h = await Harness.start(tmp_path, subsystems=("control_bot",), bot_api=True)
    await h.wait_for_audit("control_bot_started")
    return h


def _owner_texts(h: Harness) -> list[str]:
    return h.bot_api.texts("sendMessage", chat_id=OWNER)


@run_async
async def test_a_crash_loop_alerts_the_owner_on_the_second_failure(tmp_path):
    async with await _start(tmp_path) as h:
        attempts = 0

        async def broken() -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError(f"boom {attempts}")

        h.supervise("flaky", broken)
        await h.wait_for_audit("subsystem_crash", subsystem="flaky", count=2)
        call = await h.bot_api.wait_for_call("sendMessage", chat_id=OWNER)
        assert "flaky" in call.data["text"] and "crashed 2" in call.data["text"]
        sent = await h.wait_for_audit("owner_alert_sent", key="subsystem:flaky")
        assert "boom 2" in sent["text"]
        assert h.audit("subsystem_crash")[0]["consecutive"] == 1
        # The first failure did NOT alert: one bad restart is not news.
        assert len(h.bot_api.calls_of("sendMessage", chat_id=OWNER)) == 1
        # It keeps crashing but the owner is not spammed: same key, suppressed.
        await h.wait_for_audit("subsystem_crash", subsystem="flaky", count=4)
        assert h.audit("owner_alert_suppressed", key="subsystem:flaky")
        assert len(h.bot_api.calls_of("sendMessage", chat_id=OWNER)) == 1
        assert h.rt.health["flaky"].startswith("crashed: RuntimeError")


@run_async
async def test_a_quiet_death_is_restarted_and_alerted(tmp_path):
    """Scenario H3: the subsystem returns while claiming to run — the shape of
    the Telethon userbot dying for the rest of the process."""
    async with await _start(tmp_path) as h:
        runs = 0

        async def quits() -> None:
            nonlocal runs
            runs += 1
            return  # health still says "running"

        h.supervise("quitter", quits)
        await h.wait_for_audit("subsystem_stopped", subsystem="quitter", count=2)
        assert runs >= 2, "a quiet death must be restarted"
        call = await h.bot_api.wait_for_call("sendMessage", chat_id=OWNER)
        assert "quitter stopped unexpectedly 2" in call.data["text"]
        assert h.audit("subsystem_stopped")[0]["state"] == "stopped unexpectedly"


@run_async
async def test_a_deliberately_disabled_subsystem_stays_quiet(tmp_path):
    async with await _start(tmp_path) as h:
        runs = 0

        async def not_configured() -> None:
            nonlocal runs
            runs += 1
            h.rt.health["opt"] = "disabled (no credentials for opt)"

        h.supervise("opt", not_configured)
        await h.wait_for_audit("subsystem_idle", subsystem="opt")
        await asyncio.sleep(0.2)
        assert runs == 1
        assert _owner_texts(h) == []
        assert h.rt.health["opt"] == "disabled (no credentials for opt)"


@run_async
async def test_a_terminal_state_is_reported_once_and_not_restarted(tmp_path):
    async with await _start(tmp_path) as h:
        runs = 0

        async def logged_out() -> None:
            nonlocal runs
            runs += 1
            h.rt.health["wa"] = "LOGGED OUT — re-pair required"

        h.supervise("wa", logged_out)
        await h.wait_for_audit("subsystem_terminal", subsystem="wa")
        call = await h.bot_api.wait_for_call("sendMessage", chat_id=OWNER)
        assert "wa is down: LOGGED OUT" in call.data["text"]
        await asyncio.sleep(0.2)
        assert runs == 1


@run_async
async def test_alerts_raised_before_the_bot_exists_are_delivered_when_it_starts(tmp_path):
    h = await Harness.start(tmp_path, subsystems=(), bot_api=True)
    try:
        assert await alerts.alert_owner(h.rt, "early", "WhatsApp died before the bot was up") is False
        assert h.audit("owner_alert_queued", key="early")
        h.start_subsystem("control_bot")
        await h.wait_for_audit("control_bot_started")
        call = await h.bot_api.wait_for_call("sendMessage", chat_id=OWNER)
        assert call.data["text"] == "WhatsApp died before the bot was up"
        assert h.audit("owner_alert_sent", key="early")
    finally:
        await h.stop()


@run_async
async def test_alert_rate_limit_is_per_key_and_force_bypasses_it(tmp_path):
    async with await _start(tmp_path) as h:
        assert await alerts.alert_owner(h.rt, "k1", "first")
        assert await alerts.alert_owner(h.rt, "k1", "second") is False
        assert await alerts.alert_owner(h.rt, "k2", "other key")
        assert await alerts.alert_owner(h.rt, "k1", "forced", force=True)
        await h.bot_api.wait_for_call("sendMessage", chat_id=OWNER, count=3)
        assert _owner_texts(h) == ["first", "other key", "forced"]
        assert h.audit("owner_alert_suppressed", key="k1")


@run_async
async def test_a_failed_send_is_audited_not_swallowed(tmp_path):
    async with await _start(tmp_path) as h:
        h.bot_api.refuse_next("sendMessage", status=400, retry_after=0,
                              description="Bad Request: chat not found")
        assert await alerts.alert_owner(h.rt, "k", "hello") is False
        assert h.audit("owner_alert_failed", key="k")
        assert h.audit("throttled_send_failed")


@run_async
async def test_the_old_supervisor_would_have_stayed_silent(tmp_path):
    """Mutation check: the pre-fix loop (audit note + health + sleep) never
    reaches the owner however often the subsystem dies."""
    import traceback

    from archon import app as app_module

    async def old_supervise(rt, name, coro_factory, *, backoff_s=0.05):
        backoff = backoff_s
        while True:
            try:
                app_module._set_health(rt, name, "running")
                await coro_factory()
                return
            except Exception as exc:  # noqa: BLE001
                app_module._set_health(rt, name, f"crashed: {type(exc).__name__}")
                rt.audit.note("subsystem_crash", subsystem=name, error=repr(exc),
                              trace=traceback.format_exc()[-2000:])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async with await _start(tmp_path) as h:
        async def broken() -> None:
            raise RuntimeError("boom")

        h.tasks["old"] = asyncio.create_task(old_supervise(h.rt, "old", broken))
        await h.wait_for_audit("subsystem_crash", subsystem="old", count=3)
        assert _owner_texts(h) == []
