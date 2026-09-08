"""In-product WhatsApp (re)pairing, driven from the control chat.

Before this, re-pairing meant an operator on the VM stopping the service,
running ``scripts/wa_pair.py`` by hand and relaying PNGs of rotating QR codes
over SSH — which is why the live session stayed logged out for two weeks.

:func:`pair` stops the WhatsApp subsystem, opens a pairing client over the
same session file, hands every QR code (they rotate ~20 s) to ``on_qr`` (the
control bot posts it as a photo), waits for the server to accept the device,
lets whatsmeow settle, stops the pairing client so the session file is
released, and restarts the subsystem.

Pairing is by QR, not phone code: the owner's session presents an Android
phone identity (see ``client._android_props``) and WhatsApp rejects pairing
codes for that identity.

# TODO(TOS-REVIEW): WhatsApp — pairs a forged Android-phone identity over an unofficial client — review before launch
"""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ...runtime import Runtime

PAIR_TIMEOUT_S = 180.0
#: whatsmeow finishes the handshake a moment after ConnectedEv; stopping the
#: pairing client too early produced a transient LoggedOut on the next connect.
SETTLE_S = 3.0


@dataclass(slots=True)
class PairOutcome:
    status: str  # paired | already_paired | timeout | logged_out | busy | error
    detail: str
    qr_count: int = 0


def render_png(qr_data: bytes) -> bytes:
    import segno

    buf = io.BytesIO()
    segno.make(qr_data.decode() if isinstance(qr_data, bytes) else qr_data,
               error="m").save(buf, kind="png", scale=8, border=2)
    return buf.getvalue()


def in_progress(rt: Runtime) -> bool:
    return bool(rt.factories.get("_wa_pairing_active"))


async def _stop_subsystem(rt: Runtime) -> None:
    task = rt.tasks.get("whatsapp")
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — we are replacing it
            pass
    rt.clients.pop("whatsapp", None)
    rt.health["whatsapp"] = "pairing"


async def pair(rt: Runtime, *, on_qr: Callable[[bytes, int], Awaitable[None]],
               timeout_s: float | None = None, restart: bool = True) -> PairOutcome:
    """Run one pairing attempt end to end. Safe to call from a bot handler."""
    from neonize.events import ConnectedEv, LoggedOutEv, PairStatusEv

    from ... import app as app_module
    from . import client as wa_client

    if timeout_s is None:
        timeout_s = PAIR_TIMEOUT_S
    if in_progress(rt):
        return PairOutcome("busy", "a pairing attempt is already running")
    rt.factories["_wa_pairing_active"] = True
    rt.audit.note("wa_pair_started")
    started = time.monotonic()
    await _stop_subsystem(rt)

    client = wa_client.build_client(rt)
    paired = asyncio.Event()
    failed: list[str] = []
    counter = {"qr": 0}

    async def on_qr_data(_c: Any, data: bytes) -> None:
        counter["qr"] += 1
        rt.audit.note("wa_pair_qr", n=counter["qr"])
        try:
            await on_qr(render_png(data), counter["qr"])
        except Exception as exc:  # noqa: BLE001 — the owner just misses one code
            rt.audit.note("wa_pair_qr_post_failed", error=repr(exc)[:150])

    client.event.qr(on_qr_data)

    @client.event(PairStatusEv)
    async def on_pair_status(_c: Any, ev: Any) -> None:
        rt.audit.note("wa_pair_status", detail=str(ev)[:200])

    @client.event(ConnectedEv)
    async def on_connected(_c: Any, _ev: Any) -> None:
        paired.set()

    @client.event(LoggedOutEv)
    async def on_logged_out(_c: Any, _ev: Any) -> None:
        failed.append("logged_out")
        paired.set()

    outcome: PairOutcome
    try:
        await client.connect()
        try:
            await asyncio.wait_for(paired.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            outcome = PairOutcome("timeout", f"no scan within {int(timeout_s)}s "
                                  f"({counter['qr']} codes shown)", counter["qr"])
        else:
            if failed:
                outcome = PairOutcome("logged_out", "WhatsApp rejected the session; try again",
                                      counter["qr"])
            else:
                await asyncio.sleep(SETTLE_S)
                status = "paired" if counter["qr"] else "already_paired"
                outcome = PairOutcome(status, f"linked after {int(time.monotonic() - started)}s",
                                      counter["qr"])
    except Exception as exc:  # noqa: BLE001
        outcome = PairOutcome("error", f"{type(exc).__name__}: {str(exc)[:200]}", counter["qr"])
    finally:
        try:
            await client.stop()  # release session.db before the subsystem reopens it
        except Exception as exc:  # noqa: BLE001
            rt.audit.note("wa_pair_stop_failed", error=repr(exc)[:120])
        rt.factories.pop("_wa_pairing_active", None)

    rt.audit.note("wa_pair_done", status=outcome.status, qr_count=outcome.qr_count,
                  detail=outcome.detail)
    if restart:
        factory = rt.subsystems.get("whatsapp") or (lambda: wa_client.run(rt))
        app_module.start_subsystem(rt, "whatsapp", factory)
    return outcome
