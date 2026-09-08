"""Product-tenant confirmations go to the RIGHT person and only they can act.

The bug: a product user's confirmation card was sent to the owner (who could
approve a stranger's action), and the product bot had no callback handler at
all. These check the routing (_confirm_target), the resolve tenant
(_tenant_for_action) and the cross-tenant guard (_tap_authorized).
"""

from __future__ import annotations

from test_m2 import make_rt

from archon.db import repo
from archon.db.tenancy import TenantScope
from archon.pipeline import confirm

_FUTURE = "2099-01-01 00:00:00"


def _action(store, kind="send"):
    return repo.pending_action_create(store, kind=kind, payload_json="{}",
                                      chat_pk=None, expires_at=_FUTURE)


def _product_tenant(rt, tg_user_id):
    tid = repo.user_create(rt.db, email=f"{tg_user_id}@x.com",
                           password_hash="x", display_name=None)
    repo.telegram_link_upsert(TenantScope(rt.db, tid), tg_user_id=str(tg_user_id))
    return tid


class _Q:
    def __init__(self, uid):
        self.from_user = type("U", (), {"id": uid})()


def test_owner_action_targets_the_owner(tmp_path):
    rt = make_rt(tmp_path)
    aid = _action(rt.db)
    _bot, chat_id, store = confirm._confirm_target(rt, aid)
    assert chat_id == rt.settings.telegram_owner_id
    assert store is rt.db
    assert confirm._tenant_for_action(rt, aid) is None  # owner uses raw Db


def test_product_action_goes_to_the_user_over_the_product_bot(tmp_path):
    rt = make_rt(tmp_path)
    tid = _product_tenant(rt, 77701)
    sentinel = object()
    rt.clients["product_bot"] = sentinel
    aid = _action(TenantScope(rt.db, tid))

    bot, chat_id, _store = confirm._confirm_target(rt, aid)
    assert bot is sentinel                       # the product bot, not send_bot()
    assert chat_id == 77701                       # the user...
    assert chat_id != rt.settings.telegram_owner_id  # ...never the owner
    ctx = confirm._tenant_for_action(rt, aid)
    assert ctx is not None and ctx.tenant_id == tid


def test_only_the_owning_product_user_may_resolve(tmp_path):
    rt = make_rt(tmp_path)
    tid = _product_tenant(rt, 88801)
    aid = _action(TenantScope(rt.db, tid))
    assert confirm._tap_authorized(rt, _Q(88801), aid) is True    # the linked user
    assert confirm._tap_authorized(rt, _Q(99999), aid) is False   # a stranger


def test_owner_actions_are_gated_by_the_control_middleware_not_here(tmp_path):
    rt = make_rt(tmp_path)
    aid = _action(rt.db)  # owner tenant
    # On the control bot the owner middleware is the gate, so the callback does
    # not second-guess the tapper for an owner action.
    assert confirm._tap_authorized(rt, _Q(12345), aid) is True
