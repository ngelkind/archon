"""Per-tenant Telegram USERBOT — BYO account, consent-gated.

The other Telegram integration (``integrations/telegram.py``) is the official
Business bot: compliant, but 1:1 private chats only, a 24h reply window, and
possibly Premium-gated. This one logs in as the *user's own account* over
MTProto, which reaches everything they can reach — groups, channels, history.

RISK, STATED ACCURATELY — this is deliberately NOT the WhatsApp warning
=====================================================================
Verified against core.telegram.org rather than assumed, because an inaccurate
warning devalues every other warning we show:

* Telegram **welcomes** third-party clients: *"We welcome all developers to use
  our API and source code to create Telegram-like messaging applications on our
  platform free of charge"* (API Terms of Service). Using Telethon is therefore
  **not** a terms violation the way the WhatsApp linked-device client is.
* But: *"all accounts that log in using unofficial Telegram API clients are
  automatically put under observation to avoid violations of the Terms of
  Service"*, and *"If you use the Telegram API for flooding, spamming, faking
  subscriber and view counters of channels, you will be banned forever"*
  (Creating your Telegram Application).

So the honest statement is: permitted, but watched, and automated behaviour is
what gets accounts banned. That is what ``CONSENT_WARNING`` says.

THE SHARED api_id IS THE REAL SYSTEMIC RISK
===========================================
Every tenant logs in under ONE app-level ``TELEGRAM_API_ID``. Telegram rate-
limits and flags at the api_id level (``API_ID_PUBLISHED_FLOOD`` exists
precisely for over-shared ids), and sessions here originate from one
data-centre IP. That is the same correlated-fingerprint shape as the WhatsApp
device spoof: if the api_id gets flagged, **every tenant breaks at once**, not
one. Mitigations live outside this module (conservative pacing, no bulk sends,
per-tenant rate limits) and it is flagged for the capacity conversation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..crypto import CredentialCryptoError, decrypt, encrypt
from ..db import repo
from ..db.tenancy import TenantScope
from ..models import InboundMessage

PROVIDER = "telegram_userbot"

#: Bump when the warning text changes materially.
CONSENT_VERSION = "2026-08-21.tg-userbot.v1"

#: Shown in full before linking. Accurate to Telegram's published position —
#: see the module docstring for the sources.
CONSENT_WARNING = (
    "Connecting your personal Telegram account is ADVANCED and AT YOUR OWN "
    "RISK.\n\n"
    "Archon signs in as YOU using Telegram's API, the same way third-party "
    "Telegram apps do. Telegram allows third-party clients — but it also says "
    "that accounts logging in through them are automatically placed under "
    "observation, and that using the API for flooding, spamming or faking "
    "counters will get the account BANNED FOREVER.\n\n"
    "This gives Archon full access to your account: your groups, channels, "
    "message history, and the ability to send as you. A ban would affect your "
    "whole Telegram account, not just Archon, and may not be reversible.\n\n"
    "If you would rather not take that risk, the Telegram Business bot option "
    "is official and safe — it covers your 1:1 private chats only."
)


class UserbotLinkError(RuntimeError):
    """The login could not be started or completed."""


class ConsentRequired(UserbotLinkError):
    """Linking was attempted without acknowledging the risk."""


class PasswordRequired(UserbotLinkError):
    """The account has 2FA; a password is needed to finish signing in."""


# TODO(TOS-REVIEW): Telegram — operates a user account (userbot) over MTProto with a shared api_id — automation Telegram restricts — review before launch
def consent_notice() -> dict[str, Any]:
    return {
        "version": CONSENT_VERSION,
        "warning": CONSENT_WARNING,
        "risk": "account_ban",
        "reversible": False,
        # Named so the app can offer the compliant path alongside it.
        "safe_alternative": "telegram_business_bot",
    }


def is_configured(rt: Any) -> bool:
    """The app-level API credentials every tenant's login rides on."""
    return bool(rt.settings.telegram_api_id and rt.settings.telegram_api_hash)


def _require_configured(rt: Any) -> None:
    if not is_configured(rt):
        raise UserbotLinkError(
            "Telegram userbot is not configured — set TELEGRAM_API_ID and "
            "TELEGRAM_API_HASH (from https://my.telegram.org)"
        )


# --- secret storage ----------------------------------------------------------

def _seal(rt: Any, tenant_id: int, plaintext: str) -> str:
    return encrypt(rt.settings.credential_encryption_key, plaintext,
                   tenant_id=tenant_id, purpose=PROVIDER)


def _open(rt: Any, tenant_id: int, envelope: str) -> str:
    return decrypt(rt.settings.credential_encryption_key, envelope,
                   tenant_id=tenant_id, purpose=PROVIDER)


def session_string(rt: Any, tenant_id: int) -> str | None:
    """This tenant's decrypted StringSession, or None."""
    row = repo.tg_userbot_get(TenantScope(rt.db, tenant_id))
    if row is None or not row["session_envelope"]:
        return None
    try:
        return _open(rt, tenant_id, row["session_envelope"])
    except CredentialCryptoError as exc:
        rt.audit.note("tg_userbot_session_undecryptable", tenant_id=tenant_id,
                      error=str(exc)[:200])
        return None


# --- the login flow ----------------------------------------------------------
# Telethon is injected at both steps so the whole handshake is testable without
# touching Telegram. Production passes None and gets the real client.

def _new_client(rt: Any, session: str | None = None):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(StringSession(session or None),
                          int(rt.settings.telegram_api_id),
                          rt.settings.telegram_api_hash)


async def start_login(rt: Any, tenant_id: int, *, phone: str,
                      consent_acknowledged: bool,
                      consent_version: str | None = None,
                      client_factory=None) -> dict[str, Any]:
    """Send the login code. REFUSES without explicit consent."""
    if consent_acknowledged is not True:
        rt.audit.note("tg_userbot_refused_no_consent", tenant_id=tenant_id)
        raise ConsentRequired(
            "Connecting a Telegram account requires explicitly acknowledging "
            "the account-ban risk before it can start"
        )
    if consent_version and consent_version != CONSENT_VERSION:
        raise ConsentRequired(
            f"the warning has been updated (now {CONSENT_VERSION}); please "
            "re-read it and acknowledge again"
        )
    _require_configured(rt)
    phone = (phone or "").strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        raise UserbotLinkError("phone must be in international format, e.g. +447700900000")

    scope = TenantScope(rt.db, tenant_id)
    repo.tg_userbot_create(scope, consent_version=CONSENT_VERSION, phone=phone)
    rt.audit.note("tg_userbot_consent_acknowledged", tenant_id=tenant_id,
                  consent_version=CONSENT_VERSION, risk="permanent_account_ban")

    client = (client_factory or _new_client)(rt)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        # The half-finished session must be kept: Telethon signs in on the SAME
        # connection state that requested the code.
        repo.tg_userbot_store_session(
            scope, _seal(rt, tenant_id, client.session.save()))
        repo.tg_userbot_store_login_hash(
            scope, _seal(rt, tenant_id, sent.phone_code_hash))
        repo.tg_userbot_set_status(scope, status="code_sent")
    except Exception as exc:  # noqa: BLE001 — surface as a link error, not a 500
        repo.tg_userbot_set_status(scope, status="failed",
                                   last_error=repr(exc)[:300])
        raise UserbotLinkError(f"could not send the login code: {exc}") from exc
    finally:
        await _safe_disconnect(client)

    rt.audit.note("tg_userbot_code_sent", tenant_id=tenant_id)
    return {"status": "code_sent", "phone": phone,
            "consent_version": CONSENT_VERSION}


async def complete_login(rt: Any, tenant_id: int, *, code: str,
                         password: str | None = None,
                         client_factory=None) -> dict[str, Any]:
    """Finish signing in, handling 2FA. Stores the StringSession encrypted."""
    _require_configured(rt)
    scope = TenantScope(rt.db, tenant_id)
    row = repo.tg_userbot_get(scope)
    if row is None or row["status"] not in ("code_sent", "password_required"):
        raise UserbotLinkError("no login in progress — start one first")

    session = session_string(rt, tenant_id)
    code_hash = None
    if row["login_hash_envelope"]:
        try:
            code_hash = _open(rt, tenant_id, row["login_hash_envelope"])
        except CredentialCryptoError:
            code_hash = None

    client = (client_factory or _new_client)(rt, session)
    try:
        await client.connect()
        try:
            await client.sign_in(phone=row["phone"], code=(code or "").strip(),
                                 phone_code_hash=code_hash)
        except Exception as exc:  # noqa: BLE001
            if _is_password_required(exc):
                if not password:
                    repo.tg_userbot_set_status(scope, status="password_required")
                    # Keep the session: the password step resumes this login.
                    repo.tg_userbot_store_session(
                        scope, _seal(rt, tenant_id, client.session.save()))
                    raise PasswordRequired(
                        "this account has two-step verification; send the "
                        "password to finish signing in"
                    ) from exc
                await client.sign_in(password=password)
            else:
                raise

        me = await client.get_me()
        repo.tg_userbot_store_session(
            scope, _seal(rt, tenant_id, client.session.save()))
        repo.tg_userbot_store_login_hash(scope, None)   # spent; not durable
        repo.tg_userbot_set_status(
            scope, status="active",
            tg_user_id=str(getattr(me, "id", "")) or None,
            tg_username=getattr(me, "username", None),
        )
    except PasswordRequired:
        raise
    except Exception as exc:  # noqa: BLE001
        repo.tg_userbot_set_status(scope, status="failed",
                                   last_error=repr(exc)[:300])
        raise UserbotLinkError(f"sign-in failed: {exc}") from exc
    finally:
        await _safe_disconnect(client)

    rt.audit.note("tg_userbot_linked", tenant_id=tenant_id)
    return status(rt, scope)


def _is_password_required(exc: Exception) -> bool:
    """Telethon raises SessionPasswordNeededError for 2FA accounts."""
    return type(exc).__name__ == "SessionPasswordNeededError"


async def _safe_disconnect(client: Any) -> None:
    try:
        result = client.disconnect()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception:  # noqa: BLE001 — teardown must not mask the real error
        pass


# --- status / lifecycle ------------------------------------------------------

def status(rt: Any, store: Any) -> dict[str, Any]:
    row = repo.tg_userbot_get(store)
    if row is None:
        return {"linked": False, "status": "not_linked", "phone": None,
                "username": None, "consent_version": None}
    return {
        "linked": row["status"] == "active",
        "status": row["status"],
        "phone": row["phone"],
        "username": row["tg_username"],
        "consent_version": row["consent_version"],
        "consent_acknowledged_at": row["consent_acknowledged_at"],
        "logged_in_at": row["logged_in_at"],
        "last_error": row["last_error"],
    }


async def unlink(rt: Any, tenant_id: int) -> bool:
    """Drop the live client and wipe the stored session."""
    await rt.sessions.evict(tenant_id, PROVIDER)
    revoked = repo.tg_userbot_revoke(TenantScope(rt.db, tenant_id))
    if revoked:
        rt.audit.note("tg_userbot_unlinked", tenant_id=tenant_id)
    return revoked


def record_logged_out(rt: Any, tenant_id: int, *, banned: bool = False) -> None:
    """Telegram ended the session — signed out elsewhere, or banned.

    A ban is recorded distinctly because it is the outcome the user was warned
    about, and the app should say so plainly.
    """
    scope = TenantScope(rt.db, tenant_id)
    repo.tg_userbot_set_status(scope, status="banned" if banned else "logged_out")
    repo.tg_userbot_store_session(scope, None)
    rt.audit.note("tg_userbot_banned" if banned else "tg_userbot_logged_out",
                  tenant_id=tenant_id)


# --- session registry --------------------------------------------------------

async def build_client(rt: Any, tenant_id: int):
    """SessionRegistry factory: a connected Telethon client for one tenant.

    Isolated by construction — the session comes from that tenant's row,
    decrypted under a binding to that tenant. The registry's per-key lock keeps
    a tenant from ever holding two concurrent sessions, which Telegram treats
    as a signal worth flagging.
    """
    session = session_string(rt, tenant_id)
    if not session:
        raise UserbotLinkError(
            f"tenant {tenant_id} has no linked Telegram account"
        )
    client = _new_client(rt, session)
    # Wired BEFORE connecting: Telethon starts dispatching as soon as the
    # connection is up, so registering afterwards races the first messages and
    # would silently drop whatever arrives in that window.
    wire_events(rt, tenant_id, client)
    await client.connect()
    return client


def wire_events(rt: Any, tenant_id: int, client: Any) -> None:
    """Subscribe a tenant's userbot to inbound messages.

    The owner's equivalent is ``platforms/telegram/userbot.py``. Two deliberate
    differences:

    * every message is stamped with ``tenant_id``. ``InboundMessage`` defaults
      that field to the OWNER, so a missing stamp does not fail — it files a
      stranger's Telegram into the owner's chats and agent context. That is the
      leak, and it is why the field is set explicitly rather than left to a
      default.
    * PRIVATE chats are kept. The owner's userbot drops them because their
      Business bot already covers private 1:1; a product tenant has no Business
      bot, so private chats are the main case rather than a duplicate.

    Handlers never raise into Telethon's dispatcher — one malformed message
    must not tear down the tenant's connection.
    """
    from telethon import events

    # Shared with the owner's userbot on purpose: chat-id normalisation decides
    # which row a message lands in, so if the two paths ever disagreed the same
    # chat would split in two. One implementation, not two that drift.
    from ..platforms.telegram.userbot import _display_name, _norm_chat_id

    @client.on(events.NewMessage())
    async def _on_new(event: Any) -> None:
        try:
            msg = event.message
            chat = await event.get_chat()
            if event.is_private:
                chat_kind = "private"
            elif getattr(chat, "broadcast", False):
                chat_kind = "channel"
            else:
                chat_kind = "group"
            sender = getattr(event, "sender", None)
            await rt.bus.publish(InboundMessage(
                platform="tg",
                source="userbot",
                tenant_id=tenant_id,
                chat_id=_norm_chat_id(event.chat_id),
                chat_kind=chat_kind,
                chat_name=_display_name(chat) if chat else None,
                msg_id=str(msg.id),
                sender_id=str(getattr(event, "sender_id", "") or "unknown"),
                sender_name=_display_name(sender) if sender else None,
                ts=msg.date or datetime.now(UTC),
                is_from_me=bool(getattr(msg, "out", False)),
                text=msg.message or None,
            ))
        except Exception as exc:  # noqa: BLE001 — must not kill the session
            rt.audit.note("tg_userbot_inbound_failed", tenant_id=tenant_id,
                          error=repr(exc)[:200])


async def send_message(rt: Any, tenant_id: int, peer: str, text: str) -> dict[str, Any]:
    """Send as this tenant's own Telegram account, under the outbound budgets.

    The pacing is not decoration. Every tenant's userbot rides one shared
    ``TELEGRAM_API_ID``, Telegram flags at that level, and "flooding, spamming,
    faking counters" is documented as a forever-ban — so an unpaced send path
    is the single most likely way to lose EVERY tenant's account at once. See
    ``pacing.py``; the global budget is the guard that matters.

    Budget first, gap second: a refusal should be instant rather than arriving
    after a pointless sleep. Refusals raise :class:`~archon.pacing.PaceRefused`
    so a caller can report the wait instead of stalling silently.

    UNVALIDATED AGAINST A LIVE ACCOUNT (task #12). The budget logic is covered
    by ``tests/test_pacing.py``; the Telethon call below is not, and mocking it
    would only assert what this code already believes. It needs one real
    throwaway account before anyone should call it proven.
    """
    from ..pacing import pacer_for

    pacer = pacer_for(rt)
    pacer.acquire(tenant_id, str(peer))
    await pacer.gap()

    client = await rt.sessions.get(rt, tenant_id, PROVIDER)
    sent = await client.send_message(peer, text)
    # No message text in the audit log: this table is shared across tenants and
    # the content belongs to third parties (see Settings.store_audit_content).
    rt.audit.note("tg_userbot_sent", tenant_id=tenant_id,
                  peer=str(peer), chars=len(text))
    return {"ok": True, "message_id": getattr(sent, "id", None)}


def register(rt: Any) -> None:
    rt.sessions.register_factory(PROVIDER, build_client)
