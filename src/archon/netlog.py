"""In-process network ledger: a metadata record of every outbound request.

The audit's blind spot was the wire. A subsystem could believe it was talking to
Telegram while its socket was dead, and nothing outside the process would ever
know. The ledger closes that gap by recording — for every outbound call the
hooks in :mod:`archon.net` can see — a small tuple of METADATA:

    (ts, subsystem, method, host, path, status, req_bytes, resp_bytes,
     duration_ms, error, purpose)

It NEVER stores bodies or query strings: the point is observability, not a
transcript, and query strings routinely carry tokens. Two things read it:

* operators, through ``/netstat``, ``GET /net/recent`` and the ``/status`` line;
* the test suite, through :mod:`tests.e2e.net_assert`, which asserts that a
  scenario actually hit (or never hit) a given host — the offline suite must
  record nothing but loopback.

The ledger also carries an EGRESS TRIPWIRE: the first time it sees a host that
is not on the expected-suffix allowlist, it audits ``egress_unexpected``,
degrades ``rt.health['egress']`` and alerts the owner. A bot that starts phoning
somewhere new is exactly the kind of thing an audit that only reads code cannot
catch.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

# TODO(TOS-REVIEW): telemetry — the ledger records host + path metadata for the
# owner's own traffic only (single-user PoC). If this ever runs multi-tenant,
# review whether per-tenant egress metadata is subject to the same Limited Use
# and data-minimisation rules as message content — review before launch.

#: Host suffixes this process is expected to reach. A host outside the set is
#: audited + alerted on first sighting. Suffix match, so "g.whatsapp.net" ==
#: expected via "whatsapp.net". Loopback is always allowed.
EXPECTED_SUFFIXES: tuple[str, ...] = (
    # Telegram (MTProto DCs resolve to these; Bot API is api.telegram.org)
    "telegram.org", "t.me", "telegram.me",
    # WhatsApp / Meta
    "whatsapp.net", "whatsapp.com", "facebook.com", "fbcdn.net", "meta.com",
    # Google — Gmail, Calendar, OAuth, google-genai
    "googleapis.com", "google.com", "gstatic.com", "googleusercontent.com",
    # LLM providers
    "anthropic.com", "openai.com", "openrouter.ai",
    # source updates / deploy
    "github.com", "githubusercontent.com",
    # self
    "localhost",
)

_LOOPBACK = frozenset({"127.0.0.1", "::1", "0.0.0.0", "localhost", ""})

#: Subsystems that reach hosts chosen at call time (the open web) or by the
#: operator's own config. Their calls are RECORDED but never trip the egress
#: alarm — an unexpected host there is the feature, not an intrusion.
EXEMPT_SUBSYSTEMS = frozenset({"download", "fetch", "push", "socket"})


def host_is_expected(host: str, expected: tuple[str, ...] = EXPECTED_SUFFIXES) -> bool:
    h = (host or "").lower().strip(".")
    if h in _LOOPBACK:
        return True
    return any(h == s or h.endswith("." + s) for s in expected)


def is_loopback(host: str) -> bool:
    return (host or "").lower().strip(".") in _LOOPBACK


@dataclass(slots=True)
class NetCall:
    ts: float
    subsystem: str
    method: str
    host: str
    path: str = ""
    status: int | None = None
    req_bytes: int | None = None
    resp_bytes: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    purpose: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and (self.status is None or self.status < 400)


class NetLedger:
    """A bounded, in-memory ring of :class:`NetCall`. Cheap to write from a hot
    hook; never raises back into the caller's request path."""

    def __init__(self, rt: Any, *, capacity: int = 1024,
                 expected: tuple[str, ...] = EXPECTED_SUFFIXES,
                 alert: bool = True) -> None:
        self._rt = rt
        self._buf: deque[NetCall] = deque(maxlen=capacity)
        self._expected = expected
        self._alert = alert
        self._hosts: set[str] = set()
        self._flagged: set[str] = set()
        self._alert_tasks: set[asyncio.Task[Any]] = set()

    # --- write ---------------------------------------------------------------

    def record(self, call: NetCall) -> None:
        """Append a call. Best-effort: any bookkeeping failure is swallowed so a
        ledger bug can never break the request that was being observed."""
        self._buf.append(call)
        self._hosts.add(call.host)
        with contextlib.suppress(Exception):
            self._rt.events.publish(
                "net.call", subsystem=call.subsystem, host=call.host,
                method=call.method, status=call.status,
                duration_ms=call.duration_ms, error=call.error)
        if (call.host and call.subsystem not in EXEMPT_SUBSYSTEMS
                and not host_is_expected(call.host, self._expected)
                and call.host not in self._flagged):
            self._flagged.add(call.host)
            self._raise_egress(call)

    def _raise_egress(self, call: NetCall) -> None:
        rt = self._rt
        with contextlib.suppress(Exception):
            rt.audit.note("egress_unexpected", host=call.host,
                          subsystem=call.subsystem, method=call.method, path=call.path)
        with contextlib.suppress(Exception):
            rt.health["egress"] = f"unexpected host {call.host}"
        if not self._alert:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (a sync hook off the event loop) — audit stands alone
        from . import alerts
        task = loop.create_task(alerts.alert_owner(
            rt, f"egress:{call.host}",
            f"🚨 Unexpected outbound connection to {call.host} "
            f"({call.subsystem} {call.method} {call.path})"))
        # Hold a strong ref so the task is not GC'd mid-flight (the leak the
        # registry alert path hit).
        self._alert_tasks.add(task)
        task.add_done_callback(self._alert_tasks.discard)

    # --- read ----------------------------------------------------------------

    def recent(self, limit: int = 50, *, subsystem: str | None = None) -> list[NetCall]:
        calls = list(self._buf)
        if subsystem:
            calls = [c for c in calls if c.subsystem == subsystem]
        return calls[-limit:][::-1]  # newest first

    def hosts(self) -> set[str]:
        return set(self._hosts)

    def unexpected_hosts(self) -> set[str]:
        return {h for h in self._hosts
                if h and not host_is_expected(h, self._expected)}

    def summary(self, window_s: float = 300.0) -> dict[str, Any]:
        """Per-subsystem counts over the last ``window_s`` seconds, plus the set
        of hosts and any that fell outside the policy."""
        cutoff = time.time() - window_s
        by_sub: dict[str, dict[str, int]] = {}
        hosts: set[str] = set()
        for c in self._buf:
            if c.ts < cutoff:
                continue
            hosts.add(c.host)
            slot = by_sub.setdefault(c.subsystem, {"calls": 0, "errors": 0})
            slot["calls"] += 1
            if not c.ok:
                slot["errors"] += 1
        return {
            "window_s": window_s,
            "total": sum(s["calls"] for s in by_sub.values()),
            "by_subsystem": by_sub,
            "hosts": sorted(h for h in hosts if h),
            "unexpected_hosts": sorted(
                h for h in hosts if h and not host_is_expected(h, self._expected)),
        }
