# Testing Archon

Three layers, each proving something the layer below cannot. All of L1 runs
offline in CI with no network and no keys; L3 runs on the VM against real
platforms.

```
uv run pytest                      # everything offline (unit + L1 e2e)
uv run pytest -m e2e               # just the L1 end-to-end scenarios
uv run pytest -m "not e2e"         # just the fast unit layer
```

`-m live` selects the L3 probe tests, which are skipped everywhere except the
VM (they need real sessions). CI never runs them.

## L1 — simulated-platform end-to-end (`src/archon/testing/`, `tests/e2e/`)

Boots the REAL runtime — the same `app._wire_llm_and_tools`, pipeline, tools,
registry, DB and supervisor the bot runs — with FAKE transports swapped in:

- `fake_bot_api.py` — a local aiohttp server aiogram polls for real (the control
  bot's dispatcher, filters and handlers all run unchanged).
- `fake_telethon.py` / `fake_neonize.py` — the Telegram userbot and WhatsApp
  event shapes, constructed from the real protobufs.
- `scripted_llm.py` — a deterministic provider; a call nobody scripted is
  recorded and fails the run, so a scenario can never pass on an answer nobody
  wrote.

Scenarios assert on **effects** — an audit event, a DB row, a Bot API call, a
ledger entry — never on "it didn't raise". The harness (`harness.py`) exposes
`owner_says`, `wait_for_audit`, `rows`, `audit`, and boots the same network
ledger the product does, so an e2e test also asserts the run stayed on loopback
(`tests/e2e/net_assert.py::only_expected_hosts`) — the CI net-hygiene gate.

Write a new scenario by starting a `Harness`, driving an inbound message or an
owner command, and waiting for the effect:

```python
async with await Harness.start(tmp_path, subsystems=("pipeline", "control_bot"),
                               bot_api=True) as h:
    await h.owner_says("/status")
    assert_call(h.rt, subsystem="telegram", method="POST")
```

## L2 — the network ledger (`src/archon/netlog.py`, `src/archon/net/`)

Records metadata (never bodies or query strings) for every outbound request the
httpx / aiohttp / httplib2 hooks can see, plus a socket probe for the raw-socket
protocols (Telethon MTProto, neonize) no hook observes. Surfaces: `/netstat`
(control bot), `GET /net/summary` and `/net/recent` (API), a `/status` line.

An unexpected host trips the egress alarm (`egress_unexpected` audit + owner
alert). `net_assert.assert_call` / `assert_no_call_to` / `only_expected_hosts`
let a test assert on the wire. Honest gap: google-genai exposes no http hook, so
Gemini's own API traffic shows up only in the socket probe and the VM tap.

## L3 — live probes (`src/archon/probe/`)

Drives inbound from a SEPARATE test account and asserts on an OBSERVED effect —
a DB row, a Calendar entry, a card read back from the log channel — never on
"the call returned". Runs on the VM only, gated by `probe.enabled` and a check
that the test session is distinct from the owner's.

Run it: `/livetest [names]` from the control bot, `live [names]` written to
`data/cmd.trigger` over SSH, or `POST /probe`. Results land in
`data/probe.result.json` and `GET /probe/last`. A dry run
(`probe.dry_run=1`) exercises the wiring with no network.

**One-time inputs the owner provides before a live run:** the test account's
Telethon `StringSession` (`PROBE_TELETHON_SESSION`), its own
`PROBE_TELEGRAM_API_ID` / `PROBE_TELEGRAM_API_HASH` (never the owner's), and —
for the WhatsApp probe — the paired test-number session.
