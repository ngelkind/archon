"""Outbound pacing for per-tenant userbot sends.

``integrations/telegram_userbot.py`` says mitigations for the shared-``api_id``
risk "live outside this module (conservative pacing, no bulk sends, per-tenant
limits)". This is that module.

THE POINT THAT IS EASY TO GET WRONG. Every tenant's userbot logs in under ONE
app-level ``TELEGRAM_API_ID``, and Telegram rate-limits and flags at the api_id
level (``API_ID_PUBLISHED_FLOOD`` exists precisely for over-shared ids). So a
purely PER-TENANT limit does not protect the thing actually at risk: fifty
tenants each behaving perfectly within their own budget still present fifty
times that volume to one api_id, from one datacentre IP. The **global** budget
here is the mitigation that matters; the per-tenant budget only stops one
account from eating everyone else's share of it. Both exist, and removing the
global one would quietly restore the original correlated-failure risk.

What Telegram documents as ban-worthy (core.telegram.org/api/obtaining_api_id)
is "flooding, spamming, faking subscriber and view counters" — so the guards are
shaped after that behaviour rather than after raw throughput:

* **per-peer** — repeatedly hammering one chat.
* **per-tenant, per-minute and per-hour** — one account's total volume.
* **global, per-minute and per-hour** — what the api_id actually presents.
* **distinct new peers per hour** — the signature of a bulk/broadcast send,
  which is the pattern that reads as spam even at a low message rate. Ten
  messages to ten strangers is far more dangerous than fifty to one friend.

Two different failure modes on purpose:

* a small randomised **gap** is always slept before a send, so ordinary
  conversation is paced without ever being refused;
* a budget overrun **refuses** rather than queues. Queuing would hide the
  problem and then emit the backlog as exactly the burst we are trying to avoid,
  and a caller that is told "refused, retry in 40s" can say so, whereas one
  silently stalled for ten minutes cannot.

In-process state, one box, one process — the same scope as ``api/ratelimit.py``,
and it stops being sufficient at the same moment (multiple replicas), because
each replica would then hold its own idea of the shared api_id's budget.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from .util.window import SlidingWindow

_MINUTE = 60.0
_HOUR = 3600.0

#: Key used for the process-wide budget. Not a tenant id — tenant ids are ints,
#: so this can never collide with one.
GLOBAL_KEY = "*global*"


class PaceRefused(RuntimeError):
    """A send was refused to keep the shared api_id out of trouble."""

    def __init__(self, reason: str, retry_after_s: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_s = retry_after_s


@dataclass(slots=True)
class Decision:
    allowed: bool
    reason: str = ""
    retry_after_s: int = 0


class DistinctWindow:
    """How many DISTINCT values a key has seen in the trailing window.

    Separate from :class:`SlidingWindow` because the bulk-send signature is
    about breadth, not volume: fifty messages to one chat is a conversation,
    ten messages to ten strangers is a broadcast.
    """

    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self._seen: dict[str, deque[tuple[float, str]]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[tuple[float, str]]:
        bucket = self._seen[key]
        cutoff = now - self.window_s
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()
        return bucket

    def count(self, key: str, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        return len({v for _, v in self._prune(key, now)})

    def contains(self, key: str, value: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        return any(v == value for _, v in self._prune(key, now))

    def add(self, key: str, value: str, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._prune(key, now)
        self._seen[key].append((now, value))

    def retry_after(self, key: str, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        bucket = self._prune(key, now)
        if not bucket:
            return 1
        return max(1, int(self.window_s - (now - bucket[0][0])) + 1)


class OutboundPacer:
    """Budget keeper for per-tenant userbot sends.

    ``check`` is side-effect free so several budgets can be consulted before any
    is charged — charging a tenant for a send that the global budget then
    refuses would bill them for a message that never went out, and enough
    refusals would lock them out of their own quota.
    """

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.per_min = SlidingWindow(_MINUTE)
        self.per_hour = SlidingWindow(_HOUR)
        self.per_peer = SlidingWindow(_MINUTE)
        self.new_peers = DistinctWindow(_HOUR)

    # --- budgets -------------------------------------------------------------

    def _limits(self) -> dict[str, int]:
        s = self.settings
        return {
            "tenant_min": s.userbot_sends_per_min_per_tenant,
            "tenant_hour": s.userbot_sends_per_hour_per_tenant,
            "global_min": s.userbot_sends_per_min_global,
            "global_hour": s.userbot_sends_per_hour_global,
            "peer_min": s.userbot_sends_per_min_per_peer,
            "peers_hour": s.userbot_new_peers_per_hour_per_tenant,
        }

    def check(self, tenant_id: int, peer: str, now: float | None = None) -> Decision:
        """Whether this send may proceed. Records nothing."""
        now = now if now is not None else time.monotonic()
        lim = self._limits()
        t_key, p_key = str(tenant_id), f"{tenant_id}:{peer}"

        if not self.per_peer.would_allow(p_key, lim["peer_min"], now):
            return Decision(False, f"too many messages to {peer} in the last minute",
                            self.per_peer.retry_after(p_key, now))
        if not self.per_min.would_allow(t_key, lim["tenant_min"], now):
            return Decision(False, "this account's per-minute send budget is spent",
                            self.per_min.retry_after(t_key, now))
        if not self.per_hour.would_allow(t_key, lim["tenant_hour"], now):
            return Decision(False, "this account's hourly send budget is spent",
                            self.per_hour.retry_after(t_key, now))

        # The shared-api_id guards. Deliberately checked AFTER the per-tenant
        # ones so a single greedy tenant is blamed for its own overrun rather
        # than reported as a platform-wide limit.
        if not self.per_min.would_allow(GLOBAL_KEY, lim["global_min"], now):
            return Decision(False, "shared Telegram app is at its per-minute limit",
                            self.per_min.retry_after(GLOBAL_KEY, now))
        if not self.per_hour.would_allow(GLOBAL_KEY, lim["global_hour"], now):
            return Decision(False, "shared Telegram app is at its hourly limit",
                            self.per_hour.retry_after(GLOBAL_KEY, now))

        # Breadth: only NEW peers count, so a long conversation is unaffected
        # while contacting many strangers in an hour is not.
        if not self.new_peers.contains(t_key, peer, now):
            limit = lim["peers_hour"]
            if limit > 0 and self.new_peers.count(t_key, now) >= limit:
                return Decision(
                    False,
                    "this account has messaged too many new chats this hour "
                    "(bulk-send guard)",
                    self.new_peers.retry_after(t_key, now),
                )
        return Decision(True)

    def charge(self, tenant_id: int, peer: str, now: float | None = None) -> None:
        """Record a send against every budget. Call only after ``check`` passes."""
        now = now if now is not None else time.monotonic()
        lim = self._limits()
        t_key, p_key = str(tenant_id), f"{tenant_id}:{peer}"
        self.per_peer.hit(p_key, lim["peer_min"], now)
        self.per_min.hit(t_key, lim["tenant_min"], now)
        self.per_hour.hit(t_key, lim["tenant_hour"], now)
        self.per_min.hit(GLOBAL_KEY, lim["global_min"], now)
        self.per_hour.hit(GLOBAL_KEY, lim["global_hour"], now)
        self.new_peers.add(t_key, peer, now)

    def acquire(self, tenant_id: int, peer: str, now: float | None = None) -> None:
        """Check-and-charge. Raises :class:`PaceRefused` when over budget."""
        decision = self.check(tenant_id, peer, now)
        if not decision.allowed:
            raise PaceRefused(decision.reason, decision.retry_after_s)
        self.charge(tenant_id, peer, now)

    # --- humanisation --------------------------------------------------------

    async def gap(self) -> None:
        """Sleep a small randomised interval before a send.

        Always applied, never refuses: this is what makes ordinary replies look
        like typing rather than automation. The budgets above are the ban
        guard; this is the texture.
        """
        lo = float(self.settings.userbot_send_gap_s_min)
        hi = float(self.settings.userbot_send_gap_s_max)
        if hi <= 0:
            return
        await asyncio.sleep(random.uniform(min(lo, hi), max(lo, hi)))

    def stats(self) -> dict[str, Any]:
        return {
            "global_last_minute": len(self.per_min._hits.get(GLOBAL_KEY, ())),
            "global_last_hour": len(self.per_hour._hits.get(GLOBAL_KEY, ())),
            "limits": self._limits(),
        }


def pacer_for(rt: Any) -> OutboundPacer:
    """The process-wide pacer, created on first use.

    One instance per process on purpose — a per-tenant pacer could not enforce
    the global budget, which is the whole reason this exists.
    """
    if rt.pacer is None:
        rt.pacer = OutboundPacer(rt.settings)
    return rt.pacer
