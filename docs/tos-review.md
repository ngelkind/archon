# Terms-of-Service / legal review — Archon

**Status: PoC. Do not launch any platform integration to third parties before a
lawyer has cleared the rows below.** This document is the single catalogue of
every place in the code that touches a platform's Terms of Service, a platform's
API data-use policy, or a third party's expectations about their own messages.
Each site also carries an inline `# TODO(TOS-REVIEW): …` marker so it is visible
in context; this file is the index a reviewer reads end to end.

Marker format used in the code:

```
# TODO(TOS-REVIEW): <platform> — <risk> — review before launch
```

## Context the reviewer needs

- **WhatsApp is the highest risk and is expected to be dropped.** WhatsApp/Meta
  do not permit unofficial clients. The owner is in contact with someone at Meta
  and wants a *working proof of concept to demonstrate*, but the working
  assumption is that this integration will most likely be removed. Every
  WhatsApp row below is built on `neonize`/`whatsmeow` (an unofficial
  reimplementation of the WhatsApp Web protocol) plus a **patched build** that
  adds a `SendPeerMessage` export and forces an Android-phone device identity.
  There is a clean off switch (`whatsapp.enabled` / `WHATSAPP_ENABLED`).
- **Telegram** automation via a *user account* (userbot, MTProto) is restricted;
  the Bot API and Telegram Business connections are the sanctioned surfaces. A
  **shared `api_id`** across tenants is the systemic risk if this ever runs
  multi-tenant (see `integrations/telegram_userbot.py`).
- **Google (Gmail/Calendar)** uses *restricted scopes*. That pulls in Google's
  **Limited Use** requirements and a **CASA** security assessment, and forbids
  sending user data to a third party except as the user directs. Gmail bodies
  are sent to a third-party LLM for triage/drafting — the central Limited-Use
  question.
- **Undisclosed AI authorship.** The bot can draft and send replies *as the
  owner* with no indication a model wrote them. Several jurisdictions and
  platform policies are moving to require disclosure.
- **Ephemeral content.** The bot intentionally captures view-once and
  self-destruct (TTL) media and republishes others' deleted/edited messages to a
  log channel — i.e. it defeats the sender's expectation that the content
  disappears. This is a privacy/consent issue independent of any single ToS.
- **Retention.** Third-party message content is cached indefinitely with no
  expiry policy today.

## How to use this list

1. A lawyer classifies each row: allowed / allowed-with-changes / not allowed.
2. For "allowed-with-changes", the change (disclosure text, retention window,
   consent capture, scope reduction, off by default) is implemented and the
   inline marker updated or removed.
3. For "not allowed", the feature is gated off before launch (WhatsApp already
   has its switch; others may need one).

## Critical — gate before any external launch

| Area | File | Risk |
|---|---|---|
| WhatsApp device spoof | `platforms/whatsapp/client.py` (`_android_props`) | Presents as an Android phone companion so the server delivers phone-only media. |
| WhatsApp patched build | `deploy/build_goneonize.sh` | Injects a `SendPeerMessage` export into a patched whatsmeow/neonize (reads media with no visible receipt). |
| WhatsApp view-once | `platforms/whatsapp/events.py` (`unwrap_view_once`) | Unwraps view-once media so it can be stored/forwarded, defeating one-view intent. |
| Telegram TTL media | `platforms/telegram/userbot.py` (`_ephemeral_kind`, `_capture_ephemeral`) | Detects and saves self-destruct media the sender meant to vanish. |
| Telegram retractions | `logging_/tglog.py` (`render_card`) | Republishes another user's deleted/edited message into the owner's log channel. |
| Store before gate | `pipeline/ingest.py` (`message_upsert` in `run`) | Caches third-party content before the monitoring gate decides to act. |
| Gmail → third-party LLM | `pipeline/ingest.py` (`triage` call) | Sends message content incl. Gmail bodies to an external LLM (Google Limited Use). |
| WhatsApp product integration | `integrations/whatsapp.py` (`consent_notice`) | A WhatsApp integration Meta's Terms do not permit for unofficial clients. |

## High

| Area | File | Risk |
|---|---|---|
| WhatsApp revoke/edit intercept | `platforms/whatsapp/events.py` | Logs content a sender withdrew (delete/edit). |
| WhatsApp simulated typing | `platforms/whatsapp/sender.py` (`send_text`) | Fakes human typing presence before an automated send. |
| WhatsApp read receipts | `platforms/whatsapp/sender.py` (`mark_read`) | Programmatic send/suppress of read receipts on an unofficial client. |
| WhatsApp /download re-send | `platforms/whatsapp/download_cmd.py` | Downloads third-party media and re-sends it as the owner. |
| WhatsApp pairing | `platforms/whatsapp/pairing.py`, `scripts/wa_pair.py` | Pairs a forged Android-phone identity over an unofficial client. |
| Telegram userbot / shared api_id | `integrations/telegram_userbot.py` | User-account automation over MTProto; shared api_id is a systemic risk. |
| Telegram Business ingest | `platforms/telegram/business.py` | Ingests private-chat messages both directions; can send as the connected account. |
| Undisclosed AI replies | `pipeline/ingest.py` (`_auto_reply`), `agent/prompts.py` (persona) | Sends AI-drafted replies as the owner with no disclosure. |
| Indefinite retention | `db/repo.py` (`message_upsert`) | Third-party message content kept with no expiry. |
| Ephemeral capture | `logging_/capture.py` | Captures view-once / self-destruct media. |
| YouTube bot-check bypass | `platforms/downloader.py`, `platforms/telegram/download_cmd.py`, `tools/media.py` | Player-client args + cookies to bypass YouTube's bot check; download/redistribution. |
| Restricted Gmail scopes | `platforms/google_auth.py`, `scripts/google_consent.py`, `platforms/gmail/poller.py`, `tools/email_.py` | Restricted scopes → CASA + Limited Use; full-inbox ingest; send as the owner. |

## Medium / low

| Area | File | Risk |
|---|---|---|
| Audit content on disk | `logging_/audit.py` | Writes third-party message text to a local audit log when `store_content` is on. |
| Capture toggle | `tools/capture.py` | Owner can enable ephemeral-media capture per chat. |
| Web fetch | `tools/websearch.py` | Fetches arbitrary pages server-side; respect fetched sites' robots/ToS. |
| Remote image attach | `tools/scheduling.py` | Downloads arbitrary remote images to attach to sends. |
| Message content over API | `api/routers/chats.py` (`chat_messages`) | Exposes stored third-party content via the API. |
| LLM transport | `llm/claude_code.py` | Routes user content through the Claude Code subscription transport. |
| Self-test targets | `selftest.py` | Hardcoded test number / username / TikTok URL; move to settings, confirm consent. |

## Conceptual items (no single line — policy decisions)

- **Monitor-everything defaults.** The current default triages all private chats
  (including the owner's own messages) and logs deletes/edits for all groups.
  Whether that default is acceptable, or should be opt-in, is a policy call.
- **Media cache & re-attach.** Cached media is re-attached to deletion cards, so
  a deleted photo still surfaces — same ephemeral-content concern as above.
- **Log outbox.** Retracted content is queued for delivery to the log channel;
  confirm the retention/visibility of that queue.
- **Network ledger (`netlog.py`).** Records host + path metadata (never bodies)
  of the owner's own traffic. Single-user PoC; revisit for multi-tenant.

## Not a concern (recorded so the reviewer sees it was considered)

- **Socket probe (`net/socket_probe.py`).** Reads only this process's own socket
  table via an unprivileged `ss`; touches no third-party data or platform API.
