# Archon

Personal AI assistant. Reads WhatsApp / Telegram / Gmail, auto-creates Google
Calendar events from messages, and is controlled entirely through a Telegram
bot. Runs as one asyncio process on a small Linux VM; no inbound ports except
SSH (Telegram long polling, outbound-only connections everywhere).

- **Security model**: see [SECURITY.md](SECURITY.md). Short version: every
  dependency is open source and talks only to its own platform's servers;
  message content leaves the machine only toward the active LLM provider,
  and only for whitelisted chats.
- **Setup / migration**: see `deploy/MIGRATION.md`.
- **Development**: `uv sync --group dev`, copy `.env.example` → `.env`,
  `uv run -m archon`. Tests: `uv run pytest`.
- **Operating it** (control commands, `/status`, alerts, re-pairing, off
  switches, deploy): see [docs/operations.md](docs/operations.md).
- **Testing it** (the three-layer harness / network ledger / live probes): see
  [docs/testing.md](docs/testing.md).
- **Legal / ToS review**: see [docs/tos-review.md](docs/tos-review.md).

## Layout

| Path | What |
|---|---|
| `src/archon/platforms/` | WhatsApp (neonize), Telegram (control bot + Business + Telethon userbot), Gmail |
| `src/archon/pipeline/` | normalize → cache → gate → triage → agent |
| `src/archon/agent/` | triage classifier, tool-loop agent, personas, contexts |
| `src/archon/llm/` | 5 provider backends, single-active-provider router, cost tracking |
| `src/archon/tools/` | the ~60-tool catalog the agent operates with |
| `src/archon/scheduler/` | scheduled messages + per-chat reply delays |
| `src/archon/logging_/` | audit (JSONL+DB), PII redaction, deleted/edited-message log channel |
| `src/archon/netlog.py`, `src/archon/net/` | network ledger: every outbound request + a socket probe, with an egress tripwire |
| `src/archon/probe/` | live-probe runner: inbound from a test account, asserted on observed effects (VM only) |
| `src/archon/testing/`, `tests/e2e/` | offline end-to-end harness: real runtime, fake transports, scripted LLM |
| `deploy/` | OCI provisioning, cloud-init, systemd units, goneonize build, network tap, migration runbook |
