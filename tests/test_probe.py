"""The live-probe framework, exercised offline.

The drivers touch real platforms (VM only), but the RUNNER, the WAITERS, the
safety gate and the dry-run path are all testable here — and they are what make
a probe trustworthy: a probe passes only on an OBSERVED effect written after the
act, and fails (not hangs, not lies) when the effect never comes.
"""

from __future__ import annotations

import json

from conftest import run_async
from test_m2 import make_rt

from archon.probe import waiters
from archon.probe.runner import (
    Probe,
    ProbeDisabled,
    ProbeError,
    run_probes,
    select,
)


def _enable(rt, *, dry_run=False):
    rt.settings.probe_enabled = True
    rt.settings.probe_dry_run = dry_run
    rt.settings.probe_telethon_session = "TEST-SESSION-distinct-from-owner"


# --- safety gate -------------------------------------------------------------

@run_async
async def test_gate_refuses_when_disabled(tmp_path):
    rt = make_rt(tmp_path)
    try:
        await run_probes(rt, "all", probes=[])
        raise AssertionError("should have refused")
    except ProbeDisabled as exc:
        assert "probe.enabled" in str(exc)


@run_async
async def test_gate_refuses_without_a_distinct_session(tmp_path):
    rt = make_rt(tmp_path)
    rt.settings.probe_enabled = True  # enabled, but no session set
    try:
        await run_probes(rt, "all", probes=[])
        raise AssertionError("should have refused")
    except ProbeDisabled as exc:
        assert "PROBE_TELETHON_SESSION" in str(exc)


@run_async
async def test_gate_refuses_probing_from_the_owners_own_session(tmp_path):
    rt = make_rt(tmp_path)
    _enable(rt)
    rt.settings.telethon_session = rt.settings.probe_telethon_session  # same account!
    try:
        await run_probes(rt, "all", probes=[])
        raise AssertionError("should have refused")
    except ProbeDisabled as exc:
        assert "owner" in str(exc).lower()


# --- runner + waiters --------------------------------------------------------

@run_async
async def test_probe_passes_only_on_an_observed_effect(tmp_path):
    rt = make_rt(tmp_path)
    _enable(rt)

    async def act(rt_, ctx):
        rt_.audit.note("probe_effect", token=ctx.scratch.setdefault("t", "abc"))

    async def expect(rt_, ctx):
        rec = await waiters.await_audit(rt_, ctx, "probe_effect", timeout_s=5,
                                        token="abc")
        return f"saw {rec['token']}"

    results = await run_probes(rt, "all", probes=[Probe("p", "tg", act, expect)],
                               force=True)
    assert results[0].ok and results[0].evidence == "saw abc"


@run_async
async def test_probe_fails_when_the_effect_never_comes(tmp_path):
    rt = make_rt(tmp_path)
    _enable(rt)

    async def act(rt_, ctx):
        pass  # do nothing — the effect never happens

    async def expect(rt_, ctx):
        await waiters.await_audit(rt_, ctx, "never_happens", timeout_s=1)
        return "unreachable"

    results = await run_probes(rt, "all", probes=[Probe("p", "tg", act, expect)],
                               force=True)
    assert not results[0].ok and "timed out" in results[0].detail


@run_async
async def test_waiter_ignores_effects_from_before_the_act(tmp_path):
    rt = make_rt(tmp_path)
    _enable(rt)
    # A stale note with the SAME action, written before the probe runs.
    rt.audit.note("stale", token="old")

    async def act(rt_, ctx):
        pass  # writes nothing new

    async def expect(rt_, ctx):
        await waiters.await_audit(rt_, ctx, "stale", timeout_s=1)  # must NOT match old
        return "unreachable"

    results = await run_probes(rt, "all", probes=[Probe("p", "tg", act, expect)],
                               force=True)
    assert not results[0].ok, "a pre-act line must not satisfy the waiter"


@run_async
async def test_await_db_row_finds_a_row_the_act_wrote(tmp_path):
    from archon.db import repo
    rt = make_rt(tmp_path)
    _enable(rt)

    async def act(rt_, ctx):
        repo.setting_set(rt_.db, "probe.marker", "landed")

    async def expect(rt_, ctx):
        row = await waiters.await_db_row(
            rt_, "SELECT value_json FROM settings WHERE key='probe.marker'", timeout_s=5)
        return row["value_json"]

    results = await run_probes(rt, "all", probes=[Probe("p", "tg", act, expect)],
                               force=True)
    assert results[0].ok and "landed" in results[0].evidence


@run_async
async def test_cleanup_runs_even_when_expect_fails(tmp_path):
    rt = make_rt(tmp_path)
    _enable(rt)
    cleaned = []

    async def act(rt_, ctx):
        pass

    async def expect(rt_, ctx):
        raise ProbeError("boom")

    async def cleanup(rt_, ctx):
        cleaned.append(True)

    results = await run_probes(
        rt, "all", probes=[Probe("p", "tg", act, expect, cleanup=cleanup)], force=True)
    assert not results[0].ok and cleaned == [True]


@run_async
async def test_result_json_is_written(tmp_path):
    rt = make_rt(tmp_path)
    _enable(rt)

    async def act(rt_, ctx):
        pass

    async def expect(rt_, ctx):
        return "ok"

    await run_probes(rt, "all", probes=[Probe("p", "tg", act, expect)], force=True)
    path = rt.settings.archon_data / "probe.result.json"
    data = json.loads(path.read_text())
    assert data["total"] == 1 and data["passed"] == 1
    assert data["results"][0]["name"] == "p"


def test_select_rejects_unknown_probe_names():
    probes = [Probe("a", "tg", None, None), Probe("b", "wa", None, None)]
    assert [p.name for p in select(probes, "all")] == ["a", "b"]
    assert [p.name for p in select(probes, "b")] == ["b"]
    try:
        select(probes, "nope")
        raise AssertionError("should reject unknown")
    except ProbeError as exc:
        assert "nope" in str(exc)


# --- the real drivers, on their dry-run path (no network) --------------------

@run_async
async def test_tg_driver_dry_run_needs_no_network(tmp_path):
    rt = make_rt(tmp_path)
    _enable(rt, dry_run=True)  # owner id is 1 (from make_rt), session set
    results = await run_probes(rt, "tg_text")  # real build_probes, dry-run act
    assert results[0].name == "tg_text"
    assert results[0].ok and "dry-run" in results[0].evidence
