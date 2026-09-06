"""WhatsApp ingestion through the REAL neonize handlers with real protobufs.

The six handlers used to be closures inside ``client.run`` — including the
three fatal-event handlers and the view-once stub route — and no test had ever
fired one. These scenarios drive them through ``FakeAClient`` and assert on
the pipeline's effects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archon.db import repo
from archon.testing import fake_neonize as wa
from archon.testing.harness import Harness
from archon.testing.scripted_llm import ScriptedProvider

from conftest import run_async

pytestmark = pytest.mark.e2e

GROUP = "120363418193527511@g.us"
DANA = "972500000002@s.whatsapp.net"
DANA_LID = "117184542068754@lid"


def _client(**kw) -> wa.FakeAClient:
    return wa.FakeAClient(groups=[wa.FakeGroup(GROUP, "kkk")],
                          lid_to_phone={DANA_LID: DANA}, **kw)


async def _start(tmp_path, client, script=None, **settings) -> Harness:
    h = await Harness.start(tmp_path, subsystems=("pipeline", "whatsapp"),
                            neonize=client, script=script, settings=settings or None)
    await h.wait_for(lambda: h.rt.clients.get("whatsapp") is client, what="client registered")
    await client.go_online()
    await h.wait_for_audit("wa_groups_synced")
    return h


@run_async
async def test_connect_syncs_groups_and_reports_connected(tmp_path):
    client = _client()
    async with await _start(tmp_path, client) as h:
        assert h.audit("wa_groups_synced")[-1]["count"] == 1
        row = h.row("SELECT name, kind FROM chats WHERE chat_id=?", (GROUP,))
        assert (row["name"], row["kind"]) == ("kkk", "group")
        assert h.rt.health["whatsapp"] == "connected"
        assert set(client.handlers) >= {wa.ne.MessageEv, wa.ne.LoggedOutEv,
                                        wa.ne.UndecryptableMessageEv}


@run_async
async def test_group_text_is_cached_and_gated(tmp_path):
    client = _client()
    async with await _start(tmp_path, client) as h:
        await client.fire(wa.text_message(GROUP, "dentist tomorrow 15:00", msg_id="M1"))
        gate = await h.wait_for_audit("not_whitelisted", chat=GROUP)
        assert gate["allowed"] is False and gate["platform"] == "wa"
        cached = h.row("SELECT text, sender_id, is_from_me FROM messages WHERE msg_id='M1'")
        assert cached["text"] == "dentist tomorrow 15:00" and cached["sender_id"] == DANA
        assert h.llm.requests == []


@run_async
async def test_whitelisted_group_reaches_triage(tmp_path):
    client = _client()
    async with await _start(tmp_path, client, ScriptedProvider().triage("ignore")) as h:
        h.chat("wa", GROUP, whitelisted=True)
        await client.fire(wa.text_message(GROUP, "anyone up for lunch?"))
        note = await h.wait_for_audit("triage", chat=GROUP)
        assert note["platform"] == "wa" and note["verdict"] == "ignore"


@run_async
async def test_lid_sender_is_whitelisted_through_its_phone_number(tmp_path):
    """A contact writes from their @lid; the owner whitelisted the phone JID.
    The gate resolves the LID to the phone, honours the whitelist, and links
    the LID row so the next message is instant."""
    client = _client()
    async with await _start(tmp_path, client, ScriptedProvider().triage("ignore")) as h:
        h.chat("wa", DANA, kind="private", whitelisted=True)
        await client.fire(wa.text_message(DANA_LID, "hi from my lid", sender=DANA_LID))
        gate = await h.wait_for_audit("whitelisted_via_lid", chat=DANA_LID)
        assert gate["allowed"] is True
        assert h.audit("wa_whitelist_linked", lid=DANA_LID, phone=DANA)
        assert h.row("SELECT is_whitelisted FROM chats WHERE chat_id=?", (DANA_LID,))[0] == 1
        await h.wait_for_audit("triage", chat=DANA_LID)


@run_async
async def test_edit_and_revoke_update_the_cache(tmp_path):
    client = _client()
    async with await _start(tmp_path, client) as h:
        await client.fire(wa.text_message(GROUP, "meet at 5", msg_id="E1"))
        await h.wait_for_audit("not_whitelisted", chat=GROUP)
        await client.fire(wa.edit_message(GROUP, "E1", "meet at 6"))
        await h.wait_for_audit("edit_or_delete_event", chat=GROUP)
        row = h.row("SELECT text, edited_text, deleted_at FROM messages WHERE msg_id='E1'")
        assert (row["text"], row["edited_text"], row["deleted_at"]) == ("meet at 5", "meet at 6", None)

        await client.fire(wa.revoke_message(GROUP, "E1"))
        await h.wait_for_audit("edit_or_delete_event", chat=GROUP, count=2)
        assert h.row("SELECT deleted_at FROM messages WHERE msg_id='E1'")["deleted_at"]


@run_async
async def test_view_once_in_an_armed_chat_is_downloaded_and_kept_when_unpostable(tmp_path):
    """Container route. No log bot is wired in this harness, so the post
    cannot happen — and the decrypted bytes must survive on disk, because
    they are the only copy of a one-time item."""
    client = _client()
    async with await _start(tmp_path, client) as h:
        pk = h.chat("wa", GROUP)
        repo.chat_set_field(h.rt.db, pk, "capture_media", 1)
        await client.fire(wa.image_message(GROUP, view_once=True, msg_id="VO1"))
        note = await h.wait_for_audit("capture_no_channel", chat=GROUP)
        assert client.downloads, "download_any was never asked for the inner media"
        kept = Path(note["kept"])
        assert kept.exists() and kept.read_bytes() == client.media
        inbound = h.audit("wa_inbound")[-1]
        assert inbound["ephemeral"] is True and inbound["media"] == ["image"]


@run_async
async def test_view_once_in_an_unarmed_chat_is_left_alone(tmp_path):
    client = _client()
    async with await _start(tmp_path, client) as h:
        await client.fire(wa.image_message(GROUP, view_once=True, msg_id="VO2"))
        await h.wait_for_audit("no_text", chat=GROUP)
        assert client.downloads == []
        assert h.audit("capture_no_channel") == []


@run_async
async def test_a_failed_post_keeps_the_captured_file(tmp_path):
    """The old finally-clause unlinked the file even when the send failed."""
    client = _client()
    async with await _start(tmp_path, client) as h:
        class _DeadBot:
            async def send_photo(self, *a, **k):
                raise RuntimeError("telegram is down")
        h.rt.clients["notifier"] = _DeadBot()
        repo.setting_set(h.rt.db, "capture.all_dms", True)
        await client.fire(wa.image_message(DANA, view_once=True, msg_id="VO3"))
        note = await h.wait_for_audit("capture_send_failed", chat=DANA)
        assert Path(note["kept"]).exists()
        assert h.audit("throttled_send_failed")


@run_async
async def test_withheld_stub_is_noticed_only_when_armed(tmp_path):
    client = _client()
    async with await _start(tmp_path, client) as h:
        await client.fire(wa.view_once_stub(DANA, msg_id="S1"))
        await h.wait_for_audit("wa_capture_disarmed", chat=DANA, route="stub")
        assert h.audit("wa_viewonce_stub", chat=DANA, msg_id="S1")

        repo.setting_set(h.rt.db, "capture.all_dms", True)
        await client.fire(wa.view_once_stub(DANA, msg_id="S2"))
        await h.wait_for_audit("capture_no_channel", chat=DANA)


@run_async
async def test_quoted_view_once_with_keys_is_recovered(tmp_path):
    client = _client()
    async with await _start(tmp_path, client) as h:
        repo.setting_set(h.rt.db, "capture.all_dms", True)
        original = wa.image_message(DANA, view_once=True).Message  # the inner pb.Message
        await client.fire(wa.quoting_message(DANA, "nice one", original))
        any_note = await h.wait_for_audit("wa_quoted_any", chat=DANA)
        assert any_note["recoverable"] is True and any_note["is_container"] is True
        await h.wait_for_audit("wa_quoted_vo_captured", chat=DANA)

        stripped = wa.image_message(DANA, view_once=True, keys=False).Message
        await client.fire(wa.quoting_message(DANA, "again", stripped))
        await h.wait_for_audit("wa_quoted_any", chat=DANA, count=2)
        assert h.audit("wa_quoted_any")[-1]["recoverable"] is False
        assert len(h.audit("wa_quoted_vo_captured")) == 1


@run_async
async def test_logged_out_ends_the_session_with_a_terminal_health_state(tmp_path):
    client = _client()
    async with await _start(tmp_path, client) as h:
        await client.fire(wa.logged_out())
        await h.wait_for_audit("wa_logged_out")
        await h.wait_for(lambda: h.rt.health["whatsapp"].startswith("LOGGED OUT"),
                         what="terminal health")
        # The supervised task returned; the state string survives it.
        await h.wait_for(lambda: h.tasks["whatsapp"].done(), what="subsystem exit")
        assert h.rt.health["whatsapp"].startswith("LOGGED OUT")


@pytest.mark.xfail(strict=True, reason="lifecycle commit: health must follow ConnectedEv, not connect()")
@run_async
async def test_health_does_not_claim_connected_before_the_server_says_so(tmp_path):
    client = _client()
    h = await Harness.start(tmp_path, subsystems=("pipeline", "whatsapp"), neonize=client)
    try:
        await h.wait_for(lambda: h.rt.clients.get("whatsapp") is client, what="client")
        await h.wait_for(lambda: client.connect_task is not None, what="connect() called")
        # connect() returned its task; no ConnectedEv has fired.
        assert h.rt.health["whatsapp"] != "connected"
    finally:
        await h.stop()


@run_async
async def test_wa_mark_read_uses_the_real_receipt_signature(tmp_path):
    """The tool used to pass a list positionally with no sender; neonize's
    mark_read(*ids, chat=, sender=, receipt=) rejected every call."""
    from archon.tools.registry import ToolContext

    client = _client()
    async with await _start(tmp_path, client) as h:
        other = "972500000003@s.whatsapp.net"
        await client.fire(wa.text_message(GROUP, "one", msg_id="R1", sender=DANA))
        await client.fire(wa.text_message(GROUP, "two", msg_id="R2", sender=other))
        await h.wait_for_audit("not_whitelisted", chat=GROUP, count=2)
        ctx = ToolContext(rt=h.rt, scope="owner")
        out = await h.rt.registry.dispatch(ctx, "wa_mark_read", {"chat_jid": GROUP})
        assert '"marked": 2' in out
        receipts = {(r["sender"], tuple(r["ids"])) for r in client.read}
        assert receipts == {(DANA, ("R1",)), (other, ("R2",))}
        assert all(r["chat"] == GROUP for r in client.read)


@run_async
async def test_download_command_sends_before_it_revokes(tmp_path, monkeypatch):
    from archon.platforms import downloader
    from archon.platforms.whatsapp import download_cmd

    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 10)

    async def fake_download(url, media_dir, max_bytes=None):
        return downloader.DownloadedVideo(path=str(video), title="clip", duration_s=3,
                                          width=1, height=1, size_bytes=10, extractor="fake")

    monkeypatch.setattr(download_cmd.downloader, "download", fake_download)
    client = _client()
    async with await _start(tmp_path, client) as h:
        await client.fire(wa.text_message(GROUP, "/download https://example.com/v",
                                          sender=wa.OWNER_JID, from_me=True, msg_id="CMD1"))
        await h.wait_for_audit("wa_download_revoked", msg_id="CMD1")
        assert client.sent[-1]["video"] == str(video)
        assert client.revoked == [{"chat": GROUP, "sender": wa.OWNER_JID, "id": "CMD1"}]
        assert h.audit("download_sent")[-1]["jid"] == GROUP

        # A failed upload leaves the command in place (no revoke).
        async def broken_send(*a, **k):
            raise RuntimeError("upload failed")

        client.send_video = broken_send  # type: ignore[method-assign]
        await client.fire(wa.text_message(GROUP, "/download https://example.com/w",
                                          sender=wa.OWNER_JID, from_me=True, msg_id="CMD2"))
        await h.wait_for_audit("wa_download_send_failed", jid=GROUP)
        assert len(client.revoked) == 1


@run_async
async def test_an_edit_of_an_uncached_message_is_audited(tmp_path):
    client = _client()
    async with await _start(tmp_path, client) as h:
        await client.fire(wa.edit_message(GROUP, "NEVER-SEEN", "new text"))
        note = await h.wait_for_audit("change_target_unknown", msg_id="NEVER-SEEN")
        assert note["kind"] == "edit" and note["platform"] == "wa"
