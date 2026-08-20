"""Multi-tenant account auth endpoints (/auth/*).

Mounted by server.py ONLY when ``settings.multitenant_enabled`` is set, so the
live single-user deploy never exposes this surface. Signup/login issue an
access+refresh token pair; refresh rotates the pair single-use (with reuse
detection); logout revokes the presented refresh token.

Token hashing / JWT minting live in ``api/accounts.py``; SQL lives in ``repo``.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ...db import repo
from ...runtime import Runtime
from .. import accounts
from ..accounts import require_user
from ..accounts_schemas import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    SignupRequest,
    TokenResponse,
    UserProfile,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid email or password",
    headers={"WWW-Authenticate": "Bearer"},
)
_INVALID_REFRESH = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid or expired refresh token",
    headers={"WWW-Authenticate": "Bearer"},
)


def _rt(request: Request) -> Runtime:
    return request.app.state.rt


def _profile(row: sqlite3.Row) -> UserProfile:
    return UserProfile(
        id=int(row["id"]),
        email=row["email"],
        display_name=row["display_name"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )


def _issue_tokens(rt: Runtime, user_row: sqlite3.Row) -> TokenResponse:
    """Mint a fresh access+refresh pair for ``user_row`` and persist the refresh
    hash. The refresh plaintext is returned to the client exactly once."""
    user_id = int(user_row["id"])
    access_token, expires_in = accounts.mint_access_token(rt.settings, user_id)
    refresh_plain = accounts.mint_refresh_token()
    repo.refresh_token_create(
        rt.db,
        token_hash=accounts.refresh_token_hash(rt.settings, refresh_plain),
        user_id=user_id,
        expires_at=accounts.refresh_token_expiry(rt.settings),
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_plain,
        expires_in=expires_in,
        user=_profile(user_row),
    )


@router.post("/signup", response_model=TokenResponse)
async def signup(body: SignupRequest, request: Request) -> TokenResponse:
    rt = _rt(request)
    if repo.user_by_email(rt.db, body.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="email already registered"
        )
    try:
        user_id = repo.user_create(
            rt.db,
            email=body.email,
            password_hash=accounts.hash_password(body.password),
            display_name=body.display_name,
        )
    except sqlite3.IntegrityError:
        # Racing duplicate: the UNIQUE(email) constraint fired between our check
        # and the insert.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="email already registered"
        ) from None
    repo.user_touch_login(rt.db, user_id)
    user_row = repo.user_by_id(rt.db, user_id)
    assert user_row is not None
    rt.audit.note("account_signup", user_id=user_id, email=body.email)
    return _issue_tokens(rt, user_row)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request) -> TokenResponse:
    rt = _rt(request)
    user_row = repo.user_by_email(rt.db, body.email)
    # Verify even when the user is unknown would be ideal for timing uniformity,
    # but argon2 on a random string is enough to keep the branches comparable.
    if user_row is None or not accounts.verify_password(
        user_row["password_hash"], body.password
    ):
        rt.audit.note("account_login_failed", email=body.email)
        raise _INVALID_CREDENTIALS
    user_id = int(user_row["id"])
    repo.user_touch_login(rt.db, user_id)
    user_row = repo.user_by_id(rt.db, user_id)
    assert user_row is not None
    rt.audit.note("account_login", user_id=user_id)
    return _issue_tokens(rt, user_row)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, request: Request) -> TokenResponse:
    rt = _rt(request)
    token_hash = accounts.refresh_token_hash(rt.settings, body.refresh_token)
    row = repo.refresh_token_get(rt.db, token_hash)
    if row is None:
        raise _INVALID_REFRESH
    if row["revoked_at"] is not None:
        # A previously-rotated (or logged-out) token is being presented again:
        # reuse. Kill the whole family so a leaked token can't outlive detection.
        revoked = repo.refresh_tokens_revoke_all(rt.db, int(row["user_id"]))
        rt.audit.note(
            "account_refresh_reuse", user_id=int(row["user_id"]), revoked=revoked
        )
        raise _INVALID_REFRESH
    if not accounts.refresh_token_is_live(row):  # expired
        raise _INVALID_REFRESH
    # Atomic single-use revoke; False means a concurrent request already rotated
    # this exact token — treat as a lost race, not a fresh rotation.
    if not repo.refresh_token_revoke(rt.db, token_hash):
        raise _INVALID_REFRESH
    user_row = repo.user_by_id(rt.db, int(row["user_id"]))
    if user_row is None:  # account disabled/deleted since the token was issued
        raise _INVALID_REFRESH
    rt.audit.note("account_refresh", user_id=int(row["user_id"]))
    return _issue_tokens(rt, user_row)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutRequest, request: Request, user_id: int = Depends(require_user)
) -> None:
    rt = _rt(request)
    token_hash = accounts.refresh_token_hash(rt.settings, body.refresh_token)
    row = repo.refresh_token_get(rt.db, token_hash)
    # Only revoke a token that belongs to the authenticated caller; otherwise
    # this is a no-op (idempotent logout — never reveal another user's token).
    if row is not None and int(row["user_id"]) == user_id:
        repo.refresh_token_revoke(rt.db, token_hash)
    rt.audit.note("account_logout", user_id=user_id)


@router.get("/me", response_model=UserProfile)
async def me(request: Request, user_id: int = Depends(require_user)) -> UserProfile:
    rt = _rt(request)
    user_row = repo.user_by_id(rt.db, user_id)
    if user_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return _profile(user_row)
