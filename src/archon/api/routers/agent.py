"""Conversational "Mind" endpoint: talk to the owner agent, streamed as SSE.

Drives the transport-neutral ``run_owner_turn`` with an ``SseSink`` so the phone
and the Telegram bot share one rolling context. Emits ``tool_call`` /
``tool_result`` frames as the agent works and a terminal ``final`` / ``error``.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...agent.owner import run_owner_turn
from ...runtime import Runtime
from ..auth import require_tenant
from ..schemas import ChatRequest
from ..sink import SseSink

router = APIRouter(dependencies=[Depends(require_tenant)], tags=["agent"])


async def _drive(rt: Runtime, text: str, sink: SseSink, tenant) -> None:
    """Run the turn; guarantee a terminal event even on unexpected failure so the
    stream can never hang. ProviderError is already turned into on_error inside
    run_owner_turn; this catches anything else."""
    try:
        await run_owner_turn(rt, text, sink, tenant=tenant)
    except Exception as exc:  # noqa: BLE001 — must terminate the stream
        await sink.on_error(exc)


@router.post("/agent/chat")
async def agent_chat(body: ChatRequest, request: Request,
                     tenant_id: int = Depends(require_tenant)) -> StreamingResponse:
    from ...tenant import tenant_context

    rt = request.app.state.rt
    # The turn runs in the CALLING tenant's mind: their memory, their tools,
    # their data. Previously this always ran as the owner.
    tenant = tenant_context(rt, tenant_id)
    sink = SseSink()

    async def event_generator():
        turn = asyncio.create_task(_drive(rt, body.text, sink, tenant))
        try:
            async for frame in sink.stream():
                yield frame
        finally:
            await turn

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
