"""Bearer-token auth for the feature routers.

Two credential types reach this API and both must resolve to the same thing —
**which tenant is calling**:

* **Device token** (`/pair`, bootstrapped from the owner's Telegram control
  bot). The single-user personal deployment's only credential.
* **Access JWT** (`/auth/login`). The product's only credential — a signed-up
  user never obtains a device token, because `/pair` is reachable only through
  the owner's Telegram bot.

``require_tenant`` accepts either and returns the tenant id. That is what makes
a signed-in product user able to reach a feature route at all: before it, every
feature router demanded a device token, so a JWT got a flat 401 everywhere.

Why one dependency rather than two: the tenant is the only thing routers
actually need, and having a single place that answers "who is calling?" means a
new router cannot accidentally be wired to the owner. ``tenant_id IS users.id``
(`db/008_tenancy.sql`), so a JWT's subject *is* its tenant — no mapping table,
no lookup, nothing to get out of sync.

``require_device`` is kept for the pairing/device-management paths that
genuinely need the device row itself.
"""

from __future__ import annotations

import sqlite3

from fastapi import Header, HTTPException, Request, status

from ..db import repo
from ..db.tenancy import OWNER_TENANT_ID, TenantScope
from ..runtime import Runtime
from .security import hash_secret

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid or missing credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def _rt(request: Request) -> Runtime:
    return request.app.state.rt


def bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[len("Bearer "):].strip() or None


def _device_for_token(rt: Runtime, token: str) -> sqlite3.Row | None:
    return repo.api_device_by_token_hash(
        rt.db, hash_secret(rt.settings.api_token_pepper, token)
    )


def tenant_for_token(rt: Runtime, token: str) -> int | None:
    """Resolve a bearer token to its tenant, or None.

    Tries the JWT first and only in multitenant mode: a device token is a
    256-bit random string that cannot parse as a JWT, so the order is
    unambiguous and neither type can be mistaken for the other.

    Shared by the HTTP dependency and the WebSocket handler so ``/stream``
    cannot drift from the rest of the API.
    """
    if rt.settings.multitenant_enabled:
        from .accounts import verify_access_token

        user_id = verify_access_token(rt.settings, token)
        if user_id is not None:
            # Re-check the account still exists and is not disabled: a JWT stays
            # cryptographically valid until it expires, so revocation has to be
            # enforced here rather than trusted to the token.
            if repo.user_by_id(rt.db, user_id) is None:
                return None
            return user_id            # tenant_id IS users.id

    device = _device_for_token(rt, token)
    if device is None:
        return None
    tenant_id = int(device["tenant_id"])
    repo.api_device_touch(TenantScope(rt.db, tenant_id), int(device["id"]))
    return tenant_id


async def require_tenant(
    request: Request,
    authorization: str | None = Header(default=None),
) -> int:
    """The tenant this request acts for. 401 if the credential is not valid.

    The tenant is derived from the *credential*, never from request data — a
    caller cannot ask to be another tenant.
    """
    rt = _rt(request)
    token = bearer_token(authorization)
    if token is None:
        raise _UNAUTHORIZED
    tenant_id = tenant_for_token(rt, token)
    if tenant_id is None:
        raise _UNAUTHORIZED
    request.state.tenant_id = tenant_id
    return tenant_id


async def require_device(
    request: Request,
    authorization: str | None = Header(default=None),
) -> sqlite3.Row:
    """The device row itself — for paths that manage devices.

    Feature routers should use :func:`require_tenant`; a JWT user has no device
    row, so requiring one here would lock them out.
    """
    rt = _rt(request)
    token = bearer_token(authorization)
    if token is None:
        raise _UNAUTHORIZED
    device = _device_for_token(rt, token)
    if device is None:
        raise _UNAUTHORIZED
    tenant_id = int(device["tenant_id"])
    request.state.tenant_id = tenant_id
    repo.api_device_touch(TenantScope(rt.db, tenant_id), int(device["id"]))
    return device


def owner_only(tenant_id: int) -> None:
    """Guard for endpoints that are meaningful only for the personal owner."""
    if tenant_id != OWNER_TENANT_ID:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this endpoint is only available to the owner deployment",
        )
