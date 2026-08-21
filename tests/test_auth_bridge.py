"""JWT → tenant auth bridge, exercised over the REAL HTTP path.

This file exists because of a launch blocker that every previous test missed: a
signed-in product user has only a JWT, but every feature router demanded a
device token from ``/pair`` — which is reachable only through the owner's
Telegram bot. So a JWT got 401 on everything.

The reason the old suite was green is the trap worth naming: it built contexts
directly (``tenant_ctx(rt, tenant_id)``) or authenticated with device tokens, so
nothing ever sent a JWT at a feature route. **Every test here goes through
TestClient with a real ``Authorization: Bearer <jwt>`` header**, because that is
the only thing that would have caught it.

There was a second, worse half: fixing auth alone would have turned the 401 into
a cross-tenant data leak, since the feature routers read ``rt.db`` and built
``api_owner_ctx(rt)`` — i.e. they served *the owner's* data to whoever got
through. ``test_jwt_user_never_sees_owner_data`` is the guard for that.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from archon.api.server import build_app
from archon.db import repo
from archon.db.tenancy import OWNER_TENANT_ID, TenantScope, owner_scope

from test_api import _make_device_token, make_rt

_SECRETS = dict(jwt_secret="j" * 64, api_token_pepper="p" * 64,
                credential_encryption_key="c" * 64)


def _rt(tmp_path, multitenant=True):
    rt = make_rt(tmp_path)
    rt.settings.multitenant_enabled = multitenant
    for k, v in _SECRETS.items():
        setattr(rt.settings, k, v)
    from archon.tools import settings_ as settings_tools

    settings_tools.register(rt.registry)
    return rt


def _signup(client, email="user@example.com", password="a long passphrase"):
    r = client.post("/auth/signup", json={"email": email, "password": password})
    assert r.status_code in (200, 201), r.text
    return r.json()["access_token"]


def _jwt_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _seed(rt, tenant_id, tag):
    """Give a tenant one of everything the feature routers read."""
    sc = TenantScope(rt.db, tenant_id)
    chat_pk = repo.chat_upsert(sc, "wa", "shared@g.us", f"Family-{tag}", "group")
    repo.setting_set(sc, "llm.active_provider", tag)
    repo.contact_add(sc, f"Contact-{tag}", f"+100{tenant_id}", "c", "import")
    repo.schedule_create(sc, platform="wa", chat_pk=chat_pk, text=f"sched-{tag}",
                         due_at="2099-01-01 00:00:00")
    repo.pending_action_create(sc, kind="wa.send", payload_json="{}",
                               chat_pk=chat_pk, expires_at="2099-01-01 00:00:00")
    repo.llm_call_record(sc, purpose="agent", provider="gemini", model="m",
                         cost_usd=1.0)
    return chat_pk


# --- the blocker itself ------------------------------------------------------

# Every feature route a signed-in product user must be able to reach.
FEATURE_ROUTES = [
    ("GET", "/tools"),
    ("GET", "/chats"),
    ("GET", "/config"),
    ("GET", "/contacts"),
    ("GET", "/schedules"),
    ("GET", "/costs"),
    ("GET", "/approvals"),
    ("GET", "/status"),
    ("GET", "/integrations"),
    ("GET", "/integrations/whatsapp"),
    ("GET", "/integrations/telegram"),
    ("GET", "/integrations/telegram/userbot"),
]


@pytest.mark.parametrize("method,path", FEATURE_ROUTES)
def test_a_signed_in_user_can_reach_every_feature_route(tmp_path, method, path):
    """The launch blocker: before the bridge, all of these returned 401
    'invalid or missing device token' for a perfectly valid JWT."""
    rt = _rt(tmp_path)
    client = TestClient(build_app(rt))
    token = _signup(client)

    response = client.request(method, path, headers=_jwt_headers(token))
    assert response.status_code == 200, f"{path} -> {response.status_code} {response.text}"


def test_google_authorize_returns_a_consent_url_for_a_jwt_user(tmp_path):
    """The exact call that failed live on the box."""
    rt = _rt(tmp_path)
    rt.settings.google_oauth_client_id = "cid.apps.googleusercontent.com"
    rt.settings.google_oauth_client_secret = "secret"
    rt.settings.google_oauth_redirect_uri = "https://x.example/integrations/google/callback"
    client = TestClient(build_app(rt))
    token = _signup(client)

    r = client.post("/integrations/google/authorize", headers=_jwt_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["authorize_url"].startswith("https://accounts.google.com/")


def test_bad_and_missing_credentials_still_401(tmp_path):
    rt = _rt(tmp_path)
    client = TestClient(build_app(rt))
    assert client.get("/chats").status_code == 401
    assert client.get("/chats", headers=_jwt_headers("not-a-jwt")).status_code == 401
    assert client.get("/chats", headers={"Authorization": "Basic x"}).status_code == 401


def test_an_expired_or_forged_jwt_is_refused(tmp_path):
    import jwt as pyjwt

    rt = _rt(tmp_path)
    client = TestClient(build_app(rt))
    _signup(client)

    forged = pyjwt.encode({"sub": "1", "type": "access", "exp": 4102444800},
                          "wrong-secret", algorithm="HS256")
    assert client.get("/chats", headers=_jwt_headers(forged)).status_code == 401

    expired = pyjwt.encode({"sub": "1", "type": "access", "exp": 1},
                           rt.settings.jwt_secret, algorithm="HS256")
    assert client.get("/chats", headers=_jwt_headers(expired)).status_code == 401


def test_a_disabled_account_loses_access_immediately(tmp_path):
    """A JWT stays cryptographically valid until it expires, so revocation has
    to be enforced on every request, not trusted to the token."""
    rt = _rt(tmp_path)
    client = TestClient(build_app(rt))
    token = _signup(client, email="gone@example.com")
    assert client.get("/chats", headers=_jwt_headers(token)).status_code == 200

    rt.db.execute("UPDATE users SET disabled_at = datetime('now') WHERE email = ?",
                  ("gone@example.com",))
    assert client.get("/chats", headers=_jwt_headers(token)).status_code == 401


# --- isolation: the second half of the bug -----------------------------------

def test_jwt_user_never_sees_owner_data(tmp_path):
    """Fixing auth alone would have converted the 401 into a data leak: the
    routers read rt.db and built api_owner_ctx(rt), i.e. the OWNER's data."""
    rt = _rt(tmp_path)
    _seed(rt, OWNER_TENANT_ID, "OWNER")
    client = TestClient(build_app(rt))
    token = _signup(client, email="stranger@example.com")
    h = _jwt_headers(token)

    assert client.get("/chats", headers=h).json() == []
    assert client.get("/contacts", headers=h).json()["contacts"] == []
    assert client.get("/schedules", headers=h).json() == []
    assert client.get("/approvals", headers=h).json() == []
    assert client.get("/config", headers=h).json()["settings"] == {}
    assert client.get("/costs", headers=h).json()["total"]["calls"] == 0
    # and the owner's rows are still there, untouched
    assert len(repo.chat_list(owner_scope(rt.db))) == 1


def test_two_users_never_see_each_others_data(tmp_path):
    rt = _rt(tmp_path)
    client = TestClient(build_app(rt))
    a_token = _signup(client, email="a@example.com")
    b_token = _signup(client, email="b@example.com")

    a_id = repo.user_by_email(rt.db, "a@example.com")["id"]
    b_id = repo.user_by_email(rt.db, "b@example.com")["id"]
    a_chat = _seed(rt, a_id, "A")
    _seed(rt, b_id, "B")

    a_chats = client.get("/chats", headers=_jwt_headers(a_token)).json()
    b_chats = client.get("/chats", headers=_jwt_headers(b_token)).json()
    assert [c["name"] for c in a_chats] == ["Family-A"]
    assert [c["name"] for c in b_chats] == ["Family-B"]

    # B cannot fetch A's chat by its primary key, even knowing it
    assert client.get(f"/chats/{a_chat}",
                      headers=_jwt_headers(b_token)).status_code == 404
    assert client.get(f"/chats/{a_chat}",
                      headers=_jwt_headers(a_token)).status_code == 200

    # settings are per tenant
    assert client.get("/config", headers=_jwt_headers(a_token)
                      ).json()["settings"]["llm.active_provider"] == "A"
    assert client.get("/config", headers=_jwt_headers(b_token)
                      ).json()["settings"]["llm.active_provider"] == "B"


def test_a_write_through_a_jwt_lands_in_that_users_tenant(tmp_path):
    """PATCH /chats dispatches a tool; it must act as the caller, not the owner."""
    rt = _rt(tmp_path)
    client = TestClient(build_app(rt))
    a_token = _signup(client, email="a@example.com")
    a_id = repo.user_by_email(rt.db, "a@example.com")["id"]
    a_chat = _seed(rt, a_id, "A")
    _seed(rt, OWNER_TENANT_ID, "OWNER")

    r = client.patch(f"/chats/{a_chat}", json={"is_whitelisted": True},
                     headers=_jwt_headers(a_token))
    assert r.status_code == 200 and r.json()["chat"]["is_whitelisted"] is True

    assert repo.chat_get(TenantScope(rt.db, a_id), "wa",
                         "shared@g.us")["is_whitelisted"] == 1
    # the owner's identically-keyed chat is untouched
    assert repo.chat_get(owner_scope(rt.db), "wa",
                         "shared@g.us")["is_whitelisted"] == 0


def test_a_tool_dispatched_by_a_jwt_user_acts_as_them(tmp_path):
    """POST /tools/{name} used to dispatch with api_owner_ctx — full owner
    authority for anyone who could authenticate."""
    rt = _rt(tmp_path)
    client = TestClient(build_app(rt))
    token = _signup(client, email="a@example.com")
    a_id = repo.user_by_email(rt.db, "a@example.com")["id"]
    _seed(rt, OWNER_TENANT_ID, "OWNER")

    r = client.post("/tools/chat_list", json={"args": {}}, headers=_jwt_headers(token))
    assert r.status_code == 200
    assert "Family-OWNER" not in r.json()["result"]

    r2 = client.post("/tools/settings_set",
                     json={"args": {"key": "tz", "value_json": '"Europe/London"'}},
                     headers=_jwt_headers(token))
    assert r2.status_code == 200
    assert repo.setting_get(TenantScope(rt.db, a_id), "tz") == "Europe/London"
    assert repo.setting_get(owner_scope(rt.db), "tz") is None


def test_isolation_would_catch_a_regression(tmp_path, monkeypatch):
    """Mutation guard: point the chats router back at the owner and the
    isolation assertion must fail, proving it is load-bearing."""
    import archon.api.routers.chats as chats_router

    rt = _rt(tmp_path)
    _seed(rt, OWNER_TENANT_ID, "OWNER")
    client = TestClient(build_app(rt))
    token = _signup(client, email="stranger@example.com")

    assert client.get("/chats", headers=_jwt_headers(token)).json() == []

    real_chat_list = repo.chat_list          # capture before patching

    def leaky(store, platform=None, whitelisted_only=False):
        return real_chat_list(owner_scope(rt.db), platform, whitelisted_only)

    monkeypatch.setattr(chats_router.repo, "chat_list", leaky)
    leaked = client.get("/chats", headers=_jwt_headers(token)).json()
    assert [c["name"] for c in leaked] == ["Family-OWNER"]   # regression visible


# --- the single-user device path must be byte-identical ----------------------

def test_device_token_still_works_in_single_user_mode(tmp_path):
    rt = _rt(tmp_path, multitenant=False)
    _seed(rt, OWNER_TENANT_ID, "OWNER")
    client = TestClient(build_app(rt))
    h = {"Authorization": f"Bearer {_make_device_token(rt)}"}

    assert [c["name"] for c in client.get("/chats", headers=h).json()] == ["Family-OWNER"]
    assert client.get("/status", headers=h).status_code == 200
    assert client.get("/tools", headers=h).status_code == 200
    assert client.get("/chats").status_code == 401


def test_jwt_is_not_accepted_in_single_user_mode(tmp_path):
    """With the flag off there are no accounts; only device tokens authenticate.
    /auth/* is not even mounted."""
    import jwt as pyjwt

    rt = _rt(tmp_path, multitenant=False)
    client = TestClient(build_app(rt))
    token = pyjwt.encode({"sub": "1", "type": "access", "exp": 4102444800},
                         rt.settings.jwt_secret, algorithm="HS256")
    assert client.get("/chats", headers=_jwt_headers(token)).status_code == 401
    assert client.post("/auth/signup",
                       json={"email": "x@y.com", "password": "a long passphrase"}
                       ).status_code == 404


def test_device_tokens_still_work_alongside_jwts_in_multitenant(tmp_path):
    """Legacy device clients must not break when the product flag goes on."""
    rt = _rt(tmp_path)
    _seed(rt, OWNER_TENANT_ID, "OWNER")
    client = TestClient(build_app(rt))

    device_h = {"Authorization": f"Bearer {_make_device_token(rt)}"}
    assert [c["name"] for c in client.get("/chats", headers=device_h).json()] \
        == ["Family-OWNER"]

    jwt_h = _jwt_headers(_signup(client, email="new@example.com"))
    assert client.get("/chats", headers=jwt_h).json() == []


# --- /stream ------------------------------------------------------------------

def test_stream_accepts_a_jwt(tmp_path):
    rt = _rt(tmp_path)
    client = TestClient(build_app(rt))
    token = _signup(client)

    with client.websocket_connect(f"/stream?token={token}") as ws:
        rt.events.publish("health.change", subsystem="api", state="running")
        assert ws.receive_json()["kind"] == "health.change"


def test_stream_still_rejects_rubbish(tmp_path):
    from starlette.websockets import WebSocketDisconnect

    rt = _rt(tmp_path)
    client = TestClient(build_app(rt))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/stream?token=nope"):
            pass
    assert exc.value.code == 1008


# --- the stale scope warning --------------------------------------------------

def test_the_stale_scope_warning_is_gone(tmp_path):
    """accounts.py carried 'keep it that way until tenancy lands'. Tenancy has
    landed and the routers are migrated; leaving that text would tell the next
    reader the opposite of the truth."""
    import pathlib

    src = pathlib.Path("src/archon/api/accounts.py").read_text(encoding="utf-8")
    assert "keep it that way until tenancy lands" not in src
    assert "Tenant-scope the data model FIRST" not in src
