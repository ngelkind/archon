"""Outbound send budgets for per-tenant userbots.

The risk being mitigated is stated in ``integrations/telegram_userbot.py``:
every tenant logs in under ONE app-level ``TELEGRAM_API_ID``, Telegram flags at
the api_id level, and all sessions egress from one datacentre IP. If that id is
flagged, EVERY tenant breaks at once — a correlated failure, not an isolated
one.

The test that carries this file is
``test_the_global_budget_stops_what_per_tenant_budgets_cannot``: it puts many
tenants through the pacer, each one perfectly within its own limits, and shows
the api_id would still be over the line without the global budget. A per-tenant
limiter is the intuitive design and it does not mitigate this risk at all.

Time is injected everywhere (``now=``) rather than slept, so the hour-scale
windows are exercised in microseconds.
"""

from __future__ import annotations

import asyncio

import pytest

from archon.config import Settings
from archon.pacing import GLOBAL_KEY, OutboundPacer, PaceRefused


def _settings(**over) -> Settings:
    base = dict(
        telegram_bot_token="x", telegram_owner_id=1,
        userbot_sends_per_min_per_tenant=5,
        userbot_sends_per_hour_per_tenant=20,
        userbot_sends_per_min_global=12,
        userbot_sends_per_hour_global=60,
        userbot_sends_per_min_per_peer=3,
        userbot_new_peers_per_hour_per_tenant=4,
        userbot_send_gap_s_min=0.0,
        userbot_send_gap_s_max=0.0,
        _env_file=None,
    )
    base.update(over)
    return Settings(**base)


def _pacer(**over) -> OutboundPacer:
    return OutboundPacer(_settings(**over))


# --- the ordinary case must not be punished ----------------------------------

def test_a_normal_conversation_is_never_refused():
    """The failure mode that would make this feature worse than useless.

    A userbot answering one person needs very little throughput; if paced
    replies get refused, the product is broken in the common case while
    protecting against the rare one.
    """
    pacer = _pacer()
    for i in range(20):
        # One peer, one message every 30s — a long, ordinary chat.
        pacer.acquire(1, "peer-a", now=i * 30.0)


def test_many_messages_to_one_peer_do_not_trip_the_bulk_guard():
    """Breadth, not volume: the bulk guard counts DISTINCT chats.

    Fifty messages to one friend is a conversation; ten to ten strangers is a
    broadcast. Only the second is the pattern Telegram calls spam.
    """
    pacer = _pacer(userbot_sends_per_min_per_peer=0, userbot_sends_per_min_per_tenant=0)
    for i in range(15):
        pacer.acquire(1, "peer-a", now=i * 120.0)
    assert pacer.new_peers.count("1", now=15 * 120.0) == 1


# --- per-tenant budgets ------------------------------------------------------

def test_per_peer_cooldown_refuses_hammering_one_chat():
    pacer = _pacer()
    for _ in range(3):
        pacer.acquire(1, "peer-a", now=0.0)
    with pytest.raises(PaceRefused) as exc:
        pacer.acquire(1, "peer-a", now=0.0)
    assert "peer-a" in exc.value.reason
    assert exc.value.retry_after_s > 0


def test_per_tenant_minute_budget_refuses():
    # Bulk guard off so this isolates the minute budget: with both on, four
    # distinct peers trip the breadth guard before the fifth send is reached.
    pacer = _pacer(userbot_sends_per_min_per_peer=0,
                   userbot_new_peers_per_hour_per_tenant=0)
    for i in range(5):
        pacer.acquire(1, f"peer-{i}", now=0.0)
    with pytest.raises(PaceRefused) as exc:
        pacer.acquire(1, "peer-x", now=0.0)
    assert "per-minute" in exc.value.reason


def test_per_tenant_hour_budget_refuses():
    """Spread out enough to clear every minute window, still capped hourly."""
    pacer = _pacer(userbot_sends_per_min_per_peer=0,
                   userbot_new_peers_per_hour_per_tenant=0)
    for i in range(20):
        pacer.acquire(1, "peer-a", now=i * 90.0)     # 30 min, never 5/min
    with pytest.raises(PaceRefused) as exc:
        pacer.acquire(1, "peer-a", now=20 * 90.0)
    assert "hourly" in exc.value.reason


def test_the_bulk_send_guard_refuses_many_new_chats():
    pacer = _pacer(userbot_sends_per_min_per_tenant=0)
    for i in range(4):
        pacer.acquire(1, f"stranger-{i}", now=i * 60.0)
    with pytest.raises(PaceRefused) as exc:
        pacer.acquire(1, "stranger-5", now=5 * 60.0)
    assert "bulk-send guard" in exc.value.reason
    # ...but the chats already spoken to remain reachable.
    pacer.acquire(1, "stranger-0", now=5 * 60.0)


# --- the shared api_id: the reason this module exists ------------------------

def test_the_global_budget_stops_what_per_tenant_budgets_cannot():
    """THE test. Many tenants, each individually compliant, one shared api_id.

    Every tenant here sends 4 messages in a minute against a per-tenant limit of
    5 — nobody is over their own budget, and a per-tenant-only limiter would
    allow all 40. Telegram, which sees only the api_id, would receive 40 in a
    minute. The global budget of 12 is what stops it.
    """
    pacer = _pacer(userbot_sends_per_min_per_peer=0)
    allowed = refused = 0
    for tenant in range(1, 11):            # 10 tenants
        for _ in range(4):                 # 4 each — within the per-tenant 5
            try:
                pacer.acquire(tenant, f"peer-of-{tenant}", now=0.0)
                allowed += 1
            except PaceRefused as exc:
                refused += 1
                assert "shared Telegram app" in exc.reason

    assert allowed == 12, f"the api_id saw {allowed} sends in a minute"
    assert refused == 28
    assert len(pacer.per_min._hits[GLOBAL_KEY]) == 12
    # And no individual tenant ever reached its OWN limit of 5 — which is
    # exactly why a per-tenant limiter would not have caught this.
    assert max(len(pacer.per_min._hits[str(t)]) for t in range(1, 11)) < 5


def test_removing_the_global_budget_restores_the_correlated_risk():
    """Mutation guard: prove the global budget is load-bearing.

    With it disabled (0), the same traffic that was capped at 12 above sails
    through at 40 — which is the original risk, restored. If someone ever
    'simplifies' the global dimension away, the test above goes red and this one
    explains why it mattered.
    """
    pacer = _pacer(userbot_sends_per_min_per_peer=0,
                   userbot_sends_per_min_global=0,
                   userbot_sends_per_hour_global=0)
    allowed = 0
    for tenant in range(1, 11):
        for _ in range(4):
            pacer.acquire(tenant, f"peer-of-{tenant}", now=0.0)
            allowed += 1
    assert allowed == 40


def test_one_greedy_tenant_is_blamed_for_its_own_overrun():
    """Ordering matters: per-tenant checks run before the global ones.

    Otherwise a single tenant burning the shared budget would be told the
    platform is busy, and so would everyone else — hiding which account is
    actually responsible.
    """
    pacer = _pacer(userbot_sends_per_min_per_peer=0,
                   userbot_new_peers_per_hour_per_tenant=0)
    for i in range(5):
        pacer.acquire(1, f"p{i}", now=0.0)
    with pytest.raises(PaceRefused) as exc:
        pacer.acquire(1, "p9", now=0.0)
    assert "this account's" in exc.value.reason      # not "shared Telegram app"


# --- accounting correctness --------------------------------------------------

def test_check_is_side_effect_free():
    """``check`` must not charge, or consulting a budget would spend it."""
    pacer = _pacer()
    for _ in range(50):
        assert pacer.check(1, "peer-a", now=0.0).allowed
    pacer.acquire(1, "peer-a", now=0.0)
    assert len(pacer.per_min._hits["1"]) == 1


def test_a_refused_send_does_not_consume_the_tenants_quota():
    """The bug this design avoids.

    If budgets were charged as they were checked, a send refused by the GLOBAL
    limit would still have billed the tenant — so a tenant could be locked out
    of a quota they never actually used, and repeated refusals would compound
    it.
    """
    pacer = _pacer(userbot_sends_per_min_per_peer=0, userbot_sends_per_min_global=2)
    pacer.acquire(1, "a", now=0.0)
    pacer.acquire(2, "b", now=0.0)                   # global budget now spent
    before = len(pacer.per_min._hits["3"])
    for _ in range(5):
        with pytest.raises(PaceRefused):
            pacer.acquire(3, "c", now=0.0)
    assert len(pacer.per_min._hits["3"]) == before == 0
    # Tenant 3 kept its full budget and can send once the window rolls over.
    pacer.acquire(3, "c", now=61.0)


def test_budgets_recover_once_the_window_rolls_over():
    pacer = _pacer()
    for _ in range(3):
        pacer.acquire(1, "peer-a", now=0.0)
    with pytest.raises(PaceRefused):
        pacer.acquire(1, "peer-a", now=10.0)
    pacer.acquire(1, "peer-a", now=61.0)             # minute window cleared


def test_zero_disables_a_dimension():
    """Documented escape hatch — an operator can turn any single guard off."""
    pacer = _pacer(userbot_sends_per_min_per_peer=0)
    for _ in range(5):
        pacer.acquire(1, "peer-a", now=0.0)          # per-peer no longer applies


# --- humanisation ------------------------------------------------------------

def test_the_gap_never_refuses_and_honours_zero():
    """The gap is texture, not a guard; at 0 it must not sleep at all."""
    pacer = _pacer()
    asyncio.run(pacer.gap())

    slept: list[float] = []

    async def _run():
        import archon.pacing as pacing_mod
        real = pacing_mod.asyncio.sleep

        async def fake(d):
            slept.append(d)
            await real(0)

        pacing_mod.asyncio.sleep = fake
        try:
            p = _pacer(userbot_send_gap_s_min=2.0, userbot_send_gap_s_max=6.0)
            await p.gap()
        finally:
            pacing_mod.asyncio.sleep = real

    asyncio.run(_run())
    assert len(slept) == 1 and 2.0 <= slept[0] <= 6.0


# --- the send path is actually gated -----------------------------------------

def test_the_userbot_send_path_is_pace_gated(tmp_path):
    """The pacer only helps if the send path consults it.

    A correct pacer that nothing calls is the most plausible way this mitigation
    fails, so this asserts the wiring rather than the budget: the transport is
    faked, the gating is real. (The Telethon call itself is unvalidated until a
    throwaway account exists — task #12.)
    """
    from archon.integrations import telegram_userbot as ub

    from test_api import make_rt

    rt = make_rt(tmp_path)
    rt.settings.userbot_sends_per_min_per_peer = 2
    rt.settings.userbot_send_gap_s_min = 0.0
    rt.settings.userbot_send_gap_s_max = 0.0

    sent: list[tuple[str, str]] = []

    class _FakeClient:
        async def send_message(self, peer, text):
            sent.append((peer, text))
            return type("Msg", (), {"id": len(sent)})()

    async def _get(_rt, _tenant_id, _kind):
        return _FakeClient()

    rt.sessions.get = _get

    async def _run():
        for _ in range(2):
            await ub.send_message(rt, 7, "peer-a", "hi")
        with pytest.raises(PaceRefused):
            await ub.send_message(rt, 7, "peer-a", "hi")

    asyncio.run(_run())
    assert len(sent) == 2, "a refused send must not reach the transport"


def test_the_pacer_is_process_wide_not_per_tenant():
    """A per-tenant pacer could not enforce the global budget — the whole point.

    Guards against a future refactor that gives each tenant its own instance,
    which would look tidier and silently remove the mitigation.
    """
    from archon.pacing import pacer_for

    class _RT:
        settings = _settings()
        pacer = None

    rt = _RT()
    assert pacer_for(rt) is pacer_for(rt)
