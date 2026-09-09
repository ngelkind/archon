"""Socket probe: sample the process's OWN established connections.

The httpx/aiohttp hooks see requests we make through those libraries. Telethon's
MTProto and neonize's WhatsApp link are raw sockets that no in-process hook can
observe — so a userbot can report "connected" while its socket is dead, and the
ledger would show nothing. This probe closes that hole by reading the kernel's
view of our own sockets (``ss -Htunp`` restricted to our pid, which needs no
privilege — verified on the VM) every :data:`SAMPLE_S` seconds and:

* cross-checking health — a subsystem that claims to be connected while the
  process holds no established socket on a platform port is a lie worth an
  ``socket_health_mismatch`` note;
* enforcing a coarse egress policy — an established peer on a port outside
  :data:`ALLOWED_PORTS` is audited and alerted, once per peer.

``ss`` is Linux-only. On a host without it (a Mac dev box) the probe stands
down cleanly with a health line, exactly like an unconfigured subsystem — it
never crashes the supervisor.

# TODO(TOS-REVIEW): none — this reads only our own process's socket table via
# an unprivileged `ss` on our pid; no third-party data or platform API is
# touched. Kept as a marker so the sweep sees it was considered.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..netlog import NetCall

SAMPLE_S = 10.0
#: Remote service ports we expect to hold established TCP to. 443 covers every
#: platform's TLS; 80 is Telegram's fallback; 5222/5223 XMPP (WhatsApp),
#: 3478 STUN (WhatsApp calls), 8443 alt-TLS.
ALLOWED_PORTS = frozenset({80, 443, 5222, 5223, 3478, 8443})
#: Subsystems whose "connected" claim should be backed by a live socket.
#: Subsystems whose live socket IS in our own process (so the cross-check is
#: meaningful). WhatsApp is intentionally absent: its socket is held by the
#: out-of-process goneonize helper, and ConnectedEv already tracks it.
_WATCHED = ("tg_userbot",)
_CONNECTED_MARKERS = ("connected", "running", "polling", "logged in")


@dataclass(slots=True)
class SocketPeer:
    ip: str
    port: int
    state: str
    pid: int | None = None


def parse_ss(output: str) -> list[SocketPeer]:
    """Parse ``ss -Htunp`` output into established TCP peers. Tolerant of the
    column drift between distros: it locates the peer as the second address-like
    token and keeps only ESTAB rows."""
    peers: list[SocketPeer] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        state = fields[1]
        if state != "ESTAB":
            continue
        peer = fields[5]
        ip, port = _split_hostport(peer)
        if port is None:
            continue
        peers.append(SocketPeer(ip=ip, port=port, state=state, pid=_owning_pid(line)))
    return peers


_PID_RE = re.compile(r"pid=(\d+)")


def _owning_pid(line: str) -> int | None:
    m = _PID_RE.search(line)
    return int(m.group(1)) if m else None


def own_pids() -> set[int]:
    """This process and its descendants. `ss` reports the whole host's sockets,
    so we keep only the ones OUR tree owns — otherwise sshd's inbound connection
    (and every other service) reads as our egress. WhatsApp's goneonize helper
    is a child, so the tree, not just our pid, is what we want."""
    import os

    me = os.getpid()
    children: dict[int, list[int]] = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat") as fh:
                    parts = fh.read().rsplit(") ", 1)[-1].split()
                ppid = int(parts[1])  # PPid is field 4 overall; field 2 after "comm)"
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(ppid, []).append(int(entry))
    except OSError:
        return {me}
    tree, stack = {me}, [me]
    while stack:
        for kid in children.get(stack.pop(), ()):
            if kid not in tree:
                tree.add(kid)
                stack.append(kid)
    return tree


def _split_hostport(token: str) -> tuple[str, int | None]:
    if token.startswith("["):  # [ipv6]:port
        host, _, rest = token[1:].partition("]")
        port = rest.lstrip(":")
        return host, (int(port) if port.isdigit() else None)
    host, _, port = token.rpartition(":")
    if not host:  # no colon at all
        return token, None
    return host, (int(port) if port.isdigit() else None)


async def _default_sampler() -> str | None:
    """Run ``ss`` for our own sockets. Returns None when ``ss`` is absent."""
    if shutil.which("ss") is None:
        return None
    try:
        # NB: no `state established` filter — it SUPPRESSES the State column,
        # which parse_ss keys on ("ESTAB" in field 1). We filter ESTAB in the
        # parser instead, so the column is present.
        proc = await asyncio.create_subprocess_exec(
            "ss", "-Htunp",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except (TimeoutError, OSError):
        return None
    return out.decode("utf-8", "replace")


def evaluate(rt: Any, peers: list[SocketPeer]) -> None:
    """One sampling pass: record peers, cross-check health, flag odd ports."""
    led = getattr(rt, "net", None)
    now = time.time()
    for p in peers:
        if led is not None:
            led.record(NetCall(ts=now, subsystem="socket", method="socket",
                               host=p.ip, path=str(p.port), status=None))
        if p.port not in ALLOWED_PORTS:
            _flag_port(rt, p)

    # Health cross-check: a subsystem that says it is connected must hold at
    # least one established socket on a platform port, or it is lying.
    live_platform = any(p.port in ALLOWED_PORTS for p in peers)
    health = getattr(rt, "health", {})
    for name in _WATCHED:
        state = str(health.get(name, "")).lower()
        claims = any(m in state for m in _CONNECTED_MARKERS)
        key = f"socket:{name}"
        if claims and not live_platform:
            if health.get(key) != "no established socket":
                health[key] = "no established socket"
                _note(rt, "socket_health_mismatch", subsystem=name, state=state)
        else:
            health.pop(key, None)


_flagged_peers: set[str] = set()


def _flag_port(rt: Any, peer: SocketPeer) -> None:
    key = f"{peer.ip}:{peer.port}"
    if key in _flagged_peers:
        return
    _flagged_peers.add(key)
    _note(rt, "egress_unexpected", host=peer.ip, subsystem="socket",
          method="socket", path=str(peer.port))
    with contextlib.suppress(Exception):
        rt.health["egress"] = f"socket to {key} (unexpected port)"
    from .. import alerts
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(alerts.alert_owner(
        rt, f"egress:{key}",
        f"🚨 Established socket to {key} on an unexpected port"))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


_tasks: set[asyncio.Task[Any]] = set()


def _note(rt: Any, message: str, **fields: Any) -> None:
    with contextlib.suppress(Exception):
        rt.audit.note(message, **fields)


async def run(rt: Any, *, sampler: Callable[[], Any] | None = None,
              interval_s: float = SAMPLE_S) -> None:
    """Supervised subsystem entry point. Loops until cancelled; stands down
    (clean return) if ``ss`` is unavailable so the supervisor doesn't restart
    it forever on a host that will never have it."""
    sample = sampler or _default_sampler
    first = await sample()
    if first is None:
        rt.health["socket_probe"] = "disabled: ss unavailable (not Linux?)"
        return
    rt.health["socket_probe"] = "running"
    while True:
        out = await sample()
        if out is not None:
            mine = own_pids()
            peers = [p for p in parse_ss(out) if p.pid in mine]
            evaluate(rt, peers)
        await asyncio.sleep(interval_s)
