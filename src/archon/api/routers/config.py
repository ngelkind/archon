"""Global settings: read the settings table, write via the settings_set tool."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request

from ...db import repo
from ...db.tenancy import TenantScope
from ..auth import require_tenant
from ..ctx import tenant_ctx
from ..schemas import ConfigPut, ConfigResponse, ToolCallResponse

router = APIRouter(dependencies=[Depends(require_tenant)], tags=["config"])


def _redact(key: str, value_json: str):
    # Same rule as the settings_get tool: never hand back provider API keys.
    if ".key." in key or key.startswith("llm.key"):
        return "•••"
    try:
        return json.loads(value_json)
    except ValueError:
        return value_json


@router.get("/config", response_model=ConfigResponse)
async def get_config(request: Request,
                     tenant_id: int = Depends(require_tenant)) -> ConfigResponse:
    rt = request.app.state.rt
    rows = repo.setting_all(TenantScope(rt.db, tenant_id))
    return ConfigResponse(settings={r["key"]: _redact(r["key"], r["value_json"]) for r in rows})


@router.put("/config/{key}", response_model=ToolCallResponse)
async def put_config(key: str, body: ConfigPut, request: Request,
                     tenant_id: int = Depends(require_tenant)) -> ToolCallResponse:
    rt = request.app.state.rt
    result = await rt.registry.dispatch(
        tenant_ctx(rt, tenant_id), "settings_set",
        {"key": key, "value_json": json.dumps(body.value, ensure_ascii=False)},
    )
    return ToolCallResponse(result=result)
