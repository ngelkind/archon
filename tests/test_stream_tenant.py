"""/stream must not leak one tenant's events to another's socket.

The socket authenticates a tenant but used to forward EVERY event on the hub;
a product user's /stream then saw the owner's (and every other tenant's)
approvals, costs and network activity. The filter fixes that.
"""

from __future__ import annotations

from test_api import _client, _pepper, make_rt

from archon.api.security import hash_secret, mint_token
from archon.db import repo
from archon.db.tenancy import OWNER_TENANT_ID, TenantScope


def _token_for(rt, tenant_id):
    token = mint_token()
    repo.api_device_create(TenantScope(rt.db, tenant_id), name="d",
                           token_hash=hash_secret(_pepper(rt), token))
    return token


def test_stream_isolates_tenants(tmp_path):
    rt = make_rt(tmp_path)
    tid = repo.user_create(rt.db, email="p@x.com", password_hash="x", display_name=None)
    token = _token_for(rt, tid)

    with _client(rt).websocket_connect(f"/stream?token={token}") as ws:
        # The owner's event must be filtered out; only the tenant's own arrives.
        rt.events.publish("approval.pending", action_id=1, tenant_id=OWNER_TENANT_ID)
        rt.events.publish("approval.pending", action_id=2, tenant_id=tid)
        event = ws.receive_json()

    assert event["data"]["action_id"] == 2, "the owner's event leaked to the tenant"
    assert event["data"]["tenant_id"] == tid


def test_stream_owner_still_sees_untagged_system_events(tmp_path):
    rt = make_rt(tmp_path)
    token = _token_for(rt, OWNER_TENANT_ID)
    with _client(rt).websocket_connect(f"/stream?token={token}") as ws:
        rt.events.publish("net.call", host="api.telegram.org")  # untagged, system-wide
        event = ws.receive_json()
    assert event["kind"] == "net.call"
