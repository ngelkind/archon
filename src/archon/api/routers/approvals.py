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
from ...pipeline import confirm
from ..auth import require_device
from ..schemas import Approval, ApprovalDecision, ApprovalDecisionResponse

router = APIRouter(dependencies=[Depends(require_device)], tags=["approvals"])


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
async def list_approvals(request: Request, status: str = "pending") -> list[Approval]:
    rows = repo.pending_action_list(request.app.state.rt.db, status=status)
    return [_approval_dto(r) for r in rows]


@router.post("/approvals/{action_id}/decision", response_model=ApprovalDecisionResponse)
async def decide(
    action_id: int, body: ApprovalDecision, request: Request,
    device: sqlite3.Row = Depends(require_device),
) -> ApprovalDecisionResponse:
    rt = request.app.state.rt
    outcome = await confirm.resolve_action(
        rt, action_id, "ok" if body.ok else "no", actor=f"api:device:{device['id']}"
    )
    return ApprovalDecisionResponse(
        status=outcome.status, detail=outcome.detail, ok=outcome.ok
    )
