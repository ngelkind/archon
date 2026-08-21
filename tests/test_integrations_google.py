"""Per-tenant Google linking: OAuth flow, credential encryption at rest, and
per-tenant client resolution.

No network: the token exchange is injected, and the Google client classes are
only constructed (never called), so nothing reaches accounts.google.com.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from archon.crypto import CredentialCryptoError, decrypt, encrypt
from archon.db import repo
from archon.db.tenancy import OWNER_TENANT_ID, TenantScope, owner_scope
from archon.integrations import google as gi
from archon.platforms.google_auth import SCOPES, GoogleAuth, TenantCredentialStore

from test_api import make_rt
from test_tenancy import _new_tenant

_KEY = "a" * 64  # 32 bytes of hex


def _rt(tmp_path):
    rt = make_rt(tmp_path)
    rt.settings.credential_encryption_key = _KEY
    rt.settings.google_oauth_client_id = "client-id.apps.googleusercontent.com"
    rt.settings.google_oauth_client_secret = "client-secret"
    rt.settings.google_oauth_redirect_uri = "https://archon.example/integrations/google/callback"
    return rt


def _fake_exchange(refresh="refresh-token", email="user@example.com",
                   scope=None, access="access-token"):
    def exchange(rt, code):
        assert code, "the authorization code must be passed through"
        payload = {"access_token": access, "scope": " ".join(scope or SCOPES),
                   "email": email}
        if refresh is not None:
            payload["refresh_token"] = refresh
        return payload

    return exchange


# --- credential encryption ---------------------------------------------------

def test_credentials_round_trip_and_are_not_plaintext():
    secret = "1//super-secret-refresh-token"
    envelope = encrypt(_KEY, secret, tenant_id=7, purpose="google")
    assert secret not in envelope                      # never stored readable
    assert json.loads(envelope)["alg"] == "AESGCM-256"
    assert decrypt(_KEY, envelope, tenant_id=7, purpose="google") == secret


def test_a_credential_cannot_be_moved_to_another_tenant():
    """The AAD binds the ciphertext to its tenant: copying the row into another
    tenant yields an undecryptable blob, not a working credential."""
    envelope = encrypt(_KEY, "token", tenant_id=7, purpose="google")
    with pytest.raises(CredentialCryptoError):
        decrypt(_KEY, envelope, tenant_id=8, purpose="google")
    with pytest.raises(CredentialCryptoError):
        decrypt(_KEY, envelope, tenant_id=7, purpose="telegram")
    with pytest.raises(CredentialCryptoError):
        decrypt("b" * 64, envelope, tenant_id=7, purpose="google")


def test_tampering_is_detected():
    envelope = json.loads(encrypt(_KEY, "token", tenant_id=1, purpose="google"))
    envelope["ct"] = envelope["ct"][:-4] + "AAAA"
    with pytest.raises(CredentialCryptoError):
        decrypt(_KEY, json.dumps(envelope), tenant_id=1, purpose="google")


def test_each_encryption_uses_a_fresh_data_key():
    a = json.loads(encrypt(_KEY, "same", tenant_id=1, purpose="google"))
    b = json.loads(encrypt(_KEY, "same", tenant_id=1, purpose="google"))
    assert a["ct"] != b["ct"] and a["n"] != b["n"] and a["dek"] != b["dek"]


def test_missing_key_refuses_rather_than_storing_plaintext():
    with pytest.raises(CredentialCryptoError):
        encrypt("", "token", tenant_id=1, purpose="google")


# --- the OAuth flow ----------------------------------------------------------

def test_authorize_url_is_tenant_bound_and_requests_offline_access(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    url, state = gi.authorize_url(rt, b_id)

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in url and "prompt=consent" in url
    assert f"state={state}" in url
    for scope in SCOPES:
        assert scope.replace(":", "%3A").replace("/", "%2F") in url or scope in url
    # the state is recorded against THIS tenant, server-side
    row = rt.db.query_one("SELECT * FROM oauth_states WHERE state = ?", (state,))
    assert row["tenant_id"] == b_id and row["used_at"] is None


def test_callback_stores_an_encrypted_token_for_the_right_tenant(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _, state = gi.authorize_url(rt, b_id)

    result = gi.complete_link(rt, state=state, code="auth-code",
                              exchange=_fake_exchange(refresh="B-refresh"))
    assert result["tenant_id"] == b_id
    assert result["account"] == "user@example.com"

    row = repo.integration_cred_get(TenantScope(rt.db, b_id), "google")
    assert row is not None
    assert "B-refresh" not in row["secret_envelope"]        # encrypted at rest
    loaded = TenantCredentialStore(rt, b_id).load()
    assert loaded["refresh_token"] == "B-refresh"

    # the owner has no credential of their own
    assert repo.integration_cred_get(owner_scope(rt.db), "google") is None


def test_state_is_single_use_and_tenant_bound(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _, state = gi.authorize_url(rt, b_id)

    gi.complete_link(rt, state=state, code="c", exchange=_fake_exchange())
    # replay is refused: this is what stops a stolen/pasted code being re-used
    with pytest.raises(gi.GoogleLinkError, match="already-used"):
        gi.complete_link(rt, state=state, code="c", exchange=_fake_exchange())
    with pytest.raises(gi.GoogleLinkError, match="OAuth state"):
        gi.complete_link(rt, state="never-issued", code="c",
                         exchange=_fake_exchange())


def test_expired_state_is_refused(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    repo.oauth_state_create(rt.db, state="stale", tenant_id=b_id,
                            provider="google", expires_at="2000-01-01 00:00:00")
    with pytest.raises(gi.GoogleLinkError):
        gi.complete_link(rt, state="stale", code="c", exchange=_fake_exchange())


def test_link_requires_a_refresh_token_and_full_scopes(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")

    _, state = gi.authorize_url(rt, b_id)
    with pytest.raises(gi.GoogleLinkError, match="refresh token"):
        gi.complete_link(rt, state=state, code="c",
                         exchange=_fake_exchange(refresh=None))

    _, state2 = gi.authorize_url(rt, b_id)
    with pytest.raises(gi.GoogleLinkError, match="missing required"):
        gi.complete_link(rt, state=state2, code="c",
                         exchange=_fake_exchange(scope=[SCOPES[0]]))


def test_unconfigured_oauth_fails_clearly(tmp_path):
    rt = make_rt(tmp_path)          # no google_oauth_* settings
    assert gi.is_configured(rt) is False
    with pytest.raises(gi.GoogleLinkError, match="not configured"):
        gi.authorize_url(rt, OWNER_TENANT_ID)


# --- two tenants are isolated ------------------------------------------------

def test_two_tenants_tokens_are_isolated(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")

    for tid, token, email in ((b_id, "B-refresh", "b@gmail.com"),
                              (c_id, "C-refresh", "c@gmail.com")):
        _, state = gi.authorize_url(rt, tid)
        gi.complete_link(rt, state=state, code="c",
                         exchange=_fake_exchange(refresh=token, email=email))

    assert TenantCredentialStore(rt, b_id).load()["refresh_token"] == "B-refresh"
    assert TenantCredentialStore(rt, c_id).load()["refresh_token"] == "C-refresh"

    # neither tenant can see the other's row at all
    assert repo.integration_cred_get(TenantScope(rt.db, b_id),
                                     "google")["account_label"] == "b@gmail.com"
    assert repo.integration_cred_get(TenantScope(rt.db, c_id),
                                     "google")["account_label"] == "c@gmail.com"
    b_envelope = repo.integration_cred_get(TenantScope(rt.db, b_id),
                                           "google")["secret_envelope"]
    # ...and C's key cannot open B's envelope
    with pytest.raises(CredentialCryptoError):
        decrypt(_KEY, b_envelope, tenant_id=c_id, purpose="google")


# --- session registry / client resolution ------------------------------------

def test_each_tenant_gets_their_own_google_session(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    for tid, token in ((b_id, "B-refresh"), (c_id, "C-refresh")):
        _, state = gi.authorize_url(rt, tid)
        gi.complete_link(rt, state=state, code="c",
                         exchange=_fake_exchange(refresh=token))

    gi.register(rt)

    async def scenario():
        b = await rt.sessions.get(rt, b_id, "google")
        c = await rt.sessions.get(rt, c_id, "google")
        return b, c

    b_session, c_session = asyncio.run(scenario())
    assert b_session["gmail"] is not c_session["gmail"]
    assert b_session["calendar"] is not c_session["calendar"]
    # each session carries that tenant's credentials
    assert b_session["auth"]._store.load()["refresh_token"] == "B-refresh"
    assert c_session["auth"]._store.load()["refresh_token"] == "C-refresh"
    assert rt.sessions.live_count == 2


def test_a_tool_under_tenant_b_uses_bs_credentials(tmp_path):
    """The end-to-end property: dispatching a Google-backed tool as B resolves
    B's client, not the owner's."""
    from archon.api.ctx import tenant_ctx
    from archon.tools import calendar as calendar_tools

    rt = _rt(tmp_path)
    calendar_tools.register(rt.registry)
    b_id = _new_tenant(rt, "b@example.com")
    _, state = gi.authorize_url(rt, b_id)
    gi.complete_link(rt, state=state, code="c",
                     exchange=_fake_exchange(refresh="B-refresh"))
    gi.register(rt)

    ctx = tenant_ctx(rt, b_id)
    client = asyncio.run(calendar_tools._client(ctx.rt, ctx.tenant_id))
    assert client._auth._store.load()["refresh_token"] == "B-refresh"

    # the owner, unlinked, still resolves to the file-backed token — the live
    # single-user path is untouched
    owner_auth = gi.auth_for(rt, OWNER_TENANT_ID)
    assert owner_auth._store.describe().endswith(
        "(run scripts/google_consent.py and copy it over)")


def test_owner_prefers_a_linked_account_over_the_file_token(tmp_path):
    rt = _rt(tmp_path)
    _, state = gi.authorize_url(rt, OWNER_TENANT_ID)
    gi.complete_link(rt, state=state, code="c",
                     exchange=_fake_exchange(refresh="owner-refresh"))
    auth = gi.auth_for(rt, OWNER_TENANT_ID)
    assert isinstance(auth, GoogleAuth)
    assert auth._store.load()["refresh_token"] == "owner-refresh"


def test_unlink_revokes_and_drops_the_live_session(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _, state = gi.authorize_url(rt, b_id)
    gi.complete_link(rt, state=state, code="c", exchange=_fake_exchange())
    gi.register(rt)

    async def scenario():
        await rt.sessions.get(rt, b_id, "google")
        assert rt.sessions.live_count == 1
        return await gi.unlink(rt, b_id)

    assert asyncio.run(scenario()) is True
    assert rt.sessions.live_count == 0                    # session evicted
    assert repo.integration_cred_get(TenantScope(rt.db, b_id), "google") is None
    assert TenantCredentialStore(rt, b_id).load() is None


# --- API surface -------------------------------------------------------------

def _client_and_token(rt):
    from test_api import _client, _make_device_token

    return _client(rt), {"Authorization": f"Bearer {_make_device_token(rt)}"}


def test_authorize_endpoint_uses_the_calling_tenants_identity(tmp_path):
    rt = _rt(tmp_path)
    client, headers = _client_and_token(rt)

    r = client.post("/integrations/google/authorize", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["authorize_url"].startswith("https://accounts.google.com/")
    row = rt.db.query_one("SELECT * FROM oauth_states WHERE state = ?",
                          (body["state"],))
    # the device was created under the owner tenant, so the flow is bound to it
    assert row["tenant_id"] == OWNER_TENANT_ID


def test_authorize_endpoint_requires_auth(tmp_path):
    rt = _rt(tmp_path)
    from test_api import _client

    assert _client(rt).post("/integrations/google/authorize").status_code == 401


def test_callback_is_state_authenticated_not_bearer(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    from test_api import _client

    b_id = _new_tenant(rt, "b@example.com")
    _, state = gi.authorize_url(rt, b_id)
    monkeypatch.setattr(gi, "_exchange_code_over_http", _fake_exchange())

    client = _client(rt)
    r = client.get(f"/integrations/google/callback?state={state}&code=abc")
    assert r.status_code == 200 and "linked" in r.text.lower()
    assert TenantCredentialStore(rt, b_id).load()["refresh_token"] == "refresh-token"

    # a bogus state is rejected, and the page never echoes the code
    bad = client.get("/integrations/google/callback?state=nope&code=SEEKRIT")
    assert bad.status_code == 200 and "SEEKRIT" not in bad.text
    assert "failed" in bad.text.lower()


def test_integrations_list_shows_link_status(tmp_path):
    rt = _rt(tmp_path)
    client, headers = _client_and_token(rt)
    assert client.get("/integrations", headers=headers).json()["integrations"] == []

    _, state = gi.authorize_url(rt, OWNER_TENANT_ID)
    gi.complete_link(rt, state=state, code="c",
                     exchange=_fake_exchange(email="owner@gmail.com"))
    listed = client.get("/integrations", headers=headers).json()["integrations"]
    assert listed[0]["provider"] == "google"
    assert listed[0]["account_label"] == "owner@gmail.com"
    assert listed[0]["revoked_at"] is None
    # the secret itself is never exposed by the API
    assert "refresh" not in json.dumps(listed).lower()
