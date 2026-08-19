"""WhatsApp JID parsing and normalisation.

Every allowlist comparison in this project goes through :func:`normalize`. The
same account can appear with a device suffix (``9725...:12@s.whatsapp.net``) or
in different letter case, so comparing raw strings would let an allowed sender
look unknown — or, worse, let a crafted value look allowed.

Normalisation never widens a JID: it only strips the device suffix and lowercases.
Anything it cannot parse confidently becomes ``None``, which every caller treats
as "not on the allowlist".
"""

from __future__ import annotations

import re

USER_SERVER = "s.whatsapp.net"
GROUP_SERVER = "g.us"
LID_SERVER = "lid"
BROADCAST_SERVER = "broadcast"

KNOWN_SERVERS = frozenset({USER_SERVER, GROUP_SERVER, LID_SERVER, BROADCAST_SERVER})

# user part: digits (phone) or digits-with-hyphen (legacy group), optionally
# followed by ":<device>" or "_<agent>" which we strip.
_JID_RE = re.compile(
    r"^(?P<user>[0-9][0-9\-]*)(?::(?P<device>\d+))?(?:_(?P<agent>\d+))?@(?P<server>[a-z0-9.\-]+)$"
)


def normalize(raw: object) -> str | None:
    """Return ``user@server`` with device/agent suffixes stripped, or ``None``.

    Accepts a string or any object exposing ``User``/``Server`` attributes (the
    shape neonize hands back). Returns ``None`` for anything unrecognised —
    callers must treat that as untrusted.
    """
    if raw is None:
        return None

    if not isinstance(raw, str):
        user = getattr(raw, "User", None)
        server = getattr(raw, "Server", None)
        if not user or not server:
            return None
        raw = f"{user}@{server}"

    candidate = raw.strip().lower()
    if not candidate or len(candidate) > 128:
        return None

    match = _JID_RE.match(candidate)
    if not match:
        return None

    server = match.group("server")
    if server not in KNOWN_SERVERS:
        return None

    return f"{match.group('user')}@{server}"


def is_group(jid: str | None) -> bool:
    return bool(jid) and jid.endswith("@" + GROUP_SERVER)


def is_user(jid: str | None) -> bool:
    """True for a direct-message JID (a person, not a group or broadcast)."""
    return bool(jid) and (
        jid.endswith("@" + USER_SERVER) or jid.endswith("@" + LID_SERVER)
    )


def is_broadcast(jid: str | None) -> bool:
    return bool(jid) and jid.endswith("@" + BROADCAST_SERVER)
