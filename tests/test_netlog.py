"""The network ledger, the httpx hook, and the socket probe.

These prove the wire is actually observed: a recorded call carries the right
host/subsystem/status, an unexpected host trips the egress alarm exactly once,
a failed request is recorded with its error and then re-raised, and the socket
probe both parses `ss` output and catches a subsystem that claims to be
connected while holding no socket.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from conftest import run_async
from test_m2 import make_rt

from archon.net.client import new_async_client
from archon.net.socket_probe import SocketPeer, evaluate, parse_ss
from archon.netlog import NetCall, NetLedger, host_is_expected, is_loopback


def _audit_actions(rt) -> list[str]:
    path = rt.audit.path
    if not path.exists():
        return []
    return [json.loads(line)["action"] for line in path.read_text().splitlines()
            if line.strip()]


# --- policy ------------------------------------------------------------------

def test_expected_host_policy():
    assert host_is_expected("api.telegram.org")
    assert host_is_expected("g.whatsapp.net")
    assert host_is_expected("oauth2.googleapis.com")
    assert host_is_expected("127.0.0.1")
    assert is_loopback("localhost")
    assert not host_is_expected("evil.example.com")
    # a look-alike suffix must not pass (endswith needs the dot boundary)
    assert not host_is_expected("nottelegram.org")
    assert not host_is_expected("telegram.org.evil.com")


# --- ledger ------------------------------------------------------------------

def test_ledger_records_summary_and_recent(tmp_path):
    rt = make_rt(tmp_path)
    led = NetLedger(rt, alert=False)
    led.record(NetCall(ts=time.time(), subsystem="llm", method="POST",
                       host="api.anthropic.com", path="/v1/messages", status=200))
    led.record(NetCall(ts=time.time(), subsystem="telegram", method="POST",
                       host="api.telegram.org", path="/bot/sendMessage", status=500))
    summ = led.summary()
    assert summ["total"] == 2
    assert summ["by_subsystem"]["telegram"]["errors"] == 1
    assert summ["by_subsystem"]["llm"]["errors"] == 0
    assert set(summ["hosts"]) == {"api.anthropic.com", "api.telegram.org"}
    assert summ["unexpected_hosts"] == []
    # newest first
    assert led.recent(limit=1)[0].host == "api.telegram.org"


def test_unexpected_host_trips_egress_once(tmp_path):
    rt = make_rt(tmp_path)
    led = NetLedger(rt, alert=False)
    for _ in range(3):
        led.record(NetCall(ts=time.time(), subsystem="llm", method="GET",
                           host="tracker.evil.com", path="/x"))
    actions = _audit_actions(rt)
    assert actions.count("egress_unexpected") == 1, actions
    assert "tracker.evil.com" in rt.health.get("egress", "")
    assert led.unexpected_hosts() == {"tracker.evil.com"}


def test_fetch_subsystem_is_exempt_from_egress(tmp_path):
    rt = make_rt(tmp_path)
    led = NetLedger(rt, alert=False)
    # web_fetch reaches arbitrary hosts by design — recorded, never alarmed.
    led.record(NetCall(ts=time.time(), subsystem="fetch", method="GET",
                       host="some-random-blog.example", path="/post"))
    assert _audit_actions(rt).count("egress_unexpected") == 0
    assert led.summary()["total"] == 1


# --- httpx hook --------------------------------------------------------------

@run_async
async def test_httpx_client_records_success(tmp_path):
    rt = make_rt(tmp_path)
    rt.net = NetLedger(rt, alert=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    async with new_async_client(rt, subsystem="llm", purpose="anthropic",
                                transport=httpx.MockTransport(handler)) as client:
        resp = await client.post("https://api.anthropic.com/v1/messages?key=SECRET")
    assert resp.status_code == 200
    call = rt.net.recent(limit=1)[0]
    assert call.subsystem == "llm" and call.host == "api.anthropic.com"
    assert call.status == 200 and call.method == "POST"
    # the path is kept, the query string (with the token) is NOT
    assert call.path == "/v1/messages"


@run_async
async def test_httpx_client_records_error_and_reraises(tmp_path):
    rt = make_rt(tmp_path)
    rt.net = NetLedger(rt, alert=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        async with new_async_client(rt, subsystem="llm",
                                    transport=httpx.MockTransport(handler)) as client:
            await client.get("https://api.openai.com/v1/models")
    call = rt.net.recent(limit=1)[0]
    assert call.error == "ConnectError" and call.status is None
    assert call.host == "api.openai.com"


# --- socket probe ------------------------------------------------------------

def test_parse_ss_keeps_established_peers():
    out = (
        'tcp ESTAB 0 0 10.0.0.5:5432 149.154.167.51:443 users:(("python",pid=1,fd=7))\n'
        'tcp ESTAB 0 0 10.0.0.5:5100 17.253.0.10:5222 users:(("python",pid=1,fd=8))\n'
        'tcp LISTEN 0 128 127.0.0.1:8788 0.0.0.0:*\n'
        'tcp ESTAB 0 0 [::1]:5000 [2606:4700::1]:443 users:(("python",pid=1,fd=9))\n'
    )
    peers = parse_ss(out)
    assert (SocketPeer("149.154.167.51", 443, "ESTAB") in peers)
    assert any(p.port == 5222 for p in peers)
    assert any(p.ip == "2606:4700::1" and p.port == 443 for p in peers)  # ipv6
    assert all(p.state == "ESTAB" for p in peers)  # LISTEN dropped


def test_socket_probe_records_and_flags_odd_port(tmp_path):
    rt = make_rt(tmp_path)
    rt.net = NetLedger(rt, alert=False)
    from archon.net import socket_probe
    socket_probe._flagged_peers.clear()
    evaluate(rt, [SocketPeer("149.154.167.51", 443, "ESTAB"),
                  SocketPeer("6.6.6.6", 6667, "ESTAB")])  # IRC-ish odd port
    hosts = {c.host for c in rt.net.recent(limit=10)}
    assert {"149.154.167.51", "6.6.6.6"} <= hosts
    assert _audit_actions(rt).count("egress_unexpected") == 1


def test_socket_probe_catches_connected_claim_without_socket(tmp_path):
    rt = make_rt(tmp_path)
    rt.net = NetLedger(rt, alert=False)
    rt.health["tg_userbot"] = "connected as owner"
    # No platform socket present at all -> the claim is a lie.
    evaluate(rt, [])
    assert rt.health.get("socket:tg_userbot") == "no established socket"
    assert "socket_health_mismatch" in _audit_actions(rt)
    # once a real socket appears, the mismatch clears
    evaluate(rt, [SocketPeer("149.154.167.51", 443, "ESTAB")])
    assert "socket:tg_userbot" not in rt.health
