"""Hashing + minting for device auth.

Only *hashes* of bearer tokens and pair codes are ever stored (HMAC-SHA256 keyed
by a server-side pepper); the plaintext is shown to the client once and never
persisted, sidestepping the plaintext-secrets concern. This module has no heavy
imports so it is safe to use from the control bot as well as the API.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def hash_secret(pepper: str, secret: str) -> str:
    """Deterministic keyed hash of ``secret``. Without the pepper (a server-side
    secret) a stolen DB yields no usable tokens; being keyed also makes it
    constant-work per lookup and unamenable to precomputed tables."""
    return hmac.new(
        pepper.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def tokens_match(pepper: str, secret: str, expected_hash: str) -> bool:
    """Constant-time comparison of ``secret``'s hash against a stored hash."""
    return hmac.compare_digest(hash_secret(pepper, secret), expected_hash)


def mint_token() -> str:
    """A fresh 256-bit URL-safe bearer token."""
    return secrets.token_urlsafe(32)


def mint_pair_code() -> str:
    """A short 6-digit one-time code for out-of-band entry in the app."""
    return f"{secrets.randbelow(1_000_000):06d}"
