"""Contact directory: read the table, sync the device address book in.

The sync writes through the ``contact_remember`` tool per entry rather than bulk
SQL, so every learned name/phone lands in the audit trail exactly as if the
owner had taught it. The app is expected to send only new/changed entries; the
batch is capped so one runaway sync cannot flood the log.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request

from ...db import repo
from ...db.tenancy import TenantScope
from ..auth import require_tenant
from ..ctx import tenant_ctx
from ..schemas import (
    Contact, ContactsResponse, ContactSyncRequest, ContactSyncResponse,
)

router = APIRouter(dependencies=[Depends(require_tenant)], tags=["contacts"])

_MAX_SYNC = 1000


@router.get("/contacts", response_model=ContactsResponse)
async def list_contacts(
    request: Request, limit: int = Query(default=500, ge=1, le=2000),
    tenant_id: int = Depends(require_tenant),
) -> ContactsResponse:
    db = TenantScope(request.app.state.rt.db, tenant_id)
    counts = repo.contact_counts(db)
    return ContactsResponse(
        contacts=[
            Contact(name=r["name"], phone=r["phone"], source=r["source"])
            for r in repo.contact_list(db, limit=limit)
        ],
        entries=int(counts["entries"]) if counts else 0,
        unique_numbers=int(counts["unique_numbers"]) if counts else 0,
    )


@router.post("/contacts/sync", response_model=ContactSyncResponse)
async def sync_contacts(body: ContactSyncRequest, request: Request,
                        tenant_id: int = Depends(require_tenant)
                        ) -> ContactSyncResponse:
    rt = request.app.state.rt
    ctx = tenant_ctx(rt, tenant_id)
    stored = skipped = 0
    for entry in body.contacts[:_MAX_SYNC]:
        if not entry.name.strip() or not entry.phone.strip():
            skipped += 1
            continue
        result = await rt.registry.dispatch(
            ctx, "contact_remember", {"name": entry.name, "phone": entry.phone}
        )
        try:
            ok = bool(json.loads(result).get("ok"))
        except ValueError:
            ok = False
        stored += int(ok)
        skipped += int(not ok)
    return ContactSyncResponse(
        submitted=len(body.contacts), stored=stored, skipped=skipped
    )
