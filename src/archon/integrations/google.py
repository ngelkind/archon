"""Per-tenant Google (Gmail + Calendar) linking.

The compliant core of the product: Google supports this at scale through normal
OAuth, so each tenant grants access to their own account and we hold only their
refresh token — encrypted, scoped to them.

Flow
----
1. ``authorize_url(rt, tenant_id)`` mints a single-use ``state`` bound to that
   tenant, stores it, and returns Google's consent URL.
2. The user consents; Google redirects to the callback with ``code`` + ``state``.
3. ``complete_link(rt, state, code)`` spends the state — which is what tells us
   *which tenant* the code belongs to, and stops a pasted code from linking an
   attacker's account into someone else's tenant — exchanges the code, and
   stores the resulting token encrypted under that tenant.

``exchange_code`` is the single network boundary and is injected, so tests can
run the whole flow without touching Google.

The owner tenant is deliberately untouched: the personal bot keeps using
``token.json`` unless it has explicitly linked through this flow.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from ..db import repo
from ..platforms.google_auth import SCOPES, GoogleAuth, TenantCredentialStore

PROVIDER = "google"
_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 — URL, not a secret
_STATE_TTL_MINUTES = 15


class GoogleLinkError(RuntimeError):
    """The link flow could not be completed."""


def is_configured(rt: Any) -> bool:
    s = rt.settings
    return bool(s.google_oauth_client_id.strip()
                and s.google_oauth_client_secret.strip()
                and s.google_oauth_redirect_uri.strip())


def _require_configured(rt: Any) -> None:
    if not is_configured(rt):
        raise GoogleLinkError(
            "Google OAuth is not configured — set GOOGLE_OAUTH_CLIENT_ID, "
            "GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REDIRECT_URI from a "
            "Google Cloud OAuth client (Web application)"
        )


def authorize_url(rt: Any, tenant_id: int) -> tuple[str, str]:
    """(consent URL, state) for this tenant. The state is stored single-use."""
    from urllib.parse import urlencode

    _require_configured(rt)
    state = secrets.token_urlsafe(32)
    expires = (datetime.now(UTC) + timedelta(minutes=_STATE_TTL_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    repo.oauth_state_create(rt.db, state=state, tenant_id=tenant_id,
                            provider=PROVIDER, expires_at=expires)
    params = {
        "client_id": rt.settings.google_oauth_client_id,
        "redirect_uri": rt.settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline + consent so we actually receive a refresh token, including on
        # a re-link where Google would otherwise omit it.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{_AUTH_ENDPOINT}?{urlencode(params)}", state


def _exchange_code_over_http(rt: Any, code: str) -> dict[str, Any]:
    """Trade an authorization code for tokens. The only network call here."""
    import httpx

    response = httpx.post(_TOKEN_ENDPOINT, timeout=30, data={
        "code": code,
        "client_id": rt.settings.google_oauth_client_id,
        "client_secret": rt.settings.google_oauth_client_secret,
        "redirect_uri": rt.settings.google_oauth_redirect_uri,
        "grant_type": "authorization_code",
    })
    if response.status_code >= 400:
        # Google echoes the code back in some errors; never log the body.
        raise GoogleLinkError(
            f"Google rejected the authorization code (HTTP {response.status_code})"
        )
    return response.json()


#: Injection point for tests — replaced with a stub so no network is needed.
ExchangeFn = Callable[[Any, str], dict[str, Any]]


def complete_link(rt: Any, *, state: str, code: str,
                  exchange: ExchangeFn | None = None) -> dict[str, Any]:
    """Finish the flow: spend the state, exchange the code, store the token.

    Returns a small summary (tenant + account label). Never returns the token.
    """
    _require_configured(rt)
    row = repo.oauth_state_consume(rt.db, state, PROVIDER)
    if row is None:
        raise GoogleLinkError("invalid, expired, or already-used OAuth state")
    tenant_id = int(row["tenant_id"])

    payload = (exchange or _exchange_code_over_http)(rt, code)
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        # Without offline access we could not act for the user later.
        raise GoogleLinkError(
            "Google did not return a refresh token — the account may already be "
            "linked; revoke access and link again"
        )

    granted = (payload.get("scope") or "").split() or list(SCOPES)
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        raise GoogleLinkError(
            "missing required Google permissions: " + ", ".join(missing)
        )

    token_json = {
        "token": payload.get("access_token"),
        "refresh_token": refresh_token,
        "token_uri": _TOKEN_ENDPOINT,
        "client_id": rt.settings.google_oauth_client_id,
        "client_secret": rt.settings.google_oauth_client_secret,
        "scopes": granted,
        "account_label": payload.get("account_label") or payload.get("email"),
    }
    TenantCredentialStore(rt, tenant_id).save(token_json)
    rt.audit.note("google_linked", tenant_id=tenant_id,
                  account=token_json.get("account_label"))
    return {"tenant_id": tenant_id, "account": token_json.get("account_label"),
            "scopes": granted}


async def unlink(rt: Any, tenant_id: int) -> bool:
    """Revoke the stored credential and drop any live session for it."""
    from ..db.tenancy import TenantScope

    revoked = repo.integration_cred_revoke(TenantScope(rt.db, tenant_id), PROVIDER)
    await rt.sessions.evict(tenant_id, PROVIDER)
    if revoked:
        rt.audit.note("google_unlinked", tenant_id=tenant_id)
    return revoked


def auth_for(rt: Any, tenant_id: int) -> GoogleAuth:
    """A GoogleAuth for one tenant.

    The owner falls back to the file-backed token so the single-user deployment
    keeps working exactly as before — unless the owner has linked through this
    flow, in which case the stored credential wins.
    """
    from ..db.tenancy import OWNER_TENANT_ID, TenantScope

    linked = repo.integration_cred_get(TenantScope(rt.db, tenant_id), PROVIDER)
    if linked is None and tenant_id == OWNER_TENANT_ID:
        return GoogleAuth(rt.settings.google_token_path)
    return GoogleAuth(TenantCredentialStore(rt, tenant_id))


def build_session(rt: Any, tenant_id: int) -> dict[str, Any]:
    """SessionRegistry factory for ``google``.

    Returns the tenant's Gmail and Calendar clients, both sharing one
    authorized-credential object so a refresh is done once per tenant.
    """
    from ..calendar_.client import CalendarClient
    from ..platforms.gmail.client import GmailClient

    auth = auth_for(rt, tenant_id)
    return {
        "auth": auth,
        "gmail": GmailClient(auth),
        "calendar": CalendarClient(auth, rt.settings.timezone),
    }


async def client_for(rt: Any, tenant_id: int, which: str) -> Any:
    """This tenant's ``gmail`` / ``calendar`` client, built on first use.

    Self-registering so a caller never has to care whether startup wired the
    factory — which keeps single-user runs and tests working unchanged.
    """
    if not rt.sessions.has_factory(PROVIDER):
        register(rt)
    session = await rt.sessions.get(rt, tenant_id, PROVIDER)
    return session[which]


def register(rt: Any) -> None:
    """Teach the session registry how to build a tenant's Google clients."""
    rt.sessions.register_factory(PROVIDER, build_session)
