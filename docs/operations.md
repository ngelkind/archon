# Operating Archon

Everything is driven from the owner's Telegram control bot. This is the field
guide: what the commands do, what `/status` is telling you, how alerts reach
you, how to re-pair a platform, and how to turn things off.

## Control commands

| Command | What it does |
|---|---|
| `/status` | Subsystem health, queue depth, whitelist counts, LLM provider, and a one-line network summary. This is the first thing to read. |
| `/netstat` | Recent outbound connections per subsystem and any unexpected hosts. |
| `/chats` | Every chat with flags: ✅ whitelisted, 📝 delete/edit logging, 🤖 auto-reply. |
| `/whitelist`, `/unwhitelist` | Turn triage on/off for a chat by name or id. Groups need this to be triaged. |
| `/monitor` | `monitor all\|whitelist [groups]` — what gets triaged. Default: all private chats, whitelisted groups. |
| `/logall` | `logall on\|off` — edit/delete logging for all groups at once. |
| `/approvals` | Pending confirmation cards awaiting your tap. |
| `/costs` | LLM spend over 24h / 7d / 30d. |
| `/ask` | Ask the agent a question directly. |
| `/download <url>` | Download a video and send it. |
| `/wa_pair` | Re-pair WhatsApp: the bot posts a QR; scan it from the linked phone. |
| `/pair` | Pair a new control device. |
| `/selftest [steps]` | Outbound self-test (sends to the test targets). |
| `/livetest [names]` | Inbound live probes from the test account (VM only). |

## Reading `/status`

Each subsystem reports a short state. Healthy is `running` (or `watching` for
pollers). The states that mean "deliberately idle, not broken":

- `disabled: …` — off by configuration (e.g. WhatsApp off, or the socket probe
  on a non-Linux box). Will not restart; nothing wrong.
- `no session` / `not configured` / `no token` — a credential is absent.

The states that mean "down and will NOT come back on its own" (you are alerted
once, with force): `LOGGED OUT`, `TEMPORARY BAN`, `SESSION INVALID`,
`NOT PAIRED`, `STREAM REPLACED`. A WhatsApp `LOGGED OUT` means run `/wa_pair`.

Anything reading `crashed: …` or `stopped unexpectedly` is being restarted with
backoff; you are alerted after the second consecutive failure and at most hourly
after that. `egress: unexpected host …` means the network ledger saw a
connection to a host outside the allowlist — check `/netstat`.

## Alerts

The bot messages you (throttled, at most hourly per issue) on: repeated
subsystem crashes, terminal platform states, LLM provider failing, a tool bug,
unexpected egress, and triage repeatedly failing. Alerts raised before the bot
is up are queued and flushed once it connects.

## Re-pairing and off switches

- **WhatsApp**: `/wa_pair` stops the subsystem, QR-pairs, and restarts it. Turn
  the whole integration off with `whatsapp.enabled=0` (or `WHATSAPP_ENABLED=0`)
  — the wa_* tools then disappear from the agent and the subsystem stands down.
- **Telegram userbot**: session invalid → re-provision the `TELETHON_SESSION`.
- **Google**: `scripts/google_consent.py` mints the combined-scope token.
- **Monitoring**: `/monitor` and `/logall`, or per-chat `/whitelist`, all take
  effect without a restart.

## Deploy

Push to `main` runs the tests, then over SSH: `git reset --hard`,
`uv sync --frozen`, **rebuild goneonize from source** (`deploy/build_goneonize.sh`
— this MUST run after `uv sync`, which reinstalls the prebuilt .so), restart
`archon`, and a post-deploy live-smoke gate. Timers: nightly SQLite backup,
egress monitor every 10 min, and (once enabled) the nightly network tap + live
probes.

The VM runs in UTC; the bot renders times in `settings.timezone`
(`Asia/Jerusalem`). `ss -Htunp` for our own pid works unprivileged, which is
what the socket probe and `deploy/egress-monitor.sh` rely on.

## Legal / ToS

Before exposing any integration to third parties, read
[tos-review.md](tos-review.md): WhatsApp is the highest risk (unofficial client,
expected to be dropped), Google restricted scopes carry Limited Use / CASA, and
AI-authored replies go out undisclosed. Every risk site also carries an inline
`# TODO(TOS-REVIEW)` marker.
