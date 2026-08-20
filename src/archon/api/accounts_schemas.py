"""Pydantic DTOs for the multi-tenant account/auth surface (/auth/*).

Kept separate from ``schemas.py`` (the single-user control API) so the product
auth layer stays self-contained. Email + password-length validation lives here,
dependency-free: a minimal email shape check (no ``email-validator`` dep) and a
length floor on passwords.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

# Deliberately permissive: "something@something.tld", trimmed + lowercased. Full
# RFC-5322 validation is not worth a dependency here; deliverability is proven by
# a later verification email, not by a regex.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_LEN = 200  # argon2 hashes any length; cap guards against DoS


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("invalid email address")
    return email


class SignupRequest(BaseModel):
    email: str
    password: str
    display_name: str | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _normalize_email(v)

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        if not (_MIN_PASSWORD_LEN <= len(v) <= _MAX_PASSWORD_LEN):
            raise ValueError(
                f"password must be {_MIN_PASSWORD_LEN}-{_MAX_PASSWORD_LEN} characters"
            )
        return v

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        # Login normalizes but does not enforce shape — a malformed email simply
        # won't match any account and yields the same 401 as a wrong password.
        return v.strip().lower()


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class UserProfile(BaseModel):
    id: int
    email: str
    display_name: str | None
    created_at: str
    last_login_at: str | None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str  # returned ONCE; only its hash is stored server-side
    token_type: str = "bearer"
    expires_in: int  # access-token lifetime in seconds
    user: UserProfile
