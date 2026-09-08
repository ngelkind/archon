"""WhatsApp client (neonize async) — session owner, event source, media fetch.

The session file is the one migrated from your-other-project.
Exactly ONE process may own it; the server allows one live socket per linked
device (a second login triggers StreamReplacedEv fights). Fatal events
(LoggedOutEv / TemporaryBanEv / StreamReplacedEv) disable this subsystem and
alert the owner instead of crash-looping — pattern from wa_helper/bot.py.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from ...db import repo
from ...runtime import Runtime
from . import events as wa_events

#: How long to wait for the server's ConnectedEv before deciding the session is
#: unpaired (or the connect failed). whatsmeow normally connects in seconds.
CONNECT_TIMEOUT_S = 60.0
_CONNECT_POLL_S = 0.1
#: How often the watchdog asks whatsmeow whether the socket is still up.
WATCHDOG_S = 30.0


def _android_props():
    """Present as an Android PHONE companion (DeviceProps.PlatformType side).

    This is the ONE of three identity fields settable from Python; the other two
    (ClientPayload.UserAgent.Platform=ANDROID and WebInfo=nil) are forced in the
    goneonize source build (deploy/build_goneonize.sh android_spoof.go). All
    three together make WhatsApp's server deliver the real view-once media to
    this companion, as it does to a genuine phone. neonize merges these props
    into store.DeviceProps AFTER the Go init, so this must also say ANDROID_PHONE
    or it would override the Go side back to a default."""
    from neonize.proto.waCompanionReg import WAWebProtobufsCompanionReg_pb2 as reg

    return reg.DeviceProps(
        os="Android",
        platformType=reg.DeviceProps.ANDROID_PHONE,
        requireFullSync=False,
    )


async def _download_media_if_wanted(rt: Runtime, client: Any, event: Any, inbound) -> None:
    """Download the first media item when the chat has image recognition on."""
    if not inbound.media or inbound.media[0].kind != "image":
        return
    row = repo.chat_get(rt.db, "wa", inbound.chat_id)
    if not row or not row["image_recognition"] or not row["is_whitelisted"]:
        return
    try:
        data: bytes = await client.download_any(event.Message)
        if not data or len(data) > 8_000_000:
            return
        rt.settings.media_dir.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(c for c in inbound.msg_id if c.isalnum())[:48] or "img"
        path = rt.settings.media_dir / f"wa-{safe_id}.jpg"
        path.write_bytes(data)
        inbound.media[0].local_path = str(path)
        inbound.media[0].mime = "image/jpeg"
    except Exception as exc:  # noqa: BLE001
        rt.audit.note("wa_media_download_failed", error=repr(exc)[:200])


async def _capture_view_once(rt: Runtime, client: Any, event: Any, inbound) -> None:
    from ...logging_ import capture

    if not capture.capture_enabled(rt, "wa", inbound.chat_id, inbound.chat_kind):
        return
    kind = inbound.media[0].kind if inbound.media else "document"
    try:
        # Download the unwrapped inner message (view-once media lives inside
        # a viewOnceMessage* container that download_any won't recurse into).
        inner, _ = wa_events.unwrap_view_once(event.Message)
        data: bytes = await client.download_any(inner)
        if not data:
            return
        rt.settings.media_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in inbound.msg_id if c.isalnum())[:48] or "vo"
        ext = {"image": ".jpg", "video": ".mp4", "audio": ".ogg"}.get(kind, ".bin")
        path = rt.settings.media_dir / f"wa-vo-{safe}{ext}"
        path.write_bytes(data)
        await capture.send_capture(
            rt, platform="wa", chat_id=inbound.chat_id, chat_name=inbound.chat_name,
            sender_name=inbound.sender_name or inbound.sender_id, kind=kind,
            local_path=str(path))
    except Exception as exc:  # noqa: BLE001
        rt.audit.note("wa_capture_failed", error=repr(exc)[:200])


async def _capture_quoted_view_once(rt: Runtime, client: Any, event: Any, inbound) -> None:
    """Recover a one-time item's bytes via the quoted-reply route.

    When someone replies to a view-once, the reply carries the original in
    contextInfo.quotedMessage — and if the replier's phone still holds the
    media, the mediaKey + directPath ride along, so we (a companion) can
    download+decrypt it even though the original reached us only as a stub."""
    from ...logging_ import capture

    try:
        quoted = wa_events.find_quoted(event.Message)
        if quoted is None:
            return
        inner, is_container = wa_events.unwrap_view_once(quoted)
        kinds = wa_events.media_kinds_of(inner)
        recoverable = wa_events.has_download_keys(inner)
        # The composing phone may normalize the view-once container away and
        # store a bare imageMessage; the only surviving marker is then the
        # per-media `viewOnce` bool (which it may or may not preserve). Log
        # EVERY quoted media BEFORE gating so "route works but our marker check
        # missed it" is distinguishable from "nothing was quoted".
        vo_bool = any(
            getattr(getattr(inner, k, None), "viewOnce", False)
            for k in ("imageMessage", "videoMessage", "audioMessage")
        )
        if not kinds:
            return  # quoted text/other — nothing to capture
        rt.audit.note("wa_quoted_any", chat=inbound.chat_id, kinds=kinds,
                      recoverable=recoverable, is_container=is_container,
                      vo_bool=vo_bool, by=inbound.sender_id,
                      from_me=inbound.is_from_me)
        # Only act on quoted VIEW-ONCE items (not ordinary quoted media).
        if not (is_container or vo_bool):
            return
        if not recoverable:
            return  # WhatsApp stripped the keys — nothing to download
        if not capture.capture_enabled(rt, "wa", inbound.chat_id, inbound.chat_kind):
            return
        data: bytes = await client.download_any(inner)
        if not data:
            rt.audit.note("wa_quoted_vo_empty", chat=inbound.chat_id)
            return
        rt.settings.media_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in inbound.msg_id if c.isalnum())[:48] or "qvo"
        kind = kinds[0]
        ext = {"image": ".jpg", "video": ".mp4", "audio": ".ogg"}.get(kind, ".bin")
        path = rt.settings.media_dir / f"wa-qvo-{safe}{ext}"
        path.write_bytes(data)
        await capture.send_capture(
            rt, platform="wa", chat_id=inbound.chat_id, chat_name=inbound.chat_name,
            sender_name=inbound.sender_name or inbound.sender_id, kind=kind,
            local_path=str(path))
        rt.audit.note("wa_quoted_vo_captured", chat=inbound.chat_id, kind=kind,
                      bytes=len(data))
    except Exception as exc:  # noqa: BLE001
        rt.audit.note("wa_quoted_vo_failed", error=repr(exc)[:200])


def build_client(rt: Runtime) -> Any:
    """The real neonize async client over the owner's session file (or the
    test double registered under ``rt.factories["wa_client"]``)."""
    factory = rt.factories.get("wa_client")
    if factory is not None:
        return factory(rt)  # type: ignore[operator]
    from neonize.aioze.client import NewAClient

    session = rt.settings.wa_session_path
    try:
        return NewAClient(str(session), props=_android_props())
    except TypeError:
        return NewAClient(str(session))  # older neonize without props kwarg


@dataclass
class Session:
    """What ``run`` observes about one WhatsApp session, written by the
    handlers ``wire_events`` registers."""

    connected: asyncio.Event = field(default_factory=asyncio.Event)
    fatal: asyncio.Event = field(default_factory=asyncio.Event)
    terminal: str | None = None


def wire_events(rt: Runtime, client: Any, session: Session | asyncio.Event) -> None:
    """Register every handler on ``client``. The fatal events (logged out,
    banned, stream replaced) set ``session.fatal``; ``ConnectedEv`` sets
    ``session.connected`` — the ONLY signal that means the server accepted us.

    ``client`` only needs neonize's ``event(EvType)`` decorator plus the calls
    the handlers make, so ``archon.testing.fake_neonize.FakeAClient`` can stand
    in — the handlers used to be closures inside ``run`` and no test had ever
    fired one.
    """
    from neonize.events import (
        ConnectedEv,
        LoggedOutEv,
        MessageEv,
        PairStatusEv,
        StreamReplacedEv,
        TemporaryBanEv,
        UndecryptableMessageEv,
    )

    if isinstance(session, asyncio.Event):  # older callers passed the fatal event
        session = Session(fatal=session)
    fatal = session.fatal

    @client.event(ConnectedEv)
    async def on_connected(_c: Any, _ev: Any) -> None:
        session.connected.set()
        rt.clients["whatsapp"] = client
        rt.health["whatsapp"] = "connected"
        rt.audit.note("wa_connected")
        try:
            groups = await client.get_joined_groups()
            for g in groups:
                gjid = wa_events.jid_str(getattr(g, "JID", None))
                name = getattr(getattr(g, "GroupName", None), "Name", "") or None
                if gjid:
                    repo.chat_upsert(rt.db, "wa", gjid, name, "group")
            rt.audit.note("wa_groups_synced", count=len(groups))
        except Exception as exc:  # noqa: BLE001
            rt.audit.note("wa_group_sync_failed", error=repr(exc)[:200])

    @client.event(MessageEv)
    async def on_message(_c: Any, event: Any) -> None:
        inbound = wa_events.from_message_event(event)
        if inbound is None:
            # Log unparseable events too — helps see what WhatsApp actually
            # delivers (e.g. media types we don't yet handle).
            try:
                kinds = tuple(f.name for f, _ in event.Message.ListFields())
                rt.audit.note("wa_unparsed", kinds=list(kinds))
            except Exception:  # noqa: BLE001
                pass
            return
        # Diagnostic: surface any non-plain-text delivery (media, view-once, …).
        _kinds = inbound.raw.get("payload_kinds", [])
        if any(k not in ("conversation", "messageContextInfo", "extendedTextMessage")
               for k in _kinds):
            rt.audit.note("wa_inbound", kinds=_kinds, ephemeral=inbound.is_ephemeral_media,
                          media=[m.kind for m in inbound.media], from_me=inbound.is_from_me)
        # Owner-issued /download in a WhatsApp chat: delete + re-send as owner.
        if inbound.is_from_me and inbound.text:
            from .download_cmd import handle_wa_download, is_wa_download

            url = is_wa_download(inbound.text)
            if url:
                await handle_wa_download(rt, client, inbound, url)
                return
        # One-time (view-once) media capture — independent of the whitelist,
        # and regardless of sender (so your own test sends are captured too).
        if inbound.is_ephemeral_media:
            await _capture_view_once(rt, client, event, inbound)
        # Quoted-reply route: reply to a one-time item to recover its bytes even
        # when the original reached us only as a withheld stub.
        await _capture_quoted_view_once(rt, client, event, inbound)
        await _download_media_if_wanted(rt, client, event, inbound)
        await rt.bus.publish(inbound)

    @client.event(UndecryptableMessageEv)
    async def on_undecryptable(_c: Any, ev: Any) -> None:
        # WhatsApp delivers companions a view-once as an `unavailable` stub with
        # no ciphertext (IsUnavailable=True). whatsmeow auto-asks the phone to
        # resend; if that ever succeeds the real media arrives later as a normal
        # MessageEv (handled above). Either way, surface the STUB now so a
        # one-time send is never silent — we at least get sender + time.
        try:
            if not bool(getattr(ev, "IsUnavailable", False)):
                return  # a plain decryption failure, not a withheld one-time item
            info = wa_events.info_summary(getattr(ev, "Info", None))
            if info is None:
                rt.audit.note("wa_viewonce_stub", detail="no info")
                return
            rt.audit.note("wa_viewonce_stub", chat=info["chat_id"],
                          sender=info["sender_id"], msg_id=info["msg_id"],
                          from_me=info["is_from_me"])
            from ...logging_ import capture

            if not capture.capture_enabled(rt, "wa", info["chat_id"], info["chat_kind"]):
                rt.audit.note("wa_capture_disarmed", chat=info["chat_id"], route="stub")
                return
            row = repo.chat_get(rt.db, "wa", info["chat_id"])
            chat_name = row["name"] if row and row["name"] else None
            await capture.send_protected_notice(
                rt, platform="wa", chat_id=info["chat_id"], chat_name=chat_name,
                sender_name=info["sender_name"] or info["sender_id"])
        except Exception as exc:  # noqa: BLE001
            rt.audit.note("wa_viewonce_stub_failed", error=repr(exc)[:200])

    @client.event(PairStatusEv)
    async def on_pair(_c: Any, ev: Any) -> None:
        rt.audit.note("wa_pair_status", detail=str(ev)[:200])

    def _terminal(state: str) -> None:
        # The supervisor reports a terminal state to the owner exactly once
        # and does not restart into it; ``run`` releases the client.
        session.terminal = state
        rt.health["whatsapp"] = state
        fatal.set()

    @client.event(LoggedOutEv)
    async def on_logged_out(_c: Any, _ev: Any) -> None:
        rt.audit.note("wa_logged_out")
        _terminal("LOGGED OUT — re-pair with /wa_pair")

    @client.event(TemporaryBanEv)
    async def on_ban(_c: Any, ev: Any) -> None:
        rt.audit.note("wa_temporary_ban", detail=str(ev)[:200])
        _terminal("TEMPORARY BAN — WhatsApp disabled until it lifts")

    @client.event(StreamReplacedEv)
    async def on_replaced(_c: Any, _ev: Any) -> None:
        rt.audit.note("wa_stream_replaced")
        _terminal("STREAM REPLACED — another client is using this session; "
                  "WhatsApp disabled here to avoid a login fight")


def enabled(rt: Runtime) -> bool:
    """The off switch: WHATSAPP_ENABLED / the ``whatsapp.enabled`` setting."""
    if not getattr(rt.settings, "whatsapp_enabled", True):
        return False
    return bool(repo.setting_get(rt.db, "whatsapp.enabled", True))


async def _is_connected(client: Any) -> bool:
    from ...integrations.whatsapp import _is_connected as probe

    return await probe(client)


async def _is_logged_in(client: Any) -> bool:
    """``is_logged_in`` has the same awaitable-property trap as ``is_connected``."""
    import inspect

    value = client.is_logged_in
    if inspect.isawaitable(value):
        value = await value
    return bool(value)


async def _release(rt: Runtime, client: Any) -> None:
    """Forget the client and stop the Go side. ``disconnect()`` alone leaves the
    Go client alive holding the session file (see integrations/whatsapp.py)."""
    if rt.clients.get("whatsapp") is client:
        rt.clients.pop("whatsapp", None)
    try:
        await client.stop()
    except Exception as exc:  # noqa: BLE001 — best effort on the way out
        rt.audit.note("wa_stop_failed", error=repr(exc)[:120])


async def run(rt: Runtime, client: Any = None) -> None:
    """Supervised loop. ``client`` is injectable for tests.

    Honesty rules, each of which the previous version broke:

    * health says ``connected`` only after ``ConnectedEv`` — neonize's
      ``connect()`` returns a task the moment the Go client is being built, and
      the old code set ``connected`` right there, so /status lied for the
      whole two weeks the live session was logged out;
    * ``rt.clients["whatsapp"]`` exists only while the session is usable;
    * a session that connects but is not logged in (device removed, pairing
      never completed) is a terminal ``NOT PAIRED`` state, not a hang;
    * a watchdog re-checks whatsmeow's own ``IsConnected`` and downgrades
      health (and tells the owner) when the socket is gone;
    * a fatal event ends the session, releases the client and returns with a
      terminal state the supervisor reports once and never restarts into.
    """
    if not enabled(rt):
        rt.health["whatsapp"] = "disabled (whatsapp.enabled=false)"
        return
    session_path = rt.settings.wa_session_path
    injected = "wa_client" in rt.factories
    if client is None and not injected and not session_path.exists():
        rt.health["whatsapp"] = "no session (see deploy/MIGRATION.md step 5)"
        return  # clean return: supervisor will not restart-loop
    if client is None:
        client = build_client(rt)
    session = Session()
    wire_events(rt, client, session)

    rt.health["whatsapp"] = "connecting"
    # neonize's connect() returns the SESSION task; it completes only when the
    # connection dies. Keep it so its exception (a real connect failure) is
    # surfaced, never await it for readiness.
    session_task = await client.connect()
    try:
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        while not session.connected.is_set() and not session.fatal.is_set():
            if time.monotonic() > deadline:
                if await _is_connected(client) and not await _is_logged_in(client):
                    rt.audit.note("wa_not_paired")
                    session.terminal = "NOT PAIRED — send /wa_pair in the control chat"
                    rt.health["whatsapp"] = session.terminal
                    return
                if session_task is not None and session_task.done() and session_task.exception():
                    raise session_task.exception()  # type: ignore[misc]
                raise RuntimeError(f"no ConnectedEv within {CONNECT_TIMEOUT_S}s")
            await asyncio.sleep(_CONNECT_POLL_S)
        if session.fatal.is_set():
            return
        # Connected. Watch the socket until a fatal event ends the session.
        lost_checks = 0
        while not session.fatal.is_set():
            try:
                await asyncio.wait_for(session.fatal.wait(), timeout=WATCHDOG_S)
                break
            except TimeoutError:
                pass
            try:
                up = await _is_connected(client)
            except Exception:  # noqa: BLE001 — the bridge call itself failed
                up = False
            if up:
                if lost_checks:
                    lost_checks = 0
                    rt.health["whatsapp"] = "connected"
                    rt.audit.note("wa_socket_back")
                continue
            lost_checks += 1
            if lost_checks == 2:
                rt.health["whatsapp"] = "disconnected (socket down; whatsmeow reconnecting)"
                rt.audit.note("wa_socket_lost")
                from ... import alerts

                await alerts.alert_owner(
                    rt, "whatsapp:socket",
                    "⚠️ WhatsApp socket is down; waiting for whatsmeow to reconnect.")
    finally:
        await _release(rt, client)
        if session.terminal:
            rt.health["whatsapp"] = session.terminal
        elif session.fatal.is_set():
            rt.health["whatsapp"] = "disconnected"
        if session_task is not None and not session_task.done():
            session_task.cancel()
