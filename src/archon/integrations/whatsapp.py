"""Per-tenant WhatsApp — BYO account, consent-gated, deliberately isolated.

READ THIS BEFORE CHANGING ANYTHING HERE.

WhatsApp has no official API for personal accounts. This connects as a linked
companion device (whatsmeow via neonize), which **violates WhatsApp's Terms of
Service** and carries a real risk that the user's number is **permanently
banned**. That is not a hypothetical: the research found mass bans of
whatsmeow-linked accounts even at low volume. The product owner weighed this and
chose to ship it anyway.

So the design goal here is not "make it safe" — it cannot be made safe. It is:

1. **Make the risk consented.** Linking is refused outright without an explicit
   acknowledgement, and we record WHICH warning text the user accepted, so the
   consent record still means something after the wording changes.
2. **Make the blast radius one account.** Sessions are per tenant, isolated,
   encrypted at rest, and never shared. One banned number must not implicate
   another user's.
3. **Do not amplify the risk.** In particular the owner's Android-identity spoof
   is NOT applied to product tenants — see ``client_props_for``. A fleet of
   sessions sharing one forged device fingerprint is precisely the correlated
   signal that turns "one ban" into "all our users banned at once".

If you are tempted to make product WhatsApp look more like a real phone to get
better features: don't. That is the trade that gets the whole userbase banned.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..crypto import CredentialCryptoError, decrypt, encrypt
from ..db import repo
from ..db.tenancy import OWNER_TENANT_ID, TenantScope

PROVIDER = "whatsapp"

#: Bump when the warning text below changes materially. Stored per consent row
#: so an old acknowledgement still records what was actually shown.
CONSENT_VERSION = "2026-08-21.v1"

#: Shown in full before linking. The app must display this verbatim; the API
#: refuses to start a link without an explicit acknowledgement of it.
CONSENT_WARNING = (
    "Connecting WhatsApp is ADVANCED and AT YOUR OWN RISK.\n\n"
    "WhatsApp does not offer an official way for apps to use a personal "
    "account. Archon connects as a linked device, which breaks WhatsApp's "
    "Terms of Service.\n\n"
    "WhatsApp may PERMANENTLY BAN your phone number. If that happens you may "
    "lose access to your WhatsApp account, your chats, and any groups you "
    "run, and it may not be reversible. Nobody can appeal this on your "
    "behalf.\n\n"
    "Do not connect a number you cannot afford to lose. Google and Telegram "
    "work without this risk."
)


class WhatsAppLinkError(RuntimeError):
    """The link could not be started or completed."""


class PhoneRequired(WhatsAppLinkError):
    """Phone-number linking was attempted without a usable number.

    Its own type so the router can answer 400 with a specific message instead
    of a generic link failure — the app needs to know to re-prompt for the
    number rather than offer a blanket retry.
    """


class ConsentRequired(WhatsAppLinkError):
    """Linking was attempted without acknowledging the ban warning."""


def consent_notice() -> dict[str, Any]:
    """What the app must show, and the version it has to echo back."""
    return {"version": CONSENT_VERSION, "warning": CONSENT_WARNING,
            "risk": "account_ban", "reversible": False}


# --- session storage ---------------------------------------------------------

def session_dir(rt: Any, tenant_id: int) -> Path:
    """Per-tenant working directory for the linked-device session.

    One directory per tenant so a bug in path handling cannot make two tenants
    share a session file — which would mean two people's WhatsApp in one client.
    """
    base = Path(rt.settings.archon_secrets) / "wa" / "tenants" / str(int(tenant_id))
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:  # pragma: no cover — non-POSIX
        pass
    return base


def session_path(rt: Any, tenant_id: int) -> Path:
    return session_dir(rt, tenant_id) / "session.db"


def materialise_session(rt: Any, tenant_id: int) -> Path:
    """Decrypt this tenant's stored session onto disk so the client can open it.

    Returns the path. If they have no stored session yet (first pairing) the
    path simply does not exist, which is what the client expects.
    """
    path = session_path(rt, tenant_id)
    row = repo.whatsapp_link_get(TenantScope(rt.db, tenant_id))
    if row is None or not row["session_envelope"]:
        return path
    if path.exists():
        return path
    try:
        blob = decrypt(rt.settings.credential_encryption_key,
                       row["session_envelope"], tenant_id=tenant_id,
                       purpose=PROVIDER)
    except CredentialCryptoError as exc:
        rt.audit.note("wa_session_undecryptable", tenant_id=tenant_id,
                      error=str(exc)[:200])
        raise WhatsAppLinkError(
            "stored WhatsApp session could not be decrypted — re-link required"
        ) from exc
    path.write_bytes(bytes.fromhex(blob))
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover — non-POSIX
        pass
    return path


def persist_session(rt: Any, tenant_id: int, *, wipe: bool = True) -> bool:
    """Encrypt the on-disk session back into the database.

    Called when a session is evicted. ``wipe`` removes the plaintext working
    file afterwards, which is the point: at rest, the credential exists only as
    a tenant-bound ciphertext.
    """
    path = session_path(rt, tenant_id)
    if not path.exists():
        return False
    envelope = encrypt(rt.settings.credential_encryption_key,
                       path.read_bytes().hex(), tenant_id=tenant_id,
                       purpose=PROVIDER)
    repo.whatsapp_link_store_session(TenantScope(rt.db, tenant_id), envelope)
    if wipe:
        _shred(path)
    return True


def _shred(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # pragma: no cover
        pass


def wipe_session(rt: Any, tenant_id: int) -> None:
    """Remove every trace of a tenant's session, on disk and in the database."""
    _shred(session_path(rt, tenant_id))
    directory = session_dir(rt, tenant_id)
    try:
        shutil.rmtree(directory, ignore_errors=True)
    except OSError:  # pragma: no cover
        pass
    repo.whatsapp_link_store_session(TenantScope(rt.db, tenant_id), None)


# --- the deliberate difference from the owner's client ------------------------

def client_props_for(rt: Any, tenant_id: int):
    """Device properties for a tenant's client.

    The owner's client presents an Android-phone identity — a spoof that makes
    WhatsApp deliver view-once media as real images. It is NOT applied to
    product tenants, on purpose: many accounts sharing one forged device
    fingerprint is a correlated signal, and correlated signals are how a
    platform turns a single ban into a sweep of every account that looks alike.
    Product tenants run as a plain linked companion and lose view-once capture.
    """
    if tenant_id == OWNER_TENANT_ID:
        from ..platforms.whatsapp.client import _android_props

        return _android_props()
    return None


# --- linking -----------------------------------------------------------------

#: How long we treat an issued pairing code as current. OUR horizon, not a
#: value WhatsApp reports back — named that way in the API so the app prompts
#: for a fresh code rather than pretending to count down WhatsApp's own timer.
PAIR_CODE_TTL_S = 180


def normalise_phone(raw: str | None) -> str:
    """E.164 (or close enough) -> the bare digits whatsmeow wants.

    Parsed by ``phonenumbers`` (Google's libphonenumber) rather than by hand.
    The hand-rolled version accumulated one strip rule per bug report — first
    ``+``, then a leading ``00``, then a leading ``0`` — which is a losing game:
    each rule only ever caught the variant that had already reached a user. A
    real parser rejects the whole class, including the shapes nobody has thought
    of yet.

    WHY ``is_possible_number`` AND NOT ``is_valid_number`` — this is a
    deliberate choice against the stricter-looking option, because the two
    failure modes are not symmetric:

    * accepting a bogus number costs one failed pairing attempt, which WhatsApp
      itself rejects and the user simply retries;
    * rejecting a REAL number blocks that user from linking at all, with no
      workaround available to them.

    ``is_valid_number`` checks the number against libphonenumber's carrier
    allocation tables, which lag real allocations — measured here, it rejects
    ``+972501234567``, ``+972521234567`` and ``+35812345678``, all perfectly
    well-formed. A user handed a number in a newly-allocated range would be
    permanently unable to link. ``is_possible_number`` checks structure and
    length, which is what we actually need, and still rejects ``12345``.

    Note ``parse`` alone kills the entire leading-zero class: ``0501234567``,
    ``0972501234567`` and ``000972501234567`` all raise NumberParseException,
    because no E.164 country code begins with 0.
    """
    import phonenumbers

    if raw is None or not str(raw).strip():
        raise PhoneRequired(
            "linking by pairing code needs the phone number of the WhatsApp "
            "account to link, in international format (e.g. +972555000002)"
        )

    cleaned = "".join(ch for ch in str(raw) if ch.isdigit() or ch == "+")
    # A leading 00 is the international prefix written the other common way —
    # much of the world, Israel included, writes 00972… for +972…. Unambiguous,
    # since no country code starts with 0.
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not cleaned.lstrip("+"):
        raise PhoneRequired(f"{raw!r} contains no digits to dial")
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned

    try:
        parsed = phonenumbers.parse(cleaned, None)
    except phonenumbers.NumberParseException as exc:
        raise PhoneRequired(_phone_hint(raw, cleaned)) from exc

    if not phonenumbers.is_possible_number(parsed):
        raise PhoneRequired(
            f"{raw!r} is not a possible phone number — check the digits and "
            "include your country code, e.g. +972555000002."
        )
    return phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.E164).lstrip("+")


def _phone_hint(raw: Any, cleaned: str) -> str:
    """Say what to DO, not merely that the input was wrong.

    A rejection the user cannot act on is barely better than the wrong number
    it replaced — and the commonest mistake by far is typing a national number,
    which deserves its own instruction rather than a generic parse failure.
    """
    if cleaned.startswith("+0"):
        return (
            f"{raw!r} looks like a national number. Drop the leading 0 and add "
            "your country code — e.g. 0501234567 in Israel becomes "
            "+972501234567."
        )
    return (
        f"{raw!r} is not a phone number we can dial. Use international format "
        "with your country code, e.g. +972555000002."
    )


async def start_link(rt: Any, tenant_id: int, *, consent_acknowledged: bool,
                     phone: str | None = None,
                     consent_version: str | None = None) -> dict[str, Any]:
    """Begin pairing for a tenant by PHONE NUMBER. REFUSES without consent.

    Pairing code, not QR: the user links their own WhatsApp from the same phone
    that is showing the app, and a phone cannot scan a code on its own screen.
    WhatsApp's own answer is "Link with phone number instead", which is what
    this drives.

    ORDER MATTERS. The consent gate runs before the phone is even looked at, so
    a request missing both still fails as a consent refusal and still writes the
    audit row. Validating the phone first would let a malformed number mask the
    fact that someone tried to link without consenting.
    """
    if consent_acknowledged is not True:
        rt.audit.note("wa_link_refused_no_consent", tenant_id=tenant_id)
        raise ConsentRequired(
            "WhatsApp linking requires explicitly acknowledging the "
            "permanent-ban risk before it can start"
        )
    if consent_version and consent_version != CONSENT_VERSION:
        raise ConsentRequired(
            f"the warning has been updated (now {CONSENT_VERSION}); please "
            "re-read it and acknowledge again"
        )
    digits = normalise_phone(phone)

    scope = TenantScope(rt.db, tenant_id)
    wipe_session(rt, tenant_id)          # a fresh link never inherits a session
    await rt.sessions.evict(tenant_id, PROVIDER)   # nor a live client
    repo.whatsapp_link_create(scope, consent_version=CONSENT_VERSION,
                              phone_e164=f"+{digits}")
    # Recorded separately from the row so the consent survives in the audit log
    # even if the link row is later purged.
    rt.audit.note("wa_consent_acknowledged", tenant_id=tenant_id,
                  consent_version=CONSENT_VERSION, risk="permanent_account_ban")
    rt.audit.note("wa_link_started", tenant_id=tenant_id)

    try:
        code = await request_pair_code(rt, tenant_id, digits)
    except Exception as exc:  # noqa: BLE001 — every failure is a link failure
        # Includes neonize's PairPhoneError and our own WhatsAppLinkError (e.g.
        # the readiness timeout). Recording BEFORE re-raising is what makes the
        # audit log the first place to look; an earlier version re-raised
        # WhatsAppLinkError untouched, which meant a connect-timeout left no
        # wa_* row at all.
        repo.whatsapp_link_set_status(scope, status="failed",
                                      last_error=repr(exc)[:300])
        rt.audit.note("wa_pair_code_failed", tenant_id=tenant_id,
                      error=repr(exc)[:200])
        # Drop the half-built client: a retry must not reuse a session whose Go
        # side never finished connecting, or every retry fails the same way.
        try:
            await rt.sessions.evict(tenant_id, PROVIDER)
        except Exception:  # noqa: BLE001 — eviction is best-effort cleanup
            pass
        if isinstance(exc, WhatsAppLinkError):
            raise
        raise WhatsAppLinkError(f"could not request a pairing code: {exc}") from exc

    expires_at = _expiry_iso(PAIR_CODE_TTL_S)
    repo.whatsapp_link_set_pair_code(scope, code=code, expires_at=expires_at)
    rt.audit.note("wa_pair_code_issued", tenant_id=tenant_id)
    return {"status": "awaiting_code", "consent_version": CONSENT_VERSION,
            "pair_code": code, "pair_code_expires_at": expires_at}


def _expiry_iso(ttl_s: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(seconds=ttl_s)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


async def request_pair_code(rt: Any, tenant_id: int, digits: str) -> str:
    """Ask WhatsApp for an 8-character pairing code for ``digits``.

    UNVALIDATED AGAINST A LIVE ACCOUNT (task #12) — the one part of this flow a
    test cannot honestly cover, because it is entirely a conversation with
    WhatsApp's servers.

    TIMING, which is the part most likely to be wrong: whatsmeow can only issue
    a pairing code on a client that is connected and NOT yet logged in, and the
    same client must stay connected until the user finishes typing the code,
    because that is the session being authorised. So the client is taken from
    the SessionRegistry rather than built here — a throwaway would be garbage
    collected and the code would authorise nothing.
    """
    client = await rt.sessions.get(rt, tenant_id, PROVIDER)
    code = await _pair_phone(client, digits)
    if not code:
        raise WhatsAppLinkError("WhatsApp returned an empty pairing code")
    return str(code)


async def _pair_phone(client: Any, digits: str) -> str:
    """Call neonize's PairPhone, sync or async depending on the client flavour.

    ``NewAClient`` (aioze) and the sync client expose the same method name but
    not the same kind of callable, and this runs on the API's event loop — so
    the sync flavour goes to a thread. Deciding BEFORE calling matters: invoking
    it first and then checking whether the result is awaitable would already
    have blocked the loop on the ctypes call into the Go library.

    ``show_push_notification=True`` so WhatsApp also nudges the user's phone,
    which is what makes the code findable when they are looking at the app
    rather than at WhatsApp.
    """
    import asyncio
    import inspect

    fn = getattr(client, "PairPhone", None)
    if fn is None:
        raise WhatsAppLinkError(
            "this neonize build has no PairPhone; phone-number linking needs "
            "neonize >= 0.4.3 (the box has 0.4.3.post0)"
        )
    if inspect.iscoroutinefunction(fn):
        return await fn(digits, True)
    return await asyncio.to_thread(fn, digits, True)


def record_pair_status(rt: Any, tenant_id: int, *, ok: bool,
                       phone_jid: str | None = None,
                       error: str | None = None) -> None:
    """Result of a pairing attempt, reported by the client's PairStatus event."""
    scope = TenantScope(rt.db, tenant_id)
    if ok:
        repo.whatsapp_link_set_status(scope, status="paired", phone_jid=phone_jid)
        persist_session(rt, tenant_id, wipe=False)   # keep it live, store a copy
        rt.audit.note("wa_paired", tenant_id=tenant_id)
    else:
        repo.whatsapp_link_set_status(scope, status="failed",
                                      last_error=(error or "")[:300])
        rt.audit.note("wa_pair_failed", tenant_id=tenant_id,
                      error=(error or "")[:200])
    # Either way the code is spent: leaving it would keep the app prompting for
    # entry on a link that has already resolved.
    repo.whatsapp_link_clear_pair_code(scope)


def record_logged_out(rt: Any, tenant_id: int, *, banned: bool = False) -> None:
    """WhatsApp ended the session — logged out elsewhere, or banned.

    A ban is recorded distinctly because it is the outcome the user was warned
    about, and the app should say so plainly rather than showing "disconnected".
    """
    scope = TenantScope(rt.db, tenant_id)
    repo.whatsapp_link_set_status(scope, status="banned" if banned else "logged_out")
    wipe_session(rt, tenant_id)
    rt.audit.note("wa_banned" if banned else "wa_logged_out", tenant_id=tenant_id)


def status(rt: Any, store: Any) -> dict[str, Any]:
    row = repo.whatsapp_link_get(store)
    if row is None:
        return {"linked": False, "status": "not_linked",
                "consent_version": None, "phone": None,
                "pair_code": None, "pair_code_expires_at": None}
    return {
        "linked": row["status"] == "paired",
        "status": row["status"],
        # The confirmed JID once paired, else the number they asked to pair —
        # so a failed attempt can still show which number was tried.
        "phone": row["phone_jid"] or _row_get(row, "phone_e164"),
        "consent_version": row["consent_version"],
        "consent_acknowledged_at": row["consent_acknowledged_at"],
        "paired_at": row["paired_at"],
        "last_error": row["last_error"],
        # Returned verbatim, including once expired: the app compares against
        # pair_code_expires_at and offers a retry. Blanking it here instead
        # would leave the app showing 'awaiting_code' with nothing to display.
        "pair_code": _row_get(row, "pair_code"),
        "pair_code_expires_at": _row_get(row, "pair_code_expires_at"),
    }


def _row_get(row: Any, key: str) -> Any:
    """Column value, or None if this row predates the column.

    sqlite3.Row raises IndexError for an unknown key rather than returning
    None, so a DB that has not yet run migration 015 would break `status()`
    outright. Tolerating it keeps a partially-migrated box readable.
    """
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


async def unlink(rt: Any, tenant_id: int) -> bool:
    """Drop the live session and wipe the stored one."""
    await rt.sessions.evict(tenant_id, PROVIDER)
    revoked = repo.whatsapp_link_revoke(TenantScope(rt.db, tenant_id))
    wipe_session(rt, tenant_id)
    if revoked:
        rt.audit.note("wa_unlinked", tenant_id=tenant_id)
    return revoked


# --- session registry --------------------------------------------------------

def wire_events(rt: Any, tenant_id: int, client: Any) -> None:
    """Subscribe a tenant's client to the events that matter.

    The owner's equivalent is ``platforms/whatsapp/client.py``; this is the
    per-tenant twin, and the one difference that matters is that every message
    is stamped with ``tenant_id`` before it is published. ``InboundMessage``
    defaults that field to the OWNER, so forgetting the stamp would not fail —
    it would quietly file a stranger's WhatsApp messages into the owner's data.
    Hence ``dataclasses.replace`` on the way out rather than trusting a default.

    Handlers never raise into neonize: an exception crossing back into the Go
    callback takes the whole session down, so each one is wrapped.
    """
    from dataclasses import replace

    from neonize.events import (
        ConnectedEv,
        LoggedOutEv,
        MessageEv,
        PairStatusEv,
        TemporaryBanEv,
    )

    from ..platforms.whatsapp import events as wa_events

    def _note(action: str, **fields: Any) -> None:
        rt.audit.note(action, tenant_id=tenant_id, **fields)

    @client.event(ConnectedEv)
    async def _on_connected(_c: Any, _ev: Any) -> None:
        _note("wa_tenant_connected")

    @client.event(MessageEv)
    async def _on_message(_c: Any, event: Any) -> None:
        try:
            inbound = wa_events.from_message_event(event)
            if inbound is None:
                return
            await rt.bus.publish(replace(inbound, tenant_id=tenant_id))
        except Exception as exc:  # noqa: BLE001 — must not kill the session
            _note("wa_tenant_inbound_failed", error=repr(exc)[:200])

    @client.event(PairStatusEv)
    async def _on_pair(_c: Any, ev: Any) -> None:
        # PStatus: ERROR=1, SUCCESS=2 (neonize Neonize_pb2.PairStatus).
        try:
            ok = int(getattr(ev, "Status", 0)) == 2
            record_pair_status(
                rt, tenant_id, ok=ok,
                phone_jid=wa_events.jid_str(getattr(ev, "ID", None)),
                error=str(getattr(ev, "Error", "")) or None,
            )
        except Exception as exc:  # noqa: BLE001
            _note("wa_tenant_pair_status_failed", error=repr(exc)[:200])

    @client.event(LoggedOutEv)
    async def _on_logged_out(_c: Any, _ev: Any) -> None:
        try:
            record_logged_out(rt, tenant_id)
        except Exception as exc:  # noqa: BLE001
            _note("wa_tenant_logout_failed", error=repr(exc)[:200])

    @client.event(TemporaryBanEv)
    async def _on_ban(_c: Any, ev: Any) -> None:
        # The outcome the consent warning names. Recorded distinctly so the app
        # can say "banned" rather than "disconnected".
        try:
            record_logged_out(rt, tenant_id, banned=True)
            _note("wa_tenant_temporary_ban", detail=str(ev)[:200])
        except Exception as exc:  # noqa: BLE001
            _note("wa_tenant_ban_record_failed", error=repr(exc)[:200])


async def build_client(rt: Any, tenant_id: int):
    """SessionRegistry factory for ``whatsapp``: wired AND connected.

    Isolated per tenant by construction: the session file comes from that
    tenant's own directory, decrypted with that tenant's own key binding. The
    registry's per-key lock means a tenant can never end up with two competing
    clients, which WhatsApp itself treats as suspicious.

    Connecting here rather than leaving it to the caller is what makes pairing
    work: a code can only be requested on a connected, not-yet-logged-in client,
    and the SAME client has to stay up until the user finishes typing it.
    ``connect()`` returns once the socket is established — whatsmeow keeps it
    alive and reconnects internally — so this does not block the registry.

    UNVALIDATED AGAINST A LIVE ACCOUNT (task #12): everything below the neonize
    boundary. The wiring above it is covered by tests.
    """
    from neonize.aioze.client import NewAClient  # imported late: heavy, optional

    path = materialise_session(rt, tenant_id)
    props = client_props_for(rt, tenant_id)
    if props is None:
        client = NewAClient(str(path))
    else:
        try:
            client = NewAClient(str(path), props=props)
        except TypeError:                  # older neonize without props kwarg
            client = NewAClient(str(path))

    wire_events(rt, tenant_id, client)
    await client.connect()
    await await_ready(rt, tenant_id, client)
    return client


#: How long to wait for the websocket to come up before giving up on a session.
READY_TIMEOUT_S = 25.0
_READY_POLL_S = 0.25


async def await_ready(rt: Any, tenant_id: int, client: Any,
                      timeout_s: float = READY_TIMEOUT_S) -> None:
    """Block until whatsmeow reports the socket is actually up.

    WHY THIS IS NOT OPTIONAL, and why ``await connect()`` is not enough.
    neonize's ``connect()`` ends with::

        self.connect_task = self.loop.create_task(_connect_and_check())
        return self.connect_task

    so awaiting it yields a **Task**, not a connection — and that task only
    completes when the connection *dies*, because the Go call it wraps blocks
    for the lifetime of the session. Awaiting the returned task would therefore
    hang forever; awaiting the coroutine (what we did) returns while the Go-side
    client is still being constructed. That gap is what produced the live
    ``PairPhoneError('client is nil')``: PairPhone reached a client that did not
    exist yet.

    So readiness has to be observed, not assumed. ``is_connected`` is
    whatsmeow's own ``IsConnected`` through the ctypes bridge — the authoritative
    answer — rather than a sleep long enough to usually work. A sleep would pass
    on a fast box and fail on a slow one, which is the worst kind of green.

    Note this waits for CONNECTED, not LOGGED IN. Pairing by definition happens
    on a connected-but-not-logged-in client, so ``is_logged_in`` would never
    become true before the user types the code and would deadlock the flow.
    """
    import asyncio
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if client.is_connected:
                return
        except Exception:  # noqa: BLE001
            # The Go client may not exist yet, in which case the bridge call
            # itself fails. That is the state we are waiting out, not an error.
            pass
        await asyncio.sleep(_READY_POLL_S)

    rt.audit.note("wa_connect_timeout", tenant_id=tenant_id,
                  waited_s=round(timeout_s, 1))
    raise WhatsAppLinkError(
        f"WhatsApp did not connect within {timeout_s:.0f}s — the socket never "
        "came up, so no pairing code could be requested. Try again."
    )


async def send_message(rt: Any, tenant_id: int, chat_jid: str,
                       text: str) -> dict[str, Any]:
    """Send as this tenant's linked WhatsApp, under the outbound budgets.

    Paced through the same :mod:`archon.pacing` budgets as the Telegram userbot,
    though the risk shape differs: WhatsApp bans the ACCOUNT rather than a shared
    app id, so the global budget matters less here than the per-peer and
    per-tenant ones. Reusing the pacer keeps one place to reason about outbound
    volume instead of two that drift.

    UNVALIDATED AGAINST A LIVE ACCOUNT (task #12) — the neonize call below.
    """
    from ..pacing import pacer_for
    from ..platforms.whatsapp.events import jid_str

    pacer = pacer_for(rt)
    pacer.acquire(tenant_id, str(chat_jid))
    await pacer.gap()

    client = await rt.sessions.get(rt, tenant_id, PROVIDER)
    to = _build_jid(chat_jid)
    sent = await client.send_message(to, text)
    # No message text in the audit log: shared table, third-party content.
    rt.audit.note("wa_tenant_sent", tenant_id=tenant_id,
                  chat=jid_str(to) or str(chat_jid), chars=len(text))
    return {"ok": True, "message_id": getattr(sent, "ID", None)}


def _build_jid(raw: str):
    """String JID -> the neonize JID object its send API expects."""
    from neonize.utils import build_jid

    bare = str(raw).split("@", 1)[0]
    server = str(raw).split("@", 1)[1] if "@" in str(raw) else "s.whatsapp.net"
    return build_jid(bare, server)


def _on_evict(rt: Any, tenant_id: int) -> None:
    """Before a session is dropped: encrypt it back and remove the plaintext."""
    try:
        persist_session(rt, tenant_id, wipe=True)
    except Exception as exc:  # noqa: BLE001 — eviction must still proceed
        rt.audit.note("wa_session_persist_failed", tenant_id=tenant_id,
                      error=repr(exc)[:200])


def register(rt: Any) -> None:
    rt.sessions.register_factory(PROVIDER, build_client, on_evict=_on_evict)
