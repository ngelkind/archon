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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from ...integrations import google as google_integration
from ...integrations import telegram as tg_integration
from ...integrations import telegram_userbot as tg_userbot
from ...integrations import whatsapp as wa_integration
from ..auth import require_tenant
from ..schemas import (
    IntegrationLinkStart, IntegrationStatus, IntegrationStatusList,
    TelegramLinkStart, TelegramStatus, ToolCallResponse,
    TelegramUserbotComplete, TelegramUserbotConsent, TelegramUserbotStart,
    TelegramUserbotStatus, WhatsAppConsent, WhatsAppLinkRequest,
    WhatsAppStatus,
)

router = APIRouter(tags=["integrations"])

authed = APIRouter(dependencies=[Depends(require_tenant)], tags=["integrations"])


@authed.get("/integrations", response_model=IntegrationStatusList)
async def list_integrations(request: Request,
                            tenant_id: int = Depends(require_tenant)):
    from ...db import repo
    from ...db.tenancy import TenantScope

    rt = request.app.state.rt
    scope = TenantScope(rt.db, tenant_id)
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
                           tenant_id: int = Depends(require_tenant)):
    """Consent URL for the calling tenant. The app opens this in a browser."""
    rt = request.app.state.rt
    try:
        url, state = google_integration.authorize_url(rt, tenant_id)
    except google_integration.GoogleLinkError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=str(exc)) from exc
    return IntegrationLinkStart(authorize_url=url, state=state)


@authed.delete("/integrations/google", response_model=ToolCallResponse)
async def google_unlink(request: Request,
                        tenant_id: int = Depends(require_tenant)):
    rt = request.app.state.rt
    revoked = await google_integration.unlink(rt, tenant_id)
    return ToolCallResponse(result='{"ok": %s}' % ("true" if revoked else "false"))


@authed.post("/integrations/telegram/link", response_model=TelegramLinkStart)
async def telegram_link(request: Request,
                        tenant_id: int = Depends(require_tenant)):
    """Issue a single-use code the user sends to the product bot.

    Redeeming it in Telegram is what proves they hold that account — which is
    the only way to know which tenant a later Business connection belongs to.
    """
    rt = request.app.state.rt
    return TelegramLinkStart(**tg_integration.start_link(rt, tenant_id))


@authed.get("/integrations/telegram", response_model=TelegramStatus)
async def telegram_status(request: Request,
                          tenant_id: int = Depends(require_tenant)):
    from ...db.tenancy import TenantScope

    rt = request.app.state.rt
    scope = TenantScope(rt.db, tenant_id)
    return TelegramStatus(**tg_integration.status(scope))


@authed.delete("/integrations/telegram", response_model=ToolCallResponse)
async def telegram_unlink(request: Request,
                          tenant_id: int = Depends(require_tenant)):
    rt = request.app.state.rt
    revoked = tg_integration.unlink(rt, tenant_id)
    return ToolCallResponse(result='{"ok": %s}' % ("true" if revoked else "false"))


@authed.get("/integrations/telegram/userbot/consent",
            response_model=TelegramUserbotConsent)
async def telegram_userbot_consent(request: Request,
                                   tenant_id: int = Depends(require_tenant)):
    """The ban warning the app must display, verbatim, before offering to link."""
    return TelegramUserbotConsent(**tg_userbot.consent_notice())


@authed.post("/integrations/telegram/userbot/start",
             response_model=TelegramUserbotStatus)
async def telegram_userbot_start(body: TelegramUserbotStart, request: Request,
                                 tenant_id: int = Depends(require_tenant)):
    """Send the login code. REFUSED (400) unless the risk was acknowledged."""
    from ...db.tenancy import TenantScope

    rt = request.app.state.rt
    tenant_id = tenant_id
    try:
        await tg_userbot.start_login(
            rt, tenant_id, phone=body.phone,
            consent_acknowledged=body.consent_acknowledged,
            consent_version=body.consent_version,
        )
    except tg_userbot.ConsentRequired as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=str(exc)) from exc
    except tg_userbot.UserbotLinkError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=str(exc)) from exc
    return TelegramUserbotStatus(
        **tg_userbot.status(rt, TenantScope(rt.db, tenant_id)))


@authed.post("/integrations/telegram/userbot/complete",
             response_model=TelegramUserbotStatus)
async def telegram_userbot_complete(body: TelegramUserbotComplete, request: Request,
                                    tenant_id: int = Depends(require_tenant)):
    """Finish signing in. 409 means the account has 2FA and needs a password."""
    from ...db.tenancy import TenantScope

    rt = request.app.state.rt
    tenant_id = tenant_id
    try:
        await tg_userbot.complete_login(rt, tenant_id, code=body.code,
                                        password=body.password)
    except tg_userbot.PasswordRequired as exc:
        # A distinct code so the app can prompt for the password and retry,
        # rather than treating it as a failed login.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=str(exc)) from exc
    except tg_userbot.UserbotLinkError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=str(exc)) from exc
    return TelegramUserbotStatus(
        **tg_userbot.status(rt, TenantScope(rt.db, tenant_id)))


@authed.get("/integrations/telegram/userbot",
            response_model=TelegramUserbotStatus)
async def telegram_userbot_status(request: Request,
                                  tenant_id: int = Depends(require_tenant)):
    from ...db.tenancy import TenantScope

    rt = request.app.state.rt
    return TelegramUserbotStatus(
        **tg_userbot.status(rt, TenantScope(rt.db, tenant_id)))


@authed.delete("/integrations/telegram/userbot", response_model=ToolCallResponse)
async def telegram_userbot_unlink(request: Request,
                                  tenant_id: int = Depends(require_tenant)):
    rt = request.app.state.rt
    revoked = await tg_userbot.unlink(rt, tenant_id)
    return ToolCallResponse(result='{"ok": %s}' % ("true" if revoked else "false"))


@authed.get("/integrations/whatsapp/consent", response_model=WhatsAppConsent)
async def whatsapp_consent(request: Request,
                           tenant_id: int = Depends(require_tenant)):
    """The permanent-ban warning the app must display, and its version.

    Served as its own endpoint so the app cannot render a paraphrase: it shows
    this text, and echoes this version back when the user accepts.
    """
    return WhatsAppConsent(**wa_integration.consent_notice())


@authed.post("/integrations/whatsapp/link", response_model=WhatsAppStatus)
async def whatsapp_link(body: WhatsAppLinkRequest, request: Request,
                        tenant_id: int = Depends(require_tenant)):
    """Start pairing. REFUSED (400) unless the ban warning was acknowledged."""
    from ...db.tenancy import TenantScope

    rt = request.app.state.rt
    tenant_id = tenant_id
    try:
        wa_integration.start_link(
            rt, tenant_id,
            consent_acknowledged=body.consent_acknowledged,
            consent_version=body.consent_version,
        )
    except wa_integration.ConsentRequired as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=str(exc)) from exc
    except wa_integration.WhatsAppLinkError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=str(exc)) from exc
    return WhatsAppStatus(**wa_integration.status(rt, TenantScope(rt.db, tenant_id)))


@authed.get("/integrations/whatsapp", response_model=WhatsAppStatus)
async def whatsapp_status(request: Request,
                          tenant_id: int = Depends(require_tenant)):
    from ...db.tenancy import TenantScope

    rt = request.app.state.rt
    scope = TenantScope(rt.db, tenant_id)
    return WhatsAppStatus(**wa_integration.status(rt, scope))


@authed.delete("/integrations/whatsapp", response_model=ToolCallResponse)
async def whatsapp_unlink(request: Request,
                          tenant_id: int = Depends(require_tenant)):
    rt = request.app.state.rt
    revoked = await wa_integration.unlink(rt, tenant_id)
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
