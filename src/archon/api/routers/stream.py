"""Realtime event stream (WebSocket).

Subscribes a drop-oldest queue to ``rt.events`` for the life of the connection
and forwards JSON events. The hub is deliberately NOT the pipeline bus, so a
slow or vanished phone can never backpressure ingest or the LLM path.

Auth cannot use the ``require_device`` dependency: a rejected WebSocket must be
closed with a policy code, not answered with a 401 body. The check itself is the
same — bearer token, hashed, non-revoked — read from the ``Authorization``
header, falling back to a ``token`` query parameter for clients that cannot set
headers on the upgrade request.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, WebSocket

from ...db.tenancy import OWNER_TENANT_ID
from ..auth import tenant_for_token

router = APIRouter(tags=["stream"])

_POLICY_VIOLATION = 1008


def _authenticate(websocket: WebSocket, token: str | None) -> int | None:
    """The tenant behind this socket, or None.

    Uses the same resolver as the HTTP routes, so a product user's JWT works
    here too — and /stream can never drift from the rest of the API.
    """
    rt = websocket.app.state.rt
    header = websocket.headers.get("authorization", "")
    raw = header[len("Bearer "):].strip() if header.startswith("Bearer ") else (token or "")
    if not raw:
        return None
    return tenant_for_token(rt, raw)


def _visible_to(event: dict, tenant_id: int) -> bool:
    """A subscriber sees an event only if it belongs to their tenant. Untagged
    events (system-wide: health, cost, net) are the owner's alone — never
    fanned out to a product tenant's socket."""
    et = (event.get("data") or {}).get("tenant_id")
    if et is None:
        return tenant_id == OWNER_TENANT_ID
    return et == tenant_id


async def _forward(websocket: WebSocket, queue: asyncio.Queue, tenant_id: int) -> None:
    while True:
        event = await queue.get()
        if _visible_to(event, tenant_id):
            await websocket.send_json(event)


async def _until_disconnect(websocket: WebSocket) -> None:
    """The app sends nothing; reading is only how we notice it went away."""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return


@router.websocket("/stream")
async def stream(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    rt = websocket.app.state.rt
    tenant_id = _authenticate(websocket, token)
    if tenant_id is None:
        await websocket.close(code=_POLICY_VIOLATION)
        return
    await websocket.accept()

    with rt.events.subscription() as queue:
        tasks = {
            asyncio.create_task(_forward(websocket, queue, tenant_id)),
            asyncio.create_task(_until_disconnect(websocket)),
        }
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.exception()  # retrieve, so a send failure isn't "never retrieved"
        finally:
            for task in tasks:
                task.cancel()
