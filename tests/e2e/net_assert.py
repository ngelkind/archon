"""Assertions over the network ledger, for end-to-end scenarios.

A scenario asserts on the WIRE the same way it asserts on the DB or the audit:
that a subsystem actually reached (or never reached) a host, and — the CI
hygiene gate — that an offline run recorded nothing but loopback. These read
``rt.net`` (a :class:`archon.netlog.NetLedger`).
"""

from __future__ import annotations

from typing import Any

from archon.netlog import is_loopback


def _calls(rt: Any) -> list[Any]:
    net = getattr(rt, "net", None)
    assert net is not None, "runtime has no network ledger (rt.net is None)"
    return net.recent(limit=10_000)


def assert_call(rt: Any, *, host: str | None = None, subsystem: str | None = None,
                method: str | None = None) -> Any:
    """Assert at least one recorded call matches, and return the newest match."""
    for c in _calls(rt):
        if host is not None and c.host != host:
            continue
        if subsystem is not None and c.subsystem != subsystem:
            continue
        if method is not None and c.method != method:
            continue
        return c
    want = {k: v for k, v in (("host", host), ("subsystem", subsystem),
                              ("method", method)) if v is not None}
    seen = sorted({(c.subsystem, c.host) for c in _calls(rt)})
    raise AssertionError(f"no network call matched {want}; recorded: {seen}")


def assert_no_call_to(rt: Any, host: str) -> None:
    hits = [c for c in _calls(rt) if c.host == host]
    assert not hits, f"expected no call to {host}, but saw {len(hits)}"


def only_expected_hosts(rt: Any, *, allow: tuple[str, ...] = ()) -> None:
    """CI hygiene: every recorded host is loopback (or explicitly allowed).

    An offline scenario drives fake transports on 127.0.0.1 and a scripted LLM
    that makes no HTTP call, so a non-loopback host means something reached the
    real internet — exactly what the offline gate must forbid."""
    bad = sorted({c.host for c in _calls(rt)
                  if c.host and not is_loopback(c.host) and c.host not in allow})
    assert not bad, f"offline run reached non-loopback host(s): {bad}"
