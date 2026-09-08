"""Live probe control over the API: kick a run, read the last result.

Owner-scoped like /netstat. A run touches the real platforms, so POST is gated
by the same probe.enabled + distinct-session safety the runner enforces (it
raises ProbeDisabled before any network call)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ..auth import require_tenant

router = APIRouter(dependencies=[Depends(require_tenant)], tags=["probe"])


@router.post("/probe")
async def run_probe(request: Request, which: str = Query("all"),
                    _tenant_id: int = Depends(require_tenant)) -> dict[str, Any]:
    from ...probe.runner import ProbeDisabled, ProbeError, run_probes
    rt = request.app.state.rt
    try:
        results = await run_probes(rt, which)
    except ProbeDisabled as exc:
        return {"ok": False, "disabled": True, "error": str(exc)}
    except ProbeError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": all(r.ok for r in results),
            "passed": sum(1 for r in results if r.ok), "total": len(results),
            "results": [{"name": r.name, "kind": r.kind, "ok": r.ok,
                         "detail": r.detail, "evidence": r.evidence,
                         "elapsed_ms": r.elapsed_ms} for r in results]}


@router.get("/probe/last")
async def last_probe(request: Request,
                     _tenant_id: int = Depends(require_tenant)) -> dict[str, Any]:
    rt = request.app.state.rt
    path = rt.settings.archon_data / "probe.result.json"
    if not path.exists():
        return {"ok": False, "error": "no probe has run yet"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"ok": False, "error": "unreadable probe result"}
