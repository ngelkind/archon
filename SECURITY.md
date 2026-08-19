# Security

## Data-flow guarantee

Message content leaves the machine only to:

1. Each platform's OWN servers (Telegram DCs, WhatsApp servers, googleapis.com)
   — inherent to using those platforms.
2. The single **active** LLM provider's API, and only for content that passed
   the whitelist gate.

Nothing else. Web search (when enabled) sends only LLM-generated queries via
the LLM provider's native search. The optional DuckDuckGo fallback is OFF by
default.

## Dependency audit log

| Dependency | License | Talks to | Verified |
|---|---|---|---|
| neonize 0.4.3.post0 (pinned) | Apache-2.0 | WhatsApp only | 2026-08-20: Python layer has zero live endpoints; strings-scan of the Go binary shows only whatsapp.com/wa.me hosts. Hardening: goneonize is rebuilt from source on the VM (M5). |
| Telethon | MIT | Telegram DCs only | pure-Python MTProto; endpoint-grep at pin time |
| aiogram | MIT | api.telegram.org only | endpoint-grep at pin time |
| google-api-python-client / google-auth-oauthlib | Apache-2.0 | googleapis.com / accounts.google.com | Google-official |
| anthropic / openai / google-genai | MIT/Apache | their own APIs | intended LLM egress |
| httpx / pydantic-settings | BSD/MIT | nothing on their own | infrastructure |

## Rules

- `uv.lock` pins everything with hashes; installs are `uv sync --frozen` only.
- Upgrading any dependency requires: download sdist → grep for
  endpoints/`subprocess`/`eval`/base64 blobs → record findings here → bump.
- CI contains no third-party actions that can see secrets; deploys are plain
  `ssh` with a pinned `known_hosts`.
- Runtime egress monitor on the VM alerts the owner (via the control bot)
  about connections to unexpected hosts.
- Secrets: never in git; `/opt/archon/secrets` is 700, files 600.
- New chats default to confirm-before-send; audit log records every gate
  decision and tool call.
- All untrusted text (messages, emails, vision output, web content) is
  wrapped via `wrap_untrusted()` before reaching a model, and models never
  get settings/admin tools on runs triggered by platform content.
