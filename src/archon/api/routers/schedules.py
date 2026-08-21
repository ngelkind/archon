"""Scheduled messages: read the queue, cancel via the schedule_cancel tool."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ...db import repo
from ...db.tenancy import TenantScope
from ..auth import require_tenant
from ..ctx import tenant_ctx
from ..schemas import Schedule, ToolCallResponse

router = APIRouter(dependencies=[Depends(require_tenant)], tags=["schedules"])


@router.get("/schedules", response_model=list[Schedule])
async def list_schedules(
    request: Request, limit: int = Query(default=50, ge=1, le=200),
    tenant_id: int = Depends(require_tenant),
) -> list[Schedule]:
    return [
        Schedule(
            id=int(r["id"]), platform=r["platform"], chat_pk=int(r["chat_pk"]),
            chat_id=r["chat_id"], chat_name=r["name"], text=r["text"],
            due_at=r["due_at"], status=r["status"],
        )
        for r in repo.schedule_list(
            TenantScope(request.app.state.rt.db, tenant_id), limit=limit)
    ]


@router.delete("/schedules/{schedule_id}", response_model=ToolCallResponse)
async def cancel_schedule(schedule_id: int, request: Request,
                          tenant_id: int = Depends(require_tenant)) -> ToolCallResponse:
    rt = request.app.state.rt
    result = await rt.registry.dispatch(
        tenant_ctx(rt, tenant_id), "schedule_cancel", {"schedule_id": schedule_id}
    )
    return ToolCallResponse(result=result)
