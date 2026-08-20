"""Self-hosted ntfy / UnifiedPush notifier.

Push exists only to *wake the app*. Payloads carry no message content: a fixed
title, the action *type* (e.g. "wa.send" — what kind of thing is waiting, never
what it says), and the opaque ``action_id`` the app exchanges for details over
the authenticated tunnel. The correspondent, the description, and the message
body never leave the VM through this path — which holds regardless of where the
ntfy instance runs.

Wire format is ntfy's **JSON publish**: a single POST to the base URL with
``{"topic": …}`` in the body. Custom headers such as ``X-Action-Id`` are NOT a
usable channel — ntfy forwards only its own documented headers, so anything else
would be silently dropped before reaching the client. The action id therefore
travels as a ``tags`` entry (``action:42``), which ntfy does deliver.

Disabled cleanly when ``ntfy_base_url`` is empty: every entry point becomes a
cheap no-op, so an unconfigured deployment pays nothing and nothing fails.

Each device's topic lives in ``api_devices.push_endpoint`` (set at pairing);
revoked devices stop receiving pushes because they stop being listed.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..db import repo
from ..runtime import Runtime

_TIMEOUT_S = 5
_ACTION_TAG = "action"


def enabled(rt: Runtime) -> bool:
    return bool(rt.settings.ntfy_base_url.strip())


def action_id_from_tags(tags: list[str]) -> int | None:
    """Inverse of the tag encoding, for the app/tests: 'action:42' -> 42."""
    for tag in tags:
        prefix = f"{_ACTION_TAG}:"
        if tag.startswith(prefix):
            try:
                return int(tag[len(prefix):])
            except ValueError:
                return None
    return None


def _topics(rt: Runtime) -> list[str]:
    """Push topics of every live (non-revoked) device that registered one."""
    return [
        row["push_endpoint"].strip()
        for row in repo.api_device_list(rt.db)
        if row["revoked_at"] is None and (row["push_endpoint"] or "").strip()
    ]


async def notify(
    rt: Runtime, *, title: str, message: str, tags: list[str] | None = None
) -> int:
    """Fan a push out to every registered device; returns the number delivered.

    Best-effort by contract: a missing config, a device without a topic, a dead
    broker or an HTTP error all resolve to "fewer pushes", never an exception —
    the confirm gate and owner alerts must not depend on the broker being up.
    """
    if not enabled(rt):
        return 0
    topics = _topics(rt)
    if not topics:
        return 0

    base = rt.settings.ntfy_base_url.strip().rstrip("/")
    headers = {}
    token = rt.settings.ntfy_auth_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    sent = 0
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            for topic in topics:
                payload: dict[str, Any] = {
                    "topic": topic, "title": title, "message": message,
                }
                if tags:
                    payload["tags"] = tags
                try:
                    response = await client.post(base, json=payload, headers=headers)
                    if response.status_code >= 400:
                        rt.audit.note("push_rejected", status=response.status_code)
                        continue
                    sent += 1
                except Exception as exc:  # noqa: BLE001 — one bad topic, not the batch
                    rt.audit.note("push_failed", error=repr(exc)[:200])
    except Exception as exc:  # noqa: BLE001 — client construction must not escape either
        rt.audit.note("push_failed", error=repr(exc)[:200])
    return sent


async def ntfy_confirm_notifier(
    rt: Runtime, action_id: int, kind: str, description: str, payload: dict[str, Any]
) -> None:
    """confirm.Notifier: wake the app for a pending approval.

    ``description`` and ``payload`` are deliberately NOT sent — only the action
    type and the opaque id, which the app exchanges via GET /approvals/{id}.
    """
    await notify(
        rt,
        title="Approval requested",
        message=kind,
        tags=["lock", f"{_ACTION_TAG}:{action_id}"],
    )


async def owner_alert(rt: Runtime, *, source: str) -> None:
    """Wake the app for an owner alert (tg_notify_owner, subsystem trouble).

    ``source`` is a short internal label like 'tg_notify_owner' — never the
    alert text itself.
    """
    rt.events.publish("owner.alert", source=source)
    await notify(
        rt, title="Archon alert", message="Open Archon to read it.", tags=["warning"]
    )


def register(rt: Runtime) -> None:
    """Attach the push notifier to the confirm gate (idempotent). Safe to call
    even when push is unconfigured — the notifier self-disables."""
    from ..pipeline import confirm

    if ntfy_confirm_notifier not in confirm._NOTIFIERS:
        confirm.register_notifier(ntfy_confirm_notifier)
