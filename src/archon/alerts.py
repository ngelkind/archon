"""Operational alerts to the owner — the one path every "tell the owner" goes through.

Before this module, alerts were scattered ``bot.send_message`` calls wrapped in
``except Exception: pass``: the WhatsApp logged-out warning, the Business
connection notice, the download failure. Each bypassed the flood-control
throttle, left no record when it failed, and was dropped outright when it
fired before the control bot existed (the WhatsApp subsystem starts first).
The supervisor never alerted at all despite documenting that it did.

:func:`alert_owner` sends through the throttled notifier, mirrors to push,
audits every outcome (sent / failed / suppressed / queued), rate-limits per
``key`` so a crash loop yields one message an hour rather than one a second,
and queues alerts raised before the notifier exists so the control bot can
flush them once it is polling.
"""

from __future__ import annotations

import time
from typing import Any

#: Default minimum interval between two alerts with the same key.
DEFAULT_EVERY_S = 3600.0
_QUEUE_MAX = 20


def _state(rt: Any) -> dict[str, Any]:
    state = getattr(rt, "alert_state", None)
    if state is None:
        state = {"last": {}, "queue": []}
        rt.alert_state = state
    state.setdefault("last", {})
    state.setdefault("queue", [])
    return state


async def alert_owner(rt: Any, key: str, text: str, *, every_s: float = DEFAULT_EVERY_S,
                      force: bool = False) -> bool:
    """Tell the owner ``text``. Returns True when a message was delivered.

    ``key`` groups repeats ("subsystem:whatsapp", "llm", "gmail"): a second
    alert with the same key inside ``every_s`` is suppressed (and audited as
    such). ``force`` bypasses the rate limit for state transitions that must
    always be seen.
    """
    state = _state(rt)
    now = time.monotonic()
    last = state["last"].get(key)
    if last is not None and not force and now - last < every_s:
        rt.audit.note("owner_alert_suppressed", key=key, since_s=int(now - last))
        return False

    bot = rt.send_bot()
    if bot is None:
        queue = state["queue"]
        if len(queue) >= _QUEUE_MAX:
            queue.pop(0)
        queue.append((key, text))
        rt.audit.note("owner_alert_queued", key=key, text=text[:200])
        return False

    from .logging_.send import throttled_send

    owner_id = rt.settings.telegram_owner_id
    result = await throttled_send(rt, lambda b: b.send_message(owner_id, text))
    if result is None:
        rt.audit.note("owner_alert_failed", key=key, text=text[:200])
        return False
    state["last"][key] = now
    rt.audit.note("owner_alert_sent", key=key, text=text[:200])
    try:
        from .api import push

        await push.owner_alert(rt, source=key)
    except Exception as exc:  # noqa: BLE001 — push is best-effort by contract
        rt.audit.note("owner_alert_push_failed", key=key, error=repr(exc)[:120])
    return True


async def flush_queued(rt: Any) -> int:
    """Deliver alerts raised before a notifier existed. Called by the control
    bot once it is polling. Returns how many were sent."""
    state = _state(rt)
    queued = list(state["queue"])
    state["queue"].clear()
    sent = 0
    for key, text in queued:
        if await alert_owner(rt, key, text, force=True):
            sent += 1
    return sent
