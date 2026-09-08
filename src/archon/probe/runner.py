"""The probe runner: setup → act → expect → cleanup, timing each, asserting on
observed effects, and refusing to run unless a distinct test session is set."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..runtime import Runtime


class ProbeError(RuntimeError):
    """A probe failed its assertion or its safety gate."""


class ProbeDisabled(ProbeError):
    pass


@dataclass(slots=True)
class ProbeCtx:
    """Per-probe scratch. ``after_line`` is the audit length captured BEFORE
    ``act`` runs, so a waiter only matches effects this probe caused — not a
    stale line from an earlier probe."""

    after_line: int
    started_at: float
    scratch: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProbeResult:
    name: str
    kind: str
    ok: bool
    detail: str
    evidence: str
    elapsed_ms: int


# A probe step: (rt, ctx) -> ... . `act` performs the outbound action from the
# test account; `expect` awaits the observed effect and returns evidence text.
Step = Callable[[Runtime, ProbeCtx], Awaitable[Any]]


@dataclass(slots=True)
class Probe:
    name: str
    kind: str  # "tg" | "wa" | "gmail"
    act: Step
    expect: Step
    timeout_s: float = 60.0
    setup: Step | None = None
    cleanup: Step | None = None


def _audit_len(rt: Runtime) -> int:
    path = rt.audit.path
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def assert_can_run(rt: Runtime, *, force: bool = False) -> None:
    """Safety gate. Raises unless probing is explicitly enabled and the test
    session is DISTINCT from the owner's — a probe must never drive the owner's
    own account against their contacts."""
    s = rt.settings
    if not (force or s.probe_enabled):
        raise ProbeDisabled("probe.enabled is off; set PROBE_ENABLED=1 to run live probes")
    session = (s.probe_telethon_session or "").strip()
    if not session:
        raise ProbeDisabled(
            "no PROBE_TELETHON_SESSION configured — a probe needs its own test "
            "account, never the owner's session")
    owner = (getattr(s, "telethon_session", "") or "").strip()
    if owner and owner == session:
        raise ProbeDisabled(
            "PROBE_TELETHON_SESSION equals the owner's session — refusing to "
            "probe from the owner's own account")


async def _run_one(rt: Runtime, probe: Probe) -> ProbeResult:
    ctx = ProbeCtx(after_line=_audit_len(rt), started_at=time.monotonic())
    rt.audit.note("probe_start", name=probe.name, kind=probe.kind)
    try:
        if probe.setup is not None:
            await asyncio.wait_for(probe.setup(rt, ctx), probe.timeout_s)
        await asyncio.wait_for(probe.act(rt, ctx), probe.timeout_s)
        evidence = await asyncio.wait_for(probe.expect(rt, ctx), probe.timeout_s)
        elapsed = int((time.monotonic() - ctx.started_at) * 1000)
        rt.audit.note("probe_pass", name=probe.name, evidence=str(evidence)[:200],
                      elapsed_ms=elapsed)
        return ProbeResult(probe.name, probe.kind, True, "", str(evidence)[:300], elapsed)
    except Exception as exc:  # noqa: BLE001 — a failed probe is a result, not a crash
        elapsed = int((time.monotonic() - ctx.started_at) * 1000)
        detail = ("timed out" if isinstance(exc, asyncio.TimeoutError)
                  else f"{type(exc).__name__}: {exc}")
        rt.audit.note("probe_fail", name=probe.name, error=detail[:300], elapsed_ms=elapsed)
        return ProbeResult(probe.name, probe.kind, False, detail[:300], "", elapsed)
    finally:
        if probe.cleanup is not None:
            try:
                await asyncio.wait_for(probe.cleanup(rt, ctx), probe.timeout_s)
            except Exception as exc:  # noqa: BLE001 — cleanup failure must not mask the result
                rt.audit.note("probe_cleanup_failed", name=probe.name, error=repr(exc)[:200])


def select(probes: list[Probe], which: str) -> list[Probe]:
    words = which.split()
    if not words or "all" in words:
        return list(probes)
    by_name = {p.name: p for p in probes}
    unknown = [w for w in words if w not in by_name]
    if unknown:
        raise ProbeError(f"unknown probe(s): {' '.join(unknown)}; "
                         f"valid: {' '.join(by_name)}")
    return [by_name[w] for w in words]


def render_table(results: list[ProbeResult]) -> str:
    passed = sum(1 for r in results if r.ok)
    lines = [f"Live probes {passed}/{len(results)} passed", ""]
    for r in results:
        mark = "✅" if r.ok else "❌"
        body = r.evidence if r.ok else r.detail
        lines.append(f"{mark} [{r.kind}] {r.name} ({r.elapsed_ms}ms): {body}")
    return "\n".join(lines)


async def run_probes(rt: Runtime, which: str = "all", *,
                     probes: list[Probe] | None = None,
                     force: bool = False) -> list[ProbeResult]:
    """Run the selected probes and write ``data/probe.result.json``. Raises
    :class:`ProbeDisabled` before touching the network if the safety gate fails."""
    assert_can_run(rt, force=force)
    if probes is None:
        from .drivers import build_probes
        probes = build_probes(rt)
    selected = select(probes, which)
    rt.audit.note("probe_run_start", which=which, count=len(selected))
    results: list[ProbeResult] = []
    for probe in selected:
        results.append(await _run_one(rt, probe))
    _write_result(rt, which, results)
    passed = sum(1 for r in results if r.ok)
    rt.audit.note("probe_run_done", passed=passed, total=len(results))
    return results


def _write_result(rt: Runtime, which: str, results: list[ProbeResult]) -> None:
    payload = {
        "which": which,
        "ts": time.time(),
        "passed": sum(1 for r in results if r.ok),
        "total": len(results),
        "results": [{"name": r.name, "kind": r.kind, "ok": r.ok,
                     "detail": r.detail, "evidence": r.evidence,
                     "elapsed_ms": r.elapsed_ms} for r in results],
    }
    try:
        path = rt.settings.archon_data / "probe.result.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        rt.audit.note("probe_result_write_failed", error=repr(exc)[:200])
