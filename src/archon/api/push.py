"""Self-hosted ntfy / UnifiedPush notifier.

Push exists only to *wake the app*. Payloads are *content-free by design*: a
fixed title, a short generic body, and opaque identifiers. No message text, no
correspondent name, no approval description ever leaves the VM through this
path — on tap, the app fetches the details over the authenticated tunnel. That
holds even though the ntfy instance is self-hosted, so the guarantee does not
depend on where the broker runs.

Disabled cleanly when ``ntfy_base_url`` is empty: every entry point becomes a
cheap no-op, so an unconfigured deployment pays nothing and nothing fails.

Each device's topic lives in ``api_devices.push_endpoint`` (set at pairing);
revoked devices stop receiving pushes because they stop being listed.
"""

from __future__ import annotations

import httpx

from ..db import repo
from ..runtime import Runtime

_TIMEOUT_S = 10


def enabled(rt: Runtime) -> bool:
    return bool(rt.settings.ntfy_base_url.strip())


def _topics(rt: Runtime) -> list[str]:
    """Push topics of every live (non-revoked) device that registered one."""
    return [
        row["push_endpoint"]
        for row in repo.api_device_list(rt.db)
        if row["revoked_at"] is None and (row["push_endpoint"] or "").strip()
    ]


async def _post(rt: Runtime, topic: str, *, title: str, body: str,
                headers: dict[str, str]) -> None:
    base = rt.settings.ntfy_base_url.strip().rstrip("/")
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        await client.post(
            f"{base}/{topic}",
            content=body.encode("utf-8"),
            headers={"Title": title, **headers},
        )


async def notify(rt: Runtime, *, title: str, body: str,
                 headers: dict[str, str] | None = None) -> int:
    """Fan a content-free push out to every registered device. Returns the
    number of successful posts. Never raises: a dead broker must not break the
    confirm gate or an owner alert."""
    if not enabled(rt):
        return 0
    sent = 0
    for topic in _topics(rt):
        try:
            await _post(rt, topic, title=title, body=body, headers=headers or {})
            sent += 1
        except Exception as exc:  # noqa: BLE001 — push is best-effort by design
            rt.audit.note("push_failed", error=repr(exc)[:200])
    return sent


async def approval_notifier(
    rt: Runtime, action_id: int, kind: str, description: str, payload: dict
) -> None:
    """confirm.Notifier: wake the app for a pending approval.

    ``kind``/``description`` are deliberately NOT sent — only the opaque
    action_id, which the app exchanges for details via GET /approvals.
    """
    await notify(
        rt,
        title="Approval requested",
        body="Open Archon to review.",
        headers={"X-Action-Id": str(action_id), "Tags": "lock"},
    )


async def owner_alert(rt: Runtime, *, source: str) -> None:
    """Wake the app for an owner alert (tg_notify_owner, subsystem trouble).

    ``source`` is a short internal label like 'tg_notify_owner' — never the
    alert text itself.
    """
    rt.events.publish("owner.alert", source=source)
    await notify(
        rt,
        title="Archon alert",
        body="Open Archon to read it.",
        headers={"Tags": "warning"},
    )


def register(rt: Runtime) -> None:
    """Attach the push notifier to the confirm gate (idempotent)."""
    from ..pipeline import confirm

    if approval_notifier not in confirm._NOTIFIERS:
        confirm.register_notifier(approval_notifier)
