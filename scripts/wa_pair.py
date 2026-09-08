"""WhatsApp pairing for the VM — QR mode, registering as an iPad companion.

Registering as an iPad (not a Web/Desktop browser) is what makes WhatsApp
deliver view-once media to this companion. Phone-code pairing isn't offered
for the tablet platform, so we use QR: the QR is rendered to a PNG that the
operator relays to the phone (WhatsApp -> Link a device -> scan).

Prints ``QR_SAVED n`` each time a fresh QR is written (they rotate ~every 20s)
and ``PAIRED_OK`` once linked. Session -> /opt/archon/secrets/wa/session.db.
"""

from __future__ import annotations

import threading
import time

import segno
from neonize.client import NewClient
from neonize.events import ConnectedEv, PairStatusEv

SESSION = "/opt/archon/secrets/wa/session.db"
QR_PNG = "/opt/archon/secrets/wa/pair_qr.png"


# TODO(TOS-REVIEW): WhatsApp — pairs an unofficial WhatsApp client presenting a spoofed device — confirm the account owner consents — review before launch
def _android_props():
    # Pair as an Android PHONE so WhatsApp delivers view-once media to this
    # companion (the goneonize build forces the matching UserAgent.Platform +
    # drops WebInfo). QR pairing is required — pairing-code is rejected under
    # the Android identity.
    from neonize.proto.waCompanionReg import WAWebProtobufsCompanionReg_pb2 as reg

    return reg.DeviceProps(os="Android", platformType=reg.DeviceProps.ANDROID_PHONE,
                           requireFullSync=False)


def _refuse_if_service_running() -> None:
    """Two clients on one session file fight (StreamReplaced) and can corrupt
    it. The running bot has /wa_pair for this; use that instead."""
    import shutil
    import subprocess
    import sys

    if shutil.which("systemctl") is None:
        return
    active = subprocess.run(["systemctl", "is-active", "--quiet", "archon"], check=False)
    if active.returncode == 0:
        sys.exit("archon.service is running — send /wa_pair to the control bot instead, "
                 "or stop the service first.")


def main() -> None:
    _refuse_if_service_running()
    try:
        client = NewClient(SESSION, props=_android_props())
    except TypeError:
        client = NewClient(SESSION)
    done = threading.Event()
    counter = {"n": 0}

    @client.qr
    def _on_qr(_c, data):  # type: ignore[no-untyped-def]
        text = data.decode() if isinstance(data, (bytes, bytearray)) else str(data)
        try:
            segno.make(text, error="m").save(QR_PNG, scale=9, border=3)
            counter["n"] += 1
            print(f"QR_SAVED {counter['n']}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"QR_ERROR: {exc!r}", flush=True)

    @client.event(ConnectedEv)
    def _on_conn(_c, _e):  # type: ignore[no-untyped-def]
        print("PAIRED_OK: connected", flush=True)
        done.set()

    @client.event(PairStatusEv)
    def _on_pair(_c, e):  # type: ignore[no-untyped-def]
        print(f"PAIR_STATUS: {e}", flush=True)

    def _watch():  # type: ignore[no-untyped-def]
        done.wait()
        time.sleep(5)
        print("EXIT: session saved", flush=True)
        import os
        os._exit(0)

    threading.Thread(target=_watch, daemon=True).start()
    client.connect()


if __name__ == "__main__":
    main()
