"""/wa_pair: re-pair WhatsApp from the control chat, no SSH, no service stop.

The subsystem is unpaired (terminal state), the owner types /wa_pair, QR codes
arrive as photos as they rotate, the server accepts the device, the pairing
client releases the session file and the subsystem comes back connected.
"""

from __future__ import annotations

import pytest

from archon.platforms.whatsapp import client as wa_client
from archon.platforms.whatsapp import pairing
from archon.testing import fake_neonize as wa
from archon.testing.harness import Harness

from conftest import run_async

pytestmark = pytest.mark.e2e

OWNER = 1


@run_async
async def test_wa_pair_walks_the_owner_through_a_qr_and_restarts_whatsapp(tmp_path, monkeypatch):
    monkeypatch.setattr(wa_client, "CONNECT_TIMEOUT_S", 0.3)
    monkeypatch.setattr(pairing, "SETTLE_S", 0.05)
    unpaired = wa.FakeAClient(logged_in=False)
    unpaired.connected_flag = True
    pair_client = wa.FakeAClient(logged_in=True)
    fresh = wa.FakeAClient(groups=[wa.FakeGroup("1@g.us", "g")], logged_in=True)
    handed_out = iter([unpaired, pair_client, fresh])

    async with await Harness.start(tmp_path, subsystems=("control_bot",), bot_api=True) as h:
        h.rt.factories["wa_client"] = lambda _rt: next(handed_out)
        await h.wait_for_audit("control_bot_started")
        # Registered the way app.main does it: the factory builds a client per run.
        h.supervise("whatsapp", lambda: wa_client.run(h.rt))
        await h.wait_for_audit("subsystem_terminal", subsystem="whatsapp")
        assert h.rt.health["whatsapp"].startswith("NOT PAIRED")

        await h.owner_says("/wa_pair")  # the "Stopping WhatsApp…" instructions
        await h.wait_for(lambda: pair_client.connect_task is not None, what="pairing client")
        assert h.rt.health["whatsapp"] == "pairing"

        await pair_client.emit_qr(b"2@first,ref,key")
        await pair_client.emit_qr(b"2@second,ref,key")
        await h.bot_api.wait_for_call("sendPhoto", chat_id=OWNER, count=2)
        photos = h.bot_api.calls_of("sendPhoto", chat_id=OWNER)
        assert photos[-1].data["caption"].startswith("WhatsApp QR #2")
        assert photos[-1].files and photos[-1].files["photo"][:8] == b"\x89PNG\r\n\x1a\n"

        await pair_client.go_online()  # the server accepted the device
        done = await h.wait_for_audit("wa_pair_done")
        assert done["status"] == "paired" and done["qr_count"] == 2
        assert pair_client.stopped, "the pairing client must release session.db"
        reply = await h.bot_api.wait_for_call("sendMessage", chat_id=OWNER, count=3)
        assert reply.data["text"].startswith("✅ WhatsApp pairing: paired")

        # The subsystem was restarted on a fresh client and connects normally.
        await h.wait_for(lambda: fresh.connect_task is not None, what="restarted subsystem")
        await fresh.go_online()
        await h.wait_for_audit("wa_groups_synced")
        assert h.rt.health["whatsapp"] == "connected"
        assert h.rt.clients["whatsapp"] is fresh


@run_async
async def test_wa_pair_times_out_cleanly_and_reports_it(tmp_path, monkeypatch):
    monkeypatch.setattr(pairing, "PAIR_TIMEOUT_S", 0.2)
    pair_client = wa.FakeAClient(logged_in=False)
    after = wa.FakeAClient(logged_in=False)
    after.connected_flag = True
    handed_out = iter([pair_client, after])
    monkeypatch.setattr(wa_client, "CONNECT_TIMEOUT_S", 0.2)
    async with await Harness.start(tmp_path, subsystems=("control_bot",), bot_api=True) as h:
        h.rt.factories["wa_client"] = lambda _rt: next(handed_out)
        await h.wait_for_audit("control_bot_started")
        await h.owner_says("/wa_pair")
        done = await h.wait_for_audit("wa_pair_done")
        assert done["status"] == "timeout" and done["qr_count"] == 0
        assert pair_client.stopped
        reply = await h.bot_api.wait_for_call("sendMessage", chat_id=OWNER, count=2)
        assert reply.data["text"].startswith("⚠️ WhatsApp pairing: timeout")
        # And the subsystem is back under supervision (here: still unpaired).
        await h.wait_for_audit("subsystem_terminal", subsystem="whatsapp")


@run_async
async def test_a_second_wa_pair_while_one_runs_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(pairing, "PAIR_TIMEOUT_S", 0.6)
    pair_client = wa.FakeAClient(logged_in=False)
    async with await Harness.start(tmp_path, subsystems=("control_bot",), bot_api=True) as h:
        h.rt.factories["wa_client"] = lambda _rt: pair_client
        await h.wait_for_audit("control_bot_started")
        await h.owner_says("/wa_pair")
        await h.wait_for(lambda: pairing.in_progress(h.rt), what="pairing in progress")
        reply = await h.owner_says("/wa_pair")
        assert "already running" in reply
        await h.wait_for_audit("wa_pair_done")
