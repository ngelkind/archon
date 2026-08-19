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
| `deploy/` | OCI provisioning, cloud-init, systemd units, migration runbook |
