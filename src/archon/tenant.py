"""The per-tenant half of the Runtime split.

``Runtime`` is process-global and stays that way: settings, the database handle,
the LLM router, the tool registry, the event hub, the session registry. None of
those are per-user.

Everything that *is* per-user now lives behind a :class:`TenantContext`: which
tenant, a database scope bound to it, that tenant's settings, and that tenant's
integration sessions. Code that used to reach for ``rt.db`` and implicitly mean
"the owner's data" takes a context instead and means "this tenant's data".

The single-user bot keeps working unchanged because tenant 1 *is* the owner:
``owner_context(rt)`` returns a context over the data migration 008 backfilled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import repo
from .db.tenancy import OWNER_TENANT_ID, TenantScope


@dataclass(slots=True)
class TenantContext:
    """One tenant's view of the world. Cheap to build; hold it for a request."""

    rt: Any                 # Runtime (untyped to avoid a circular import)
    tenant_id: int
    scope: TenantScope

    # --- data ----------------------------------------------------------------

    def setting(self, key: str, default: Any = None) -> Any:
        """A setting for THIS tenant, falling back to the process default."""
        return repo.setting_get(self.scope, key, default)

    def set_setting(self, key: str, value: Any) -> None:
        repo.setting_set(self.scope, key, value)

    @property
    def is_owner(self) -> bool:
        """The original single-user owner (the personal bot's own tenant)."""
        return self.tenant_id == OWNER_TENANT_ID

    # --- integrations --------------------------------------------------------

    async def session(self, kind: str) -> Any:
        """This tenant's live client for an integration, built on first use.

        Isolation happens here: a context can only ever reach sessions keyed to
        its own ``tenant_id``.
        """
        return await self.rt.sessions.get(self.rt, self.tenant_id, kind)

    def peek_session(self, kind: str) -> Any | None:
        return self.rt.sessions.peek(self.tenant_id, kind)

    async def drop_sessions(self, kind: str | None = None) -> int:
        return await self.rt.sessions.evict(self.tenant_id, kind)

    # --- the tenant's agent conversation -------------------------------------

    def control_chat_pk(self) -> int:
        """The chat row backing this tenant's "mind" conversation.

        For the owner this is exactly the chat the personal bot has always used
        (their Telegram control chat), so the existing rolling context and its
        history carry over untouched. Product tenants get a synthetic per-tenant
        chat instead — they have no owner Telegram id, and two tenants must not
        collide on one.
        """
        if self.is_owner:
            return repo.chat_upsert(
                self.scope, "tg", str(self.rt.settings.telegram_owner_id),
                "Archon control", "private",
            )
        return repo.chat_upsert(
            self.scope, "app", f"mind:{self.tenant_id}", "Archon", "private"
        )


def tenant_context(rt: Any, tenant_id: int) -> TenantContext:
    """Build a context for an arbitrary tenant.

    The tenant id must come from an authenticated identity (a device row's
    ``tenant_id``, or a verified JWT subject) — never from request data.
    """
    return TenantContext(rt=rt, tenant_id=tenant_id,
                         scope=TenantScope(rt.db, tenant_id))


def owner_context(rt: Any) -> TenantContext:
    """Context for the single-user owner — the personal bot's own tenant."""
    return tenant_context(rt, OWNER_TENANT_ID)
