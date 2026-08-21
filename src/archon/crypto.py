"""Envelope encryption for integration credentials at rest.

A tenant's Google refresh token is a long-lived key to their mailbox and
calendar. It must not sit in the database as plaintext, where a stolen `.db`
file — or a backup, or an errant `SELECT` in a support session — hands over
every user's account at once.

Scheme: **AES-256-GCM with a per-record data key, wrapped by a KEK from config.**

* A fresh 256-bit DEK is generated per record and used once. Compromising one
  record's DEK reveals that record only, and re-encrypting a record does not
  reuse a nonce with an old key.
* The DEK is wrapped with the KEK (``credential_encryption_key``), so rotating
  the KEK means re-wrapping small DEKs rather than re-encrypting every secret,
  and the KEK itself never touches a ciphertext of user data.
* Both layers are AEAD with **associated data binding the record to its tenant
  and purpose** (``tenant:<id>:<purpose>``). A ciphertext copied into another
  tenant's row, or reused for another provider, fails to decrypt rather than
  silently handing the wrong tenant a working credential. That is the property
  that makes this safe to store next to a tenant_id an attacker can edit.

The stored value is a self-describing JSON envelope, so a future key rotation or
algorithm change can be detected rather than guessed.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: Bumped if the envelope layout ever changes.
ENVELOPE_VERSION = 1
_NONCE_BYTES = 12          # 96-bit nonce: the GCM standard
_DEK_BYTES = 32            # AES-256


class CredentialCryptoError(RuntimeError):
    """Encryption or decryption failed (bad key, tampered data, wrong tenant)."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def derive_kek(key_material: str) -> bytes:
    """Turn the configured key string into a 32-byte KEK.

    Accepts either 64 hex chars / 44 base64 chars (a real generated key, used
    verbatim) or an arbitrary passphrase, which is hashed to 32 bytes. Hashing a
    passphrase is a convenience for development — production should set a
    generated key, which ``check_production_secrets`` enforces.
    """
    import hashlib

    material = (key_material or "").strip()
    if not material:
        raise CredentialCryptoError(
            "credential_encryption_key is not set — refusing to handle secrets"
        )
    try:
        raw = bytes.fromhex(material)
        if len(raw) == _DEK_BYTES:
            return raw
    except ValueError:
        pass
    try:
        raw = base64.b64decode(material, validate=True)
        if len(raw) == _DEK_BYTES:
            return raw
    except Exception:  # noqa: BLE001 — not base64; fall through to hashing
        pass
    return hashlib.sha256(material.encode("utf-8")).digest()


def aad_for(tenant_id: int, purpose: str) -> bytes:
    """Associated data binding a ciphertext to one tenant and one purpose."""
    return f"tenant:{int(tenant_id)}:{purpose}".encode()


def encrypt(key_material: str, plaintext: str, *, tenant_id: int,
            purpose: str) -> str:
    """Encrypt ``plaintext`` for one tenant; returns the JSON envelope."""
    kek = derive_kek(key_material)
    aad = aad_for(tenant_id, purpose)

    dek = AESGCM.generate_key(bit_length=256)
    data_nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(data_nonce, plaintext.encode("utf-8"), aad)

    wrap_nonce = os.urandom(_NONCE_BYTES)
    wrapped_dek = AESGCM(kek).encrypt(wrap_nonce, dek, aad)

    return json.dumps({
        "v": ENVELOPE_VERSION,
        "alg": "AESGCM-256",
        "dek": _b64(wrapped_dek),
        "dn": _b64(wrap_nonce),
        "ct": _b64(ciphertext),
        "n": _b64(data_nonce),
    }, separators=(",", ":"))


def decrypt(key_material: str, envelope: str, *, tenant_id: int,
            purpose: str) -> str:
    """Decrypt an envelope. Raises if the key is wrong, the data was tampered
    with, or the record does not belong to this tenant/purpose."""
    kek = derive_kek(key_material)
    aad = aad_for(tenant_id, purpose)
    try:
        parsed: dict[str, Any] = json.loads(envelope)
    except ValueError as exc:
        raise CredentialCryptoError("credential envelope is not valid JSON") from exc
    if parsed.get("v") != ENVELOPE_VERSION:
        raise CredentialCryptoError(
            f"unsupported credential envelope version {parsed.get('v')!r}"
        )
    try:
        dek = AESGCM(kek).decrypt(_unb64(parsed["dn"]), _unb64(parsed["dek"]), aad)
        return AESGCM(dek).decrypt(
            _unb64(parsed["n"]), _unb64(parsed["ct"]), aad
        ).decode("utf-8")
    except InvalidTag as exc:
        raise CredentialCryptoError(
            "credential could not be decrypted — wrong key, tampered data, or a "
            "record belonging to a different tenant"
        ) from exc
    except (KeyError, ValueError) as exc:
        raise CredentialCryptoError("malformed credential envelope") from exc
