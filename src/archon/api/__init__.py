"""In-process control API for the Android app.

A single FastAPI/Uvicorn ASGI app served as one more supervised asyncio task
(see ``app.main``), riding the same process, the same lock-guarded ``Db``, and
the same ``Runtime`` as every other subsystem. It adds an API *seam* only: all
actions still go through ``Registry.dispatch`` and ``run_owner_turn``, so audit,
scope, and the confirm gate are preserved for free. Bound to loopback / the
WireGuard interface — never ``0.0.0.0``. Only imported when ``api_enabled``.
"""
