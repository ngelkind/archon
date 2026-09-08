"""Network ledger projection: recent outbound calls and a rolling summary.

Mirrors the control bot's /netstat for the app/API. Gated behind an
authenticated tenant like /status; the ledger is process-wide observability
(hosts and paths, never bodies or query strings)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ..auth import require_tenant

router = APIRouter(dependencies=[Depends(require_tenant)], tags=["net"])


@router.get("/net/summary")
async def net_summary(request: Request,
                      _tenant_id: int = Depends(require_tenant)) -> dict[str, Any]:
    net = getattr(request.app.state.rt, "net", None)
    if net is None:
        return {"enabled": False, "total": 0, "by_subsystem": {}, "hosts": [],
                "unexpected_hosts": []}
    return {"enabled": True, **net.summary()}


@router.get("/net/recent")
async def net_recent(request: Request,
                     limit: int = Query(50, ge=1, le=500),
                     subsystem: str | None = Query(None),
                     _tenant_id: int = Depends(require_tenant)) -> dict[str, Any]:
    net = getattr(request.app.state.rt, "net", None)
    if net is None:
        return {"enabled": False, "calls": []}
    calls = net.recent(limit=limit, subsystem=subsystem)
    return {"enabled": True, "calls": [
        {"ts": c.ts, "subsystem": c.subsystem, "method": c.method,
         "host": c.host, "path": c.path, "status": c.status,
         "req_bytes": c.req_bytes, "resp_bytes": c.resp_bytes,
         "duration_ms": c.duration_ms, "error": c.error, "purpose": c.purpose}
        for c in calls]}
