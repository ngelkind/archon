"""Generic tool endpoints: every registered owner-scoped tool, reachable.

``GET /tools`` mirrors ``registry.specs_for("owner")`` (any tool added later
appears automatically). ``POST /tools/{name}`` forwards straight to
``registry.dispatch`` with an API owner context, so audit / scope / confirm all
happen inside dispatch exactly as they do for Telegram — the API adds nothing
and bypasses nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...tools.registry import Registry
from ..auth import require_device
from ..ctx import api_owner_ctx
from ..schemas import ToolCallRequest, ToolCallResponse, ToolInfo, ToolListResponse

router = APIRouter(dependencies=[Depends(require_device)], tags=["tools"])


@router.get("/tools", response_model=ToolListResponse)
async def list_tools(request: Request) -> ToolListResponse:
    registry: Registry = request.app.state.rt.registry
    infos: list[ToolInfo] = []
    for spec in registry.specs_for("owner"):
        tool = registry.get(spec.name)
        infos.append(ToolInfo(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            sensitive=bool(tool.sensitive) if tool else False,
        ))
    return ToolListResponse(tools=infos)


@router.post("/tools/{name}", response_model=ToolCallResponse)
async def call_tool(name: str, body: ToolCallRequest, request: Request) -> ToolCallResponse:
    rt = request.app.state.rt
    result = await rt.registry.dispatch(api_owner_ctx(rt), name, body.args)
    return ToolCallResponse(result=result)
