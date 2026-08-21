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

def start_link(rt: Any, tenant_id: int, *, consent_acknowledged: bool,
               consent_version: str | None = None) -> dict[str, Any]:
    """Begin pairing for a tenant. REFUSES without explicit consent.

    This is the checkpoint the whole feature hangs on: no consent, no link, no
    session, no exception for convenience.
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

    scope = TenantScope(rt.db, tenant_id)
    wipe_session(rt, tenant_id)          # a fresh link never inherits a session
    repo.whatsapp_link_create(scope, consent_version=CONSENT_VERSION)
    # Recorded separately from the row so the consent survives in the audit log
    # even if the link row is later purged.
    rt.audit.note("wa_consent_acknowledged", tenant_id=tenant_id,
                  consent_version=CONSENT_VERSION, risk="permanent_account_ban")
    rt.audit.note("wa_link_started", tenant_id=tenant_id)
    return {"status": "pending", "consent_version": CONSENT_VERSION,
            "next": "scan_qr"}


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
                "consent_version": None, "phone": None}
    return {
        "linked": row["status"] == "paired",
        "status": row["status"],
        "phone": row["phone_jid"],
        "consent_version": row["consent_version"],
        "consent_acknowledged_at": row["consent_acknowledged_at"],
        "paired_at": row["paired_at"],
        "last_error": row["last_error"],
    }


async def unlink(rt: Any, tenant_id: int) -> bool:
    """Drop the live session and wipe the stored one."""
    await rt.sessions.evict(tenant_id, PROVIDER)
    revoked = repo.whatsapp_link_revoke(TenantScope(rt.db, tenant_id))
    wipe_session(rt, tenant_id)
    if revoked:
        rt.audit.note("wa_unlinked", tenant_id=tenant_id)
    return revoked


# --- session registry --------------------------------------------------------

def build_client(rt: Any, tenant_id: int):
    """SessionRegistry factory for ``whatsapp``.

    Isolated per tenant by construction: the session file comes from that
    tenant's own directory, decrypted with that tenant's own key binding. The
    registry's per-key lock means a tenant can never end up with two competing
    clients, which WhatsApp itself treats as suspicious.
    """
    from neonize.aioze.client import NewAClient  # imported late: heavy, optional

    path = materialise_session(rt, tenant_id)
    props = client_props_for(rt, tenant_id)
    if props is None:
        return NewAClient(str(path))
    try:
        return NewAClient(str(path), props=props)
    except TypeError:                      # older neonize without props kwarg
        return NewAClient(str(path))


def _on_evict(rt: Any, tenant_id: int) -> None:
    """Before a session is dropped: encrypt it back and remove the plaintext."""
    try:
        persist_session(rt, tenant_id, wipe=True)
    except Exception as exc:  # noqa: BLE001 — eviction must still proceed
        rt.audit.note("wa_session_persist_failed", tenant_id=tenant_id,
                      error=repr(exc)[:200])


def register(rt: Any) -> None:
    rt.sessions.register_factory(PROVIDER, build_client, on_evict=_on_evict)
