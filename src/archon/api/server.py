"""ASGI app assembly + the supervised ``run(rt)`` coroutine.

``build_app`` stashes the Runtime on ``app.state`` and mounts the routers.
``run`` serves it with Uvicorn using ``loop="none"`` (we are already inside the
asyncio loop the supervisor created) and ``lifespan="off"`` (no startup/shutdown
hooks needed — the Runtime is fully built before we start). Interactive docs and
the OpenAPI schema are disabled: the surface is reachable only over the tunnel,
and there is no reason to expose it unauthenticated.
"""

from __future__ import annotations

from fastapi import FastAPI

from ..runtime import Runtime
from .routers import agent, devices, status, tools


def build_app(rt: Runtime) -> FastAPI:
    app = FastAPI(
        title="Archon Control API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.rt = rt
    app.include_router(devices.router)  # /pair — code-authed, not bearer
    app.include_router(tools.router)
    app.include_router(agent.router)
    app.include_router(status.router)
    return app


async def run(rt: Runtime) -> None:
    import uvicorn

    app = build_app(rt)
    config = uvicorn.Config(
        app,
        host=rt.settings.api_bind_host,
        port=rt.settings.api_port,
        loop="none",
        lifespan="off",
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()
