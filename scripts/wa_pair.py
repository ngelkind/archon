"""One-time WhatsApp device pairing for the VM (phone-code method).

Usage:  wa_pair.py <international_number_no_plus>   e.g. 972555000001

Prints ``PAIRCODE: XXXXXXXX`` — type that into WhatsApp on your phone:
Settings -> Linked Devices -> Link a device -> Link with phone number instead.
Exits once paired. The session is written to /opt/archon/secrets/wa/session.db.
"""

from __future__ import annotations

import sys
import threading
import time

from neonize.client import NewClient
from neonize.events import ConnectedEv, PairStatusEv

SESSION = "/opt/archon/secrets/wa/session.db"


def _tablet_props():
    from neonize.proto.waCompanionReg import WAWebProtobufsCompanionReg_pb2 as reg

    return reg.DeviceProps(os="iPad", platformType=reg.DeviceProps.IPAD,
                           requireFullSync=False)


def main() -> None:
    phone = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        client = NewClient(SESSION, props=_tablet_props())
    except TypeError:
        client = NewClient(SESSION)
    done = threading.Event()

    @client.event(ConnectedEv)
    def _on_conn(_c, _e):  # type: ignore[no-untyped-def]
        print("PAIRED_OK: connected", flush=True)
        done.set()

    @client.event(PairStatusEv)
    def _on_pair(_c, e):  # type: ignore[no-untyped-def]
        print(f"PAIR_STATUS: {e}", flush=True)

    def request_code() -> None:
        for _ in range(40):
            try:
                code = client.PairPhone(phone, True)
                print(f"PAIRCODE: {code}", flush=True)
                return
            except Exception as exc:  # noqa: BLE001
                time.sleep(1.5)
        print(f"PAIR_FAILED: {exc!r}", flush=True)  # noqa: F821

    if phone:
        threading.Thread(target=request_code, daemon=True).start()

    # Exit shortly after pairing succeeds so the session file is released
    # before the archon service takes it over.
    def _watch() -> None:
        done.wait()
        time.sleep(5)
        print("EXIT: session saved", flush=True)
        import os
        os._exit(0)

    threading.Thread(target=_watch, daemon=True).start()
    client.connect()


if __name__ == "__main__":
    main()
