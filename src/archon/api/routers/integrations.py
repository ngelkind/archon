"""Per-tenant integration linking (Google first).

``/integrations/google/authorize`` is authenticated — it mints a consent URL for
the *calling* tenant, taken from the device/JWT identity, never from the request
body.

``/integrations/google/callback`` cannot be authenticated: Google redirects the
browser there and will not carry a bearer token. Its security comes from the
``state`` value instead, which was issued to one tenant, stored server-side,
and is single-use and time-bounded — so the callback learns which tenant a code
belongs to from something the server minted, not from anything the caller says.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from ...integrations import google as google_integration
from ..auth import require_device
from ..schemas import (
    IntegrationLinkStart, IntegrationStatus, IntegrationStatusList, ToolCallResponse,
)

router = APIRouter(tags=["integrations"])

authed = APIRouter(dependencies=[Depends(require_device)], tags=["integrations"])


@authed.get("/integrations", response_model=IntegrationStatusList)
async def list_integrations(request: Request,
                            device: sqlite3.Row = Depends(require_device)):
    from ...db import repo
    from ...db.tenancy import TenantScope

    rt = request.app.state.rt
    scope = TenantScope(rt.db, int(device["tenant_id"]))
    return IntegrationStatusList(integrations=[
        IntegrationStatus(
            provider=r["provider"], account_label=r["account_label"],
            scopes=(r["scopes"] or "").split() or None,
            linked_at=r["updated_at"], revoked_at=r["revoked_at"],
        )
        for r in repo.integration_cred_list(scope)
    ])


@authed.post("/integrations/google/authorize", response_model=IntegrationLinkStart)
async def google_authorize(request: Request,
                           device: sqlite3.Row = Depends(require_device)):
    """Consent URL for the calling tenant. The app opens this in a browser."""
    rt = request.app.state.rt
    try:
        url, state = google_integration.authorize_url(rt, int(device["tenant_id"]))
    except google_integration.GoogleLinkError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=str(exc)) from exc
    return IntegrationLinkStart(authorize_url=url, state=state)


@authed.delete("/integrations/google", response_model=ToolCallResponse)
async def google_unlink(request: Request,
                        device: sqlite3.Row = Depends(require_device)):
    rt = request.app.state.rt
    revoked = await google_integration.unlink(rt, int(device["tenant_id"]))
    return ToolCallResponse(result='{"ok": %s}' % ("true" if revoked else "false"))


@router.get("/integrations/google/callback", response_class=HTMLResponse)
async def google_callback(
    request: Request,
    state: str = Query(default=""),
    code: str = Query(default=""),
    error: str | None = Query(default=None),
):
    """Google's redirect target. Renders a plain page for the user's browser.

    Deliberately not a JSON API: a human is looking at this. It never echoes the
    authorization code, and reports failures without saying which part of the
    state check failed.
    """
    rt = request.app.state.rt
    if error:
        return _page("Google link cancelled", f"Google reported: {error}")
    if not state or not code:
        return _page("Google link failed", "The redirect was missing its state or code.")
    try:
        result = google_integration.complete_link(rt, state=state, code=code)
    except google_integration.GoogleLinkError as exc:
        rt.audit.note("google_link_failed", error=str(exc)[:200])
        return _page("Google link failed", str(exc))
    account = result.get("account") or "your Google account"
    return _page("Google linked", f"{account} is now connected. You can close this tab.")


def _page(title: str, message: str) -> HTMLResponse:
    from html import escape

    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title>"
        "<body style=\"font-family:system-ui,sans-serif;max-width:32rem;"
        "margin:4rem auto;padding:0 1rem;line-height:1.5\">"
        f"<h1 style='font-size:1.25rem'>{escape(title)}</h1>"
        f"<p>{escape(message)}</p></body>"
    )
