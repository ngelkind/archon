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
from .routers import (
    agent, approvals, chats, config, contacts, costs, devices, schedules, status,
    stream, tools,
)


def build_app(rt: Runtime) -> FastAPI:
    app = FastAPI(
        title="Archon Control API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.rt = rt
    # Public-exposure hardening; a no-op in single-user (tunnel) mode.
    from .ratelimit import install as install_rate_limit

    install_rate_limit(app, rt.settings)
    app.include_router(devices.router)  # /pair — code-authed, not bearer
    app.include_router(tools.router)
    app.include_router(agent.router)
    app.include_router(status.router)
    app.include_router(chats.router)
    app.include_router(config.router)
    app.include_router(contacts.router)
    app.include_router(schedules.router)
    app.include_router(costs.router)
    app.include_router(approvals.router)
    app.include_router(stream.router)
    # Multi-tenant product surface (/auth/*): mounted only when enabled, so the
    # live single-user deploy is untouched and argon2/jwt never load there. The
    # import is local for the same reason.
    if rt.settings.multitenant_enabled:
        from .routers import accounts as accounts_router
        app.include_router(accounts_router.router)
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
