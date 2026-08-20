"""Multi-tenant account auth primitives + the ``require_user`` dependency.

Two token kinds:

* **Access token** — a short-lived HS256 JWT (``jwt_secret``) carrying
  ``sub=user_id``. Stateless: verified by signature + ``exp``, never stored. The
  app sends it as ``Authorization: Bearer <jwt>`` on every request.
* **Refresh token** — a 256-bit opaque random string. Only its HMAC hash (server
  pepper, same scheme as device tokens in ``security.py``) is stored, so a stolen
  DB yields no usable tokens. Single-use: ``/auth/refresh`` revokes the presented
  one and issues a fresh pair, making a reused-after-rotation token detectable.

Passwords are hashed with argon2id (the PHC string embeds its salt + params).

This module is imported only when ``multitenant_enabled`` is set (the router is
mounted conditionally), so its argon2/jwt deps never load for the single-user
deploy.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Header, HTTPException, Request, status

from ..config import Settings
from ..runtime import Runtime
from .security import hash_secret

_JWT_ALG = "HS256"
# access-token claim marking the token kind; guards against a refresh/other JWT
# ever being accepted as an access token (defense in depth — refresh tokens are
# opaque, not JWTs, but the claim keeps the access path explicit).
_TOKEN_TYPE = "access"

_ph = PasswordHasher()

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid or missing access token",
    headers={"WWW-Authenticate": "Bearer"},
)


# --- passwords --------------------------------------------------------------

def hash_password(password: str) -> str:
    """argon2id PHC string (salt + params embedded); store verbatim."""
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time-ish verify; False on mismatch or a malformed stored hash."""
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


# --- access tokens (stateless JWT) ------------------------------------------

def mint_access_token(settings: Settings, user_id: int) -> tuple[str, int]:
    """Return ``(jwt, expires_in_seconds)`` for ``user_id``."""
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": _TOKEN_TYPE,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=_JWT_ALG)
    return token, int(ttl.total_seconds())


def verify_access_token(settings: Settings, token: str) -> int | None:
    """Return the user id from a valid, unexpired access JWT, else None."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[_JWT_ALG])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != _TOKEN_TYPE:
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.isdigit():
        return None
    return int(sub)


# --- refresh tokens (opaque; only the hash is stored) -----------------------

def mint_refresh_token() -> str:
    """A fresh 256-bit URL-safe opaque refresh token (plaintext, shown once)."""
    return secrets.token_urlsafe(32)


def refresh_token_hash(settings: Settings, token: str) -> str:
    return hash_secret(settings.api_token_pepper, token)


def refresh_token_expiry(settings: Settings) -> str:
    exp = datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days)
    return exp.strftime("%Y-%m-%d %H:%M:%S")


def refresh_token_is_live(row) -> bool:
    """True iff a stored refresh-token row is neither revoked nor expired."""
    if row is None or row["revoked_at"] is not None:
        return False
    expires_at = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return expires_at >= datetime.now(UTC)


# --- FastAPI dependency ------------------------------------------------------

def _rt(request: Request) -> Runtime:
    return request.app.state.rt


async def require_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> int:
    """Validate the bearer access token; return the user id. 401 on
    missing / malformed / invalid / expired, or if the account no longer
    exists or has been disabled."""
    rt = _rt(request)
    if not authorization or not authorization.startswith("Bearer "):
        raise _UNAUTHORIZED
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise _UNAUTHORIZED
    user_id = verify_access_token(rt.settings, token)
    if user_id is None:
        raise _UNAUTHORIZED
    from ..db import repo
    if repo.user_by_id(rt.db, user_id) is None:
        raise _UNAUTHORIZED
    return user_id
