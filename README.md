# Archon

An autonomous personal assistant agent. It ingests WhatsApp, Telegram and Gmail,
decides on its own what deserves a reply, drafts and sends one as the owner,
and turns messages into Google Calendar events — operated end to end from a
Telegram control bot. One asyncio process on a small Linux VM, no inbound ports.

**What this project is about, technically:** an agent that is allowed to act on a
real person's real messages. Most of the engineering here is the part that makes
that safe — a triage stage that decides *whether* to act at all, a ~60-tool
catalog with validated inputs and honest failures, a confirmation flow for
anything irreversible, per-tenant scoping, a network ledger that observes every
outbound request and alarms on unexpected egress, PII redaction in the audit log,
and a three-layer test harness that runs the real runtime against fake transports.

| | |
|---|---|
| Scale | ~114 commits, Python 3.12, asyncio |
| Agent | triage classifier → tool-loop agent → personas/contexts, ~60 tools |
| LLM | 5 provider backends, per-purpose routing, budget + cost tracking, vision fallback |
| Safety | egress tripwire, audit outbox, confirmation sweep, tenant isolation |
| Deploy | OCI provisioning, cloud-init, systemd, CI, live post-deploy probes |

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
