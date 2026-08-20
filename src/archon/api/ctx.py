"""Build a ToolContext for API-originated calls.

Single-user (``multitenant_enabled`` off): the context is the owner's, and it
reuses the SAME control-chat pk ``agent.owner`` uses, so the app and the
Telegram control bot share one memory thread.

Multi-tenant: the caller passes the tenant established by authentication — a
device row's ``tenant_id`` or a verified JWT subject. It is never read from
request data, so a client cannot address another tenant by asking.

Scope stays ``owner`` in the tool-registry sense throughout: that is the
*capability* level (admin tools allowed), not the identity. Identity is the
tenant, and it bounds the data the run can touch.
"""

from __future__ import annotations

from ..runtime import Runtime
from ..tenant import TenantContext, owner_context, tenant_context
from ..tools.registry import ToolContext


def tenant_ctx(rt: Runtime, tenant_id: int | None = None) -> ToolContext:
    """Owner-capability tool context bound to one tenant's data."""
    tenant: TenantContext = (
        owner_context(rt) if tenant_id is None else tenant_context(rt, tenant_id)
    )
    return ToolContext(
        rt=rt,
        scope="owner",
        origin_chat_pk=tenant.control_chat_pk(),
        extras={"transport": "api", "tenant_id": tenant.tenant_id},
        tenant=tenant,
    )


def api_owner_ctx(rt: Runtime) -> ToolContext:
    """The single-user owner's context (the personal bot's tenant)."""
    return tenant_ctx(rt, None)
