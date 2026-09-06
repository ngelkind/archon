"""Out-of-band sends survive what the live bot has actually hit: flood control
(TelegramRetryAfter) and a dead notifier connection ("Connector is closed").
Scenarios I and I2."""

from __future__ import annotations

import asyncio
import time

import pytest

from archon import alerts
from archon.logging_.send import throttle_for, throttled_send
from archon.testing.harness import Harness

from conftest import run_async

pytestmark = pytest.mark.e2e

OWNER = 1


async def _start(tmp_path) -> Harness:
    h = await Harness.start(tmp_path, subsystems=("control_bot",), bot_api=True)
    await h.wait_for_audit("control_bot_started")
    return h


def _send(h: Harness, text: str, kind: str = "test"):
    return throttled_send(h.rt, lambda b: b.send_message(OWNER, text), kind=kind)


@run_async
async def test_flood_control_is_waited_out_not_dropped(tmp_path):
    async with await _start(tmp_path) as h:
        h.bot_api.refuse_next("sendMessage", status=429, retry_after=1)
        h.bot_api.refuse_next("sendMessage", status=429, retry_after=1)
        result = await _send(h, "card after flood", kind="log_card")
        assert result is not None
        assert h.bot_api.texts(chat_id=OWNER) == ["card after flood"]
        floods = h.audit("throttled_send_flood", kind="log_card")
        assert len(floods) == 2 and floods[0]["retry_after_s"] == 2.0
        assert h.audit("throttled_send_gaveup") == []


@run_async
async def test_a_dead_connection_rebuilds_the_notifier(tmp_path):
    """The old 'refresh' fetched the same cached Bot with the same dead
    session, so every retry failed identically."""
    async with await _start(tmp_path) as h:
        before = h.rt.clients["notifier"]
        h.bot_api.refuse_next("sendMessage", drop=True)
        result = await _send(h, "after reconnect", kind="owner_alert")
        assert result is not None
        assert h.rt.clients["notifier"] is not before
        assert h.audit("throttled_send_network_error", kind="owner_alert")
        assert h.audit("notifier_rebuilt")[-1]["rebuilds"] == 1
        assert h.bot_api.texts(chat_id=OWNER) == ["after reconnect"]


@run_async
async def test_giving_up_is_loud_and_recovery_clears_it(tmp_path):
    async with await _start(tmp_path) as h:
        for _ in range(4):
            h.bot_api.refuse_next("sendMessage", drop=True)
        assert await _send(h, "doomed", kind="capture") is None
        gaveup = h.audit("throttled_send_gaveup", kind="capture")[-1]
        assert gaveup["attempts"] == 4 and "ServerDisconnected" in gaveup["last_error"]
        assert h.rt.health["notifier"] == "send failures (1 in a row)"
        assert throttle_for(h.rt).consecutive_gaveups == 1

        assert await _send(h, "back") is not None
        assert "notifier" not in h.rt.health
        assert throttle_for(h.rt).consecutive_gaveups == 0


@run_async
async def test_a_flood_wait_does_not_stall_other_senders(tmp_path):
    """The RetryAfter sleep used to run under the send lock."""
    async with await _start(tmp_path) as h:
        h.bot_api.refuse_next("sendMessage", status=429, retry_after=2)
        slow = asyncio.create_task(_send(h, "slow one"))
        await h.wait_for_audit("throttled_send_flood")
        started = time.monotonic()
        assert await _send(h, "quick one") is not None
        assert time.monotonic() - started < 1.5, "the quick send waited for the flood sleep"
        assert await slow is not None
        assert set(h.bot_api.texts(chat_id=OWNER)) == {"slow one", "quick one"}


@run_async
async def test_owner_alert_survives_a_dead_notifier(tmp_path):
    async with await _start(tmp_path) as h:
        h.bot_api.refuse_next("sendMessage", drop=True)
        assert await alerts.alert_owner(h.rt, "k", "WhatsApp logged out") is True
        assert h.audit("owner_alert_sent", key="k")
        assert h.bot_api.texts(chat_id=OWNER) == ["WhatsApp logged out"]
