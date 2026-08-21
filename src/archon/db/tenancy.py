"""Tenant scoping — the mechanism that makes cross-tenant reads/writes impossible.

The rule this module enforces: **the tenant of a query never comes from caller
arguments; it comes from the scope object the caller had to hold.** A caller
cannot ask for "tenant 7's chats" by passing 7 as data — it must construct a
``TenantScope`` for tenant 7, which is an explicit, greppable, auditable act.

Two layers, deliberately:

1. *Structural.* Every per-user accessor in ``repo.py`` takes a store and runs
   it through :func:`as_scope`, then injects ``scope.tenant_id`` into the SQL
   itself. There is no code path that reads a tenanted table without a tenant
   predicate, so "forgot to filter" is not an available mistake.

2. *Tripwire.* :meth:`TenantScope.query` and friends refuse to run a statement
   that names a tenanted table without mentioning ``tenant_id``. This catches
   the next person adding SQL by hand — the failure is a loud exception at the
   call site, not a silent cross-tenant leak in production.

Legacy single-user callers pass a raw ``Db`` and are normalised to
:data:`OWNER_TENANT_ID`. That is not a loophole: migration 008 backfilled all
pre-existing rows to tenant 1, so the personal bot querying "its" data and
querying "tenant 1's" data are the same query. It keeps the live single-user
path working with no call-site churn while still being tenant-filtered.
"""

from __future__ import annotations

import re
from typing import Any

from . import Db

#: The original single-user owner. Migration 008 backfills all pre-tenancy rows
#: to this tenant, and raw-``Db`` callers resolve to it.
OWNER_TENANT_ID = 1

#: Tables that carry a ``tenant_id`` column (see ``008_tenancy.sql``). Keep in
#: sync with that migration — the isolation test asserts they match.
TENANTED_TABLES = frozenset({
    "settings", "gmail_state", "personas", "chats", "messages", "contacts",
    "context_messages", "pending_actions", "scheduled_messages",
    "pending_replies", "llm_calls", "events_created", "sub_bots", "api_devices",
    # NULLABLE tenant_id: NULL marks a SYSTEM row (see 009_audit_tenant.sql).
    "audit",
    "integration_credentials", "telegram_links", "whatsapp_links",
    "telegram_userbot_links",
})

#: Intentionally global: identity, process bookkeeping, and the system log.
GLOBAL_TABLES = frozenset({
    "schema_version", "users", "refresh_tokens", "api_pair_codes",
})

#: Carry a ``tenant_id`` column but are deliberately NOT tenant-scoped: they are
#: read before a tenant scope exists, and the row IS the record of which tenant
#: the in-flight handshake belongs to. Looked up only by their own random,
#: single-use primary key.
PRE_AUTH_TABLES = frozenset({"oauth_states", "telegram_link_codes"})

_IDENT = re.compile(r"[a-z_][a-z0-9_]*")


class TenantScopeError(RuntimeError):
    """A statement touched a tenanted table without constraining ``tenant_id``."""


class TenantScope:
    """A database handle bound to exactly one tenant."""

    __slots__ = ("db", "tenant_id")

    def __init__(self, db: Db, tenant_id: int) -> None:
        # bool is an int subclass; True would silently mean tenant 1.
        if isinstance(tenant_id, bool) or not isinstance(tenant_id, int) or tenant_id <= 0:
            raise ValueError(f"tenant_id must be a positive int, got {tenant_id!r}")
        self.db = db
        self.tenant_id = tenant_id

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"TenantScope(tenant_id={self.tenant_id})"

    def _guard(self, sql: str) -> None:
        lowered = sql.lower()
        if "tenant_id" in lowered:
            return
        touched = set(_IDENT.findall(lowered)) & TENANTED_TABLES
        if touched:
            raise TenantScopeError(
                f"statement touches tenanted table(s) {sorted(touched)} without a "
                f"tenant_id predicate — add `tenant_id = ?` and pass "
                f"scope.tenant_id: {' '.join(sql.split())[:160]}"
            )

    def query(self, sql: str, params: tuple | dict = ()) -> list:
        self._guard(sql)
        return self.db.query(sql, params)

    def query_one(self, sql: str, params: tuple | dict = ()):
        self._guard(sql)
        return self.db.query_one(sql, params)

    def execute(self, sql: str, params: tuple | dict = ()):
        self._guard(sql)
        return self.db.execute(sql, params)


def as_scope(store: Db | TenantScope) -> TenantScope:
    """Normalise a store to a :class:`TenantScope`.

    A raw ``Db`` means the legacy single-user path and resolves to the owner
    tenant. Passing a scope through unchanged is what lets multi-tenant callers
    (``TenantContext``) reach the same accessors.
    """
    if isinstance(store, TenantScope):
        return store
    return TenantScope(store, OWNER_TENANT_ID)


def tenant_id_of(store: Db | TenantScope) -> int:
    """Which tenant a store acts for; the owner for a raw Db."""
    return as_scope(store).tenant_id


def owner_scope(db: Db) -> TenantScope:
    """Explicit scope for the single-user owner's data."""
    return TenantScope(db, OWNER_TENANT_ID)


def tenant_exists(db: Db, tenant_id: int) -> bool:
    """Whether a tenant row exists at all (including disabled accounts, which
    ``repo.user_by_id`` hides — the owner tenant is one of those)."""
    return db.query_one("SELECT 1 FROM users WHERE id = ?", (tenant_id,)) is not None


#: Child-before-parent order for deleting a tenant's rows.
_PURGE_ORDER = (
    "messages", "context_messages", "events_created", "scheduled_messages",
    "pending_replies", "pending_actions", "llm_calls", "api_devices",
    "sub_bots", "contacts", "settings", "gmail_state", "chats", "personas",
    "audit", "integration_credentials", "telegram_links", "whatsapp_links",
    "telegram_userbot_links",
    # Pre-auth handshake rows also carry a tenant_id FK, so an account that ever
    # STARTED a link flow cannot be deleted until these go too.
    "oauth_states", "telegram_link_codes",
)


def tenant_purge(db: Db, tenant_id: int, *, delete_user: bool = True) -> dict[str, int]:
    """Erase a tenant's data. Returns rows deleted per table.

    Deliberately explicit rather than an ``ON DELETE CASCADE`` on every table:
    the six rebuilt tables do cascade, but the eight that only gained a column
    cannot (SQLite cannot add a cascading FK to an existing table without
    another rebuild). Rather than have deletion half-cascade — which would leave
    orphaned messages and cost rows behind while appearing to work — account
    deletion goes through this one function, in child-before-parent order.

    This is the GDPR "delete my account" primitive; it is cross-tenant by nature
    and must only be called from an authenticated account-deletion path.
    """
    if tenant_id == OWNER_TENANT_ID:
        raise ValueError(
            "refusing to purge the owner tenant — that is the single-user bot's "
            "own data"
        )
    deleted: dict[str, int] = {}
    for table in _PURGE_ORDER:
        cur = db.execute(f"DELETE FROM {table} WHERE tenant_id = ?", (tenant_id,))  # noqa: S608
        if cur.rowcount:
            deleted[table] = int(cur.rowcount)
    cur = db.execute("DELETE FROM refresh_tokens WHERE user_id = ?", (tenant_id,))
    if cur.rowcount:
        deleted["refresh_tokens"] = int(cur.rowcount)
    if delete_user:
        db.execute("DELETE FROM users WHERE id = ?", (tenant_id,))
        deleted["users"] = 1
    return deleted


def unscoped_all(db: Db, table: str, columns: str = "*") -> list[Any]:
    """Deliberate cross-tenant read, for admin/ops paths only.

    Named to be obvious in review and in a grep. Anything that is not an
    explicitly-admin code path should use a ``TenantScope`` instead.
    """
    if table not in TENANTED_TABLES | GLOBAL_TABLES:
        raise ValueError(f"unknown table: {table}")
    return db.query(f"SELECT {columns} FROM {table}")  # noqa: S608 — table is allow-listed
