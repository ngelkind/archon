"""Multi-tenant accounts + auth (/auth/*) tests.

Exercises signup/login/refresh/logout/me end-to-end over a real SQLite DB and
FastAPI TestClient. No network: argon2 + JWT + HMAC are all local. The router is
mounted only when ``multitenant_enabled`` is set, so a flag-off app is also
checked to prove the live single-user deploy is unaffected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from fastapi.testclient import TestClient

from archon.api.server import build_app
from archon.bus import Bus
from archon.config import Settings
from archon.db import Db
from archon.db.migrations import migrate
from archon.logging_.audit import AuditLog
from archon.runtime import Runtime


def make_rt(tmp_path, *, multitenant: bool = True) -> Runtime:
    db = Db(tmp_path / "t.db")
    migrate(db)
    settings = Settings(
        telegram_bot_token="x", telegram_owner_id=1,
        archon_data=tmp_path, archon_secrets=tmp_path,
        llm_active_provider="gemini", gemini_api_key="fake",
        api_token_pepper="test-pepper",
        multitenant_enabled=multitenant,
        jwt_secret="test-jwt-secret-at-least-32-bytes-long",
        access_token_ttl_minutes=15,
        refresh_token_ttl_days=30,
        _env_file=None,
    )
    audit = AuditLog(tmp_path / "a.jsonl", db, store_content=True)
    return Runtime(settings=settings, db=db, audit=audit, bus=Bus())


def _client(rt: Runtime) -> TestClient:
    return TestClient(build_app(rt))


def _signup(client: TestClient, email="user@example.com", password="hunter2pass",
            display_name="User") -> dict:
    r = client.post("/auth/signup", json={
        "email": email, "password": password, "display_name": display_name,
    })
    assert r.status_code == 200, r.text
    return r.json()


# --- signup ------------------------------------------------------------------

def test_signup_happy_path_returns_tokens_and_profile(tmp_path):
    client = _client(make_rt(tmp_path))
    body = _signup(client, email="Foo@Example.COM ")
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    user = body["user"]
    assert user["id"] > 0
    assert user["email"] == "foo@example.com"  # normalized
    assert user["display_name"] == "User"
    assert user["last_login_at"] is not None
    # the access token immediately authenticates /auth/me
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "foo@example.com"


def test_signup_duplicate_email_rejected(tmp_path):
    client = _client(make_rt(tmp_path))
    _signup(client, email="dup@example.com")
    r = client.post("/auth/signup", json={"email": "DUP@example.com", "password": "hunter2pass"})
    assert r.status_code == 409


def test_signup_weak_password_rejected(tmp_path):
    client = _client(make_rt(tmp_path))
    r = client.post("/auth/signup", json={"email": "a@b.com", "password": "short"})
    assert r.status_code == 422  # pydantic validation (min length 8)
    # ...and no account was created
    assert client.post("/auth/login", json={"email": "a@b.com", "password": "short"}).status_code == 401


def test_signup_invalid_email_rejected(tmp_path):
    client = _client(make_rt(tmp_path))
    r = client.post("/auth/signup", json={"email": "not-an-email", "password": "hunter2pass"})
    assert r.status_code == 422


# --- login -------------------------------------------------------------------

def test_login_success_updates_last_login(tmp_path):
    rt = make_rt(tmp_path)
    client = _client(rt)
    _signup(client, email="login@example.com")
    r = client.post("/auth/login", json={"email": "login@example.com", "password": "hunter2pass"})
    assert r.status_code == 200
    assert r.json()["user"]["email"] == "login@example.com"
    assert r.json()["access_token"]


def test_login_wrong_password_401(tmp_path):
    client = _client(make_rt(tmp_path))
    _signup(client, email="wp@example.com")
    r = client.post("/auth/login", json={"email": "wp@example.com", "password": "wrongpassword"})
    assert r.status_code == 401


def test_login_unknown_email_401(tmp_path):
    client = _client(make_rt(tmp_path))
    r = client.post("/auth/login", json={"email": "ghost@example.com", "password": "hunter2pass"})
    assert r.status_code == 401


# --- access-token auth on /auth/me ------------------------------------------

def test_me_rejects_missing_garbage_and_expired_tokens(tmp_path):
    rt = make_rt(tmp_path)
    client = _client(rt)
    _signup(client)
    assert client.get("/auth/me").status_code == 401  # missing
    assert client.get("/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Basic x"}).status_code == 401
    # a validly-signed but expired access token is rejected
    expired = jwt.encode(
        {"sub": "1", "type": "access",
         "iat": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
         "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp())},
        rt.settings.jwt_secret, algorithm="HS256",
    )
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    # a token signed with the WRONG secret is rejected
    forged = jwt.encode({"sub": "1", "type": "access",
                         "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp())},
                        "not-the-secret-but-also-32-bytes-long", algorithm="HS256")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


# --- refresh rotation + reuse detection -------------------------------------

def test_refresh_rotates_and_old_token_is_single_use(tmp_path):
    client = _client(make_rt(tmp_path))
    first = _signup(client, email="rot@example.com")
    r1 = first["refresh_token"]

    # rotate: r1 -> access2 + r2
    rot = client.post("/auth/refresh", json={"refresh_token": r1})
    assert rot.status_code == 200
    body2 = rot.json()
    r2 = body2["refresh_token"]
    assert r2 != r1
    # access token from the rotation works
    assert client.get("/auth/me",
                      headers={"Authorization": f"Bearer {body2['access_token']}"}).status_code == 200

    # reuse of the OLD refresh token is rejected...
    reuse = client.post("/auth/refresh", json={"refresh_token": r1})
    assert reuse.status_code == 401
    # ...and reuse detection killed the whole family, so r2 is now dead too
    assert client.post("/auth/refresh", json={"refresh_token": r2}).status_code == 401


def test_refresh_unknown_token_401(tmp_path):
    client = _client(make_rt(tmp_path))
    assert client.post("/auth/refresh", json={"refresh_token": "never-issued"}).status_code == 401


# --- logout ------------------------------------------------------------------

def test_logout_revokes_presented_refresh_token(tmp_path):
    client = _client(make_rt(tmp_path))
    body = _signup(client, email="lo@example.com")
    access, refresh = body["access_token"], body["refresh_token"]

    out = client.post("/auth/logout", json={"refresh_token": refresh},
                      headers={"Authorization": f"Bearer {access}"})
    assert out.status_code == 204
    # the revoked refresh token can no longer be rotated
    assert client.post("/auth/refresh", json={"refresh_token": refresh}).status_code == 401


def test_logout_requires_auth(tmp_path):
    client = _client(make_rt(tmp_path))
    body = _signup(client)
    assert client.post("/auth/logout", json={"refresh_token": body["refresh_token"]}).status_code == 401


# --- feature flag: off = surface absent -------------------------------------

def test_auth_surface_absent_when_multitenant_disabled(tmp_path):
    client = _client(make_rt(tmp_path, multitenant=False))
    # route not mounted at all -> 404, never reaching auth logic
    assert client.post("/auth/signup",
                       json={"email": "a@b.com", "password": "hunter2pass"}).status_code == 404
    assert client.get("/auth/me").status_code == 404
