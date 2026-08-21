"""The confirm gate over the API.

Both endpoints ride the existing gate: the list is the ``pending_actions`` table
and the decision goes through ``confirm.resolve_action`` — the same single-use
path the Telegram keyboard uses, running the same five executors. Whichever
channel decides first wins; the other is told the action was already handled. No
new executor is written here.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Request

from ...db import repo
from ...db.tenancy import TenantScope
from ...pipeline import confirm
from ..auth import require_tenant
from ..schemas import Approval, ApprovalDecision, ApprovalDecisionResponse

router = APIRouter(dependencies=[Depends(require_tenant)], tags=["approvals"])


def _approval_dto(row: sqlite3.Row) -> Approval:
    try:
        payload = json.loads(row["payload_json"])
    except ValueError:
        payload = {}
    return Approval(
        id=int(row["id"]), kind=row["kind"], payload=payload,
        chat_pk=row["chat_pk"], status=row["status"],
        created_at=row["created_at"], expires_at=row["expires_at"],
    )


@router.get("/approvals", response_model=list[Approval])
async def list_approvals(request: Request, status: str = "pending",
                         tenant_id: int = Depends(require_tenant)) -> list[Approval]:
    rows = repo.pending_action_list(
        TenantScope(request.app.state.rt.db, tenant_id), status=status)
    return [_approval_dto(r) for r in rows]


@router.post("/approvals/{action_id}/decision", response_model=ApprovalDecisionResponse)
async def decide(
    action_id: int, body: ApprovalDecision, request: Request,
    tenant_id: int = Depends(require_tenant),
) -> ApprovalDecisionResponse:
    from ...tenant import tenant_context

    rt = request.app.state.rt
    # Scoped: one tenant can never resolve another tenant's pending action.
    outcome = await confirm.resolve_action(
        rt, action_id, "ok" if body.ok else "no",
        actor=f"api:tenant:{tenant_id}", tenant=tenant_context(rt, tenant_id),
    )
    return ApprovalDecisionResponse(
        status=outcome.status, detail=outcome.detail, ok=outcome.ok
    )
