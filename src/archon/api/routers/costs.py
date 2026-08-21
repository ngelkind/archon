"""LLM spend: totals + per provider/model/purpose breakdown."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ...db import repo
from ...db.tenancy import TenantScope
from ..auth import require_tenant
from ..schemas import CostBreakdownRow, CostsResponse, CostWindow

router = APIRouter(dependencies=[Depends(require_tenant)], tags=["costs"])

_WINDOWS = {"day": "-1 day", "week": "-7 days", "month": "-30 days"}


@router.get("/costs", response_model=CostsResponse)
async def costs(
    request: Request, window: str = Query(default="day", pattern="^(day|week|month)$"),
    tenant_id: int = Depends(require_tenant),
) -> CostsResponse:
    db = TenantScope(request.app.state.rt.db, tenant_id)
    expr = _WINDOWS[window]
    total = repo.llm_cost_since(db, expr)
    return CostsResponse(
        window=window,
        total=CostWindow(
            calls=int(total["calls"]) if total else 0,
            cost_usd=float(total["cost"]) if total else 0.0,
            in_tokens=int(total["in_tok"]) if total else 0,
            out_tokens=int(total["out_tok"]) if total else 0,
        ),
        breakdown=[
            CostBreakdownRow(
                provider=r["provider"], model=r["model"], purpose=r["purpose"],
                calls=int(r["calls"]), cost_usd=float(r["cost"]),
            )
            for r in repo.llm_cost_breakdown(db, expr)
        ],
    )
