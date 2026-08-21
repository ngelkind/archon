"""Health/status projection for the app (mirrors the control bot's /status)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...db import repo
from ...db.tenancy import TenantScope
from ..auth import require_tenant
from ..schemas import StatusResponse

router = APIRouter(dependencies=[Depends(require_tenant)], tags=["status"])


@router.get("/status", response_model=StatusResponse)
async def status_(request: Request,
                  tenant_id: int = Depends(require_tenant)) -> StatusResponse:
    rt = request.app.state.rt
    active = repo.setting_get(TenantScope(rt.db, tenant_id), "llm.active_provider",
                              rt.settings.llm_active_provider)
    return StatusResponse(
        uptime_s=rt.uptime_s(),
        health=dict(rt.health),
        active_provider=str(active),
    )
