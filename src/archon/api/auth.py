"""Bearer-token auth: the ``require_device`` FastAPI dependency.

Reads ``Authorization: Bearer <token>``, hashes it with the server pepper, looks
up a live (non-revoked) device, refreshes ``last_seen_at``, and returns the
device row. Missing / malformed / unknown / revoked → 401. Used as a
router-level dependency on every authenticated router.
"""

from __future__ import annotations

import sqlite3

from fastapi import Header, HTTPException, Request, status

from ..db import repo
from ..runtime import Runtime
from .security import hash_secret

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid or missing device token",
    headers={"WWW-Authenticate": "Bearer"},
)


def _rt(request: Request) -> Runtime:
    return request.app.state.rt


async def require_device(
    request: Request,
    authorization: str | None = Header(default=None),
) -> sqlite3.Row:
    rt = _rt(request)
    if not authorization or not authorization.startswith("Bearer "):
        raise _UNAUTHORIZED
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise _UNAUTHORIZED
    token_hash = hash_secret(rt.settings.api_token_pepper, token)
    device = repo.api_device_by_token_hash(rt.db, token_hash)
    if device is None:
        raise _UNAUTHORIZED
    repo.api_device_touch(rt.db, int(device["id"]))
    return device
