# Archon Control API

The HTTP/WebSocket surface the Android app is built against. It runs in-process
with the bot as one more supervised asyncio task, sharing the same SQLite
database and `Runtime`.

Everything here is generated from the implementation in `src/archon/api/`. If
this document and the code ever disagree, the code wins — start at
`api/server.py` (route table), `api/schemas.py` (DTOs) and `api/routers/`.

**Enabling it:** `API_ENABLED=true`, `API_BIND_HOST`, `API_PORT` (default
`8787`), `API_TOKEN_PEPPER` (required in production). See
[deploy/CONTROL_API.md](../deploy/CONTROL_API.md).

---

## 1. Auth & pairing

There is one identity — the owner — and a paired device is simply another key to
it. Every request carries `Authorization: Bearer <token>`.

Trust is bootstrapped from the only pre-existing trusted channel, the owner's
Telegram account. The owner runs **`/pair`** in the control bot, which mints a
6-digit code, stores only its hash, and replies with the code (TTL **10
minutes**, single use). The app posts that code to `POST /pair` and receives a
256-bit token **once** — only its hash is persisted, so a stolen database yields
no usable tokens. Both codes and tokens are hashed as
`HMAC-SHA256(api_token_pepper, secret)` (`api/security.py`), and the pepper is a
server-side secret that never leaves the VM. On every request `require_device`
(`api/auth.py`) hashes the bearer, looks up a **non-revoked** device, and
refreshes `last_seen_at`; anything missing, malformed, unknown or revoked gets
`401` with `WWW-Authenticate: Bearer`. Revocation is instant and permanent —
setting `api_devices.revoked_at` kills the token on its next use without
deleting the audit trail.

```jsonc
// POST /pair  — the only endpoint that does NOT take a bearer token
{ "code": "123456", "device_name": "Pixel 9",
  "push_endpoint": "archon-a1b2c3",   // optional: ntfy topic for push
  "device_pubkey": null }             // optional: reserved for future use

// 200 OK — the token is shown exactly once and never retrievable again
{ "token": "K7v…-256-bit-url-safe", "device_id": 1 }

// 400 Bad Request — unknown, already-used, or expired code
{ "detail": "invalid, used, or expired pair code" }
```

Redemption is atomic (`UPDATE … WHERE used_at IS NULL AND expires_at >= now`),
so a code works for exactly one device even if two try at once.

---

## 2. Endpoints

All endpoints require a bearer token **except `POST /pair`**. Unless noted,
errors are FastAPI's default `{"detail": "…"}`; query-parameter validation
failures return `422`.

### Devices

| Method | Path | Auth | Purpose | Request | Response |
|---|---|:--:|---|---|---|
| POST | `/pair` | ✗ | Redeem a pair code for a device token | `PairRequest` | `PairResponse` |

### Tools — the generic surface reaching every registered tool

| Method | Path | Auth | Purpose | Request | Response |
|---|---|:--:|---|---|---|
| GET | `/tools` | ✓ | List every owner-scoped tool | — | `ToolListResponse` |
| POST | `/tools/{name}` | ✓ | Invoke a tool by name | `ToolCallRequest` | `ToolCallResponse` |

`GET /tools` mirrors the live registry, so a tool added to the bot later appears
here automatically with no API change. `POST /tools/{name}` forwards to
`Registry.dispatch`, which is where scope checks, the audit log and the confirm
gate live — the API neither adds to nor bypasses any of them.

An unknown tool is **not** a 404. Dispatch reports it the same way it does to
the agent, inside the normal `200` envelope, so always check `result` for an
`error` key:

```jsonc
// POST /tools/does_not_exist  ->  200 OK
{ "result": "{\"error\": \"unknown tool: does_not_exist\"}" }
```

```jsonc
// ToolInfo (element of ToolListResponse.tools)
{ "name": "wa_send_message", "description": "…",
  "input_schema": { "type": "object", "properties": {…} },
  "sensitive": true }

// POST /tools/system_status
{ "args": {} }
// 200 — `result` is the tool's raw string return (usually JSON); parse per tool
{ "result": "{\"uptime_s\": 3600, \"queue_depth\": 0, \"subsystems\": {…}}" }
```

### Agent ("Mind")

| Method | Path | Auth | Purpose | Request | Response |
|---|---|:--:|---|---|---|
| POST | `/agent/chat` | ✓ | One owner turn, streamed | `ChatRequest` `{text}` | `text/event-stream` — see §3 |

Shares the same rolling context as the Telegram control bot, so the phone and
the bot are one conversation.

### Chats & gates

| Method | Path | Auth | Purpose | Request | Response |
|---|---|:--:|---|---|---|
| GET | `/chats` | ✓ | All known chats | `?platform=wa\|tg\|gmail`, `?whitelisted_only=bool` | `list[Chat]` |
| GET | `/chats/{pk}` | ✓ | One chat (`404` if unknown) | — | `Chat` |
| PATCH | `/chats/{pk}` | ✓ | Change gates (`400` if no fields) | `ChatPatch` | `ChatPatchResponse` |
| GET | `/chats/{pk}/messages` | ✓ | History, newest first | `?limit=1..500` (default 50) | `list[Message]` |

```jsonc
// Chat
{ "pk": 12, "platform": "wa", "chat_id": "…@g.us", "name": "Family",
  "kind": "group", "is_whitelisted": true, "auto_reply": false,
  "image_recognition": false, "send_policy": "confirm",
  "delay_policy": { "mode": "random", "min_s": 60, "max_s": 300 },
  "persona_id": null, "log_deletes": true, "capture_media": false,
  "last_seen_at": "2026-08-20 18:04:11" }

// ChatPatch — every field optional; only the ones present are applied
{ "is_whitelisted": true, "auto_reply": false, "image_recognition": false,
  "send_policy": "free|confirm", "log_deletes": true, "capture_media": false,
  "persona_name": "…",            // "" clears the assignment
  "delay_mode": "none|fixed|random", "delay_min_s": 60, "delay_max_s": 300 }

// ChatPatchResponse — `applied` maps each field to that tool's raw result,
// so one field failing is visible without guessing
{ "chat": { … }, "applied": { "is_whitelisted": "{\"ok\": true, …}" } }

// Message
{ "id": 991, "msg_id": "ABC123", "sender_id": "…", "sender_name": "Dana",
  "is_from_me": false, "ts": "2026-08-20 18:04:11", "text": "…",
  "media_path": null, "edited_text": null, "edited_at": null,
  "deleted_at": null }
```

Each PATCH field is routed through the same tool the agent and the Telegram bot
use (`whitelist_add`/`whitelist_remove`, `auto_reply_set`,
`image_recognition_set`, `send_policy_set`, `chat_log_policy_set`,
`capture_add`/`capture_remove`, `persona_assign`, `delay_policy_set`), so a gate
flipped from the phone is audited identically to one flipped by voice.

### Config

| Method | Path | Auth | Purpose | Request | Response |
|---|---|:--:|---|---|---|
| GET | `/config` | ✓ | All settings, secrets redacted | — | `ConfigResponse` |
| PUT | `/config/{key}` | ✓ | Set one setting | `ConfigPut` `{value}` | `ToolCallResponse` |

`value` is any JSON value. Keys containing `.key.` or starting with `llm.key`
read back as `"•••"` — provider API keys are never returned.

### Contacts

| Method | Path | Auth | Purpose | Request | Response |
|---|---|:--:|---|---|---|
| GET | `/contacts` | ✓ | Directory + counts | `?limit=1..2000` (default 500) | `ContactsResponse` |
| POST | `/contacts/sync` | ✓ | Push the device address book | `ContactSyncRequest` | `ContactSyncResponse` |

```jsonc
// POST /contacts/sync  (at most 1000 entries per call are processed)
{ "contacts": [ { "name": "Dana", "phone": "+972501234567" } ] }
// 200 — entries with a blank name or phone are skipped, not errors
{ "submitted": 1, "stored": 1, "skipped": 0 }
```

Send only new or changed entries: each one becomes an audited `contact_remember`
tool call.

### Schedules

| Method | Path | Auth | Purpose | Request | Response |
|---|---|:--:|---|---|---|
| GET | `/schedules` | ✓ | Scheduled messages, newest first | `?limit=1..200` (default 50) | `list[Schedule]` |
| DELETE | `/schedules/{id}` | ✓ | Cancel a pending one | — | `ToolCallResponse` |

Cancelling a message Telegram scheduled natively must be done in the Telegram
app; `result` reports `{"ok": false}` for anything not in `pending`.

### Costs

| Method | Path | Auth | Purpose | Request | Response |
|---|---|:--:|---|---|---|
| GET | `/costs` | ✓ | LLM spend + breakdown | `?window=day\|week\|month` (default `day`) | `CostsResponse` |

```jsonc
{ "window": "day",
  "total": { "calls": 12, "cost_usd": 0.0431, "in_tokens": 8100, "out_tokens": 900 },
  "breakdown": [ { "provider": "gemini", "model": "gemini-3.6-flash",
                   "purpose": "agent", "calls": 9, "cost_usd": 0.0402 } ] }
```

### Approvals (the confirm gate)

| Method | Path | Auth | Purpose | Request | Response |
|---|---|:--:|---|---|---|
| GET | `/approvals` | ✓ | Actions awaiting a decision | `?status=pending` (default) | `list[Approval]` |
| POST | `/approvals/{id}/decision` | ✓ | Approve or reject | `ApprovalDecision` `{ok}` | `ApprovalDecisionResponse` |

```jsonc
// Approval — note: `kind` here is the ACTION type (see §4 on naming)
{ "id": 7, "kind": "wa.send", "payload": { … }, "chat_pk": 12,
  "status": "pending", "created_at": "2026-08-20 18:00:00",
  "expires_at": "2026-08-20 19:00:00" }

// POST /approvals/7/decision  { "ok": true }
{ "status": "approved", "detail": "sent to Dana", "ok": true }
```

`status` is one of `approved` · `rejected` · `expired` · `already` · `unknown`.
Decisions are **single-use across every channel**: the claim is one atomic SQL
update, so if the owner taps Approve in Telegram at the same moment the phone
does, exactly one runs the action and the other receives `already`. `ok: false`
with `status: "approved"` means the action was claimed but its executor failed —
`detail` carries the error. Actions expire 60 minutes after creation.

### Status & stream

| Method | Path | Auth | Purpose | Request | Response |
|---|---|:--:|---|---|---|
| GET | `/status` | ✓ | Uptime, subsystem health, provider | — | `StatusResponse` |
| WS | `/stream` | ✓ | Realtime events | `?token=` (or header) | see §4 |

```jsonc
// StatusResponse — `health` keys are subsystem names (control_bot, pipeline,
// gmail, whatsapp, tg_userbot, scheduler, subbots, api, …)
{ "uptime_s": 86400, "health": { "pipeline": "running", "api": "running" },
  "active_provider": "gemini" }
```

---

## 3. `POST /agent/chat` (SSE)

Send `{"text": "…"}` and read `text/event-stream`. Frames are emitted by
`api/sink.py` as a named SSE event plus one JSON `data:` line:

Response `Content-Type` is `text/event-stream; charset=utf-8`. A real exchange,
copied verbatim from the wire:

```
event: tool_call
data: {"id": "toolu_01", "name": "calendar_create_event", "args": {"title": "Dentist"}}

event: tool_result
data: {"id": "toolu_01", "name": "calendar_create_event", "result": "{\"ok\": true}"}

event: final
data: {"text": "Done - added Dentist."}
```

Sequence: zero or more `tool_call` / `tool_result` pairs (matched by `id`, in
dispatch order), then **exactly one** terminal frame — either `final` or
`error`. The stream closes after it.

| Event | `data` fields |
|---|---|
| `tool_call` | `id`, `name`, `args` (object) |
| `tool_result` | `id`, `name`, `result` (the tool's raw string) |
| `final` | `text` — the assistant's reply |
| `error` | `message` — content-free provider error (e.g. budget exhausted) |

On `error` the turn is **not** written to the conversation context, so retrying
the same text is safe. The turn is saved only when `final` is sent.

---

## 4. `/stream` (WebSocket)

Authenticate with `Authorization: Bearer <token>` on the upgrade request, or
`?token=<token>` when the client cannot set headers. The header is preferred.
A failed check closes the socket with code **1008** (policy violation) — there
is no HTTP error body to read. The client sends nothing; the server unsubscribes
automatically on disconnect.

Every message is one JSON envelope:

```jsonc
{ "id": 42,                    // monotonic per process; use it to dedupe
  "kind": "approval.pending",  // the EVENT type
  "ts": 1787253840.63,         // unix seconds, float
  "data": { … } }              // per-kind, below
```

> **Naming, important:** the envelope's event type is **`kind`**. Because an
> approval *also* has a type of its own, that one is called **`action_kind`**
> inside `data` — never `data.kind`. (`/approvals` REST responses are unaffected
> and still use `kind` for the action type.)

| `kind` | `data` fields | Emitted when |
|---|---|---|
| `approval.pending` | `action_id`, `action_kind`, `description`, `chat_pk` | An action needs a decision |
| `approval.resolved` | `action_id`, `action_kind`, `status`, `actor`, `ok` (approved only) | Any channel decides, or it expires |
| `health.change` | `subsystem`, `state` | A subsystem changes state (transitions only, not a heartbeat) |
| `message.new` | `chat_pk`, `platform`, `chat_id`, `msg_id`, `sender` | An inbound message is cached |
| `cost.update` | `provider`, `model`, `purpose`, `cost_usd` | An LLM call completes |
| `owner.alert` | `source` | An owner alert fires (e.g. `tg_notify_owner`) |

`approval.resolved.status` is `approved` · `rejected` · `expired`, and `actor`
identifies the channel (`telegram`, `api:device:<id>`) — useful for suppressing
a local echo.

**`message.new` carries identifiers only — never message text.** Fetch the
content with `GET /chats/{chat_pk}/messages`. `owner.alert` likewise carries only
a short internal `source` label, never the alert text. Keeping content out of
the fan-out is what lets the same vocabulary be reused for push.

Delivery is **best-effort, drop-oldest**: each subscriber has a 200-event queue
and the oldest is discarded when it fills, so a slow or backgrounded phone can
never stall the message pipeline or the LLM path. Treat the stream as a hint to
refresh, not as a ledger — reconcile via REST on reconnect, and use `id` to
dedupe against push.

---

## 5. Push (ntfy)

Enabled by `NTFY_BASE_URL` (plus optional `NTFY_AUTH_TOKEN`); unset means push
is off and every push path is a no-op. The topic is the device's
`push_endpoint`, registered at pairing. Revoked devices stop receiving pushes.

The wire format is ntfy's **JSON publish**: one POST to the **base URL** — not
`{base}/{topic}` — with the topic in the body.

```jsonc
// POST http://<ntfy-host>:8080
// Authorization: Bearer <NTFY_AUTH_TOKEN>   (only when configured)
{ "topic": "archon-a1b2c3",
  "title": "Approval requested",
  "message": "wa.send",                 // the action TYPE, never its content
  "tags": ["lock", "action:42"] }       // the action_id lives HERE
```

Owner alerts use the same shape with `title: "Archon alert"`,
`message: "Open Archon to read it."`, `tags: ["warning"]`.

**Read the `action_id` from the `action:<id>` tag.** ntfy forwards only its own
documented headers (Title, Priority, Tags, Click, Actions, Icon, Delay,
Markdown, Filename, Email, Template), so a custom header such as `X-Action-Id`
would be silently dropped before reaching the device — the tag is the reliable
channel. Then fetch `GET /approvals` (or act via
`POST /approvals/{id}/decision`) over the tunnel.

**Content-free guarantee.** A push carries a fixed title, the action *type*, and
an opaque id. The description, the correspondent, and any message body never
leave the VM through this path — regardless of where the ntfy instance runs. The
app is expected to fetch details over the authenticated tunnel on tap.

Push is best-effort by contract: 5-second timeout, network errors swallowed into
a `push_failed` audit note, HTTP ≥400 into `push_rejected`. A dead broker never
propagates into the confirm gate — the pending action is still recorded and
still decidable from Telegram or the app.

---

## 6. Security notes

The API binds to **`api_bind_host`** — loopback by default, and the WireGuard
interface address on the VM. It is **never** bound to `0.0.0.0`, so it is
unreachable off-tunnel and the "no discoverable service" posture holds: a port
scan of the public IP shows only SSH and the silent WireGuard UDP port. The
tunnel authenticates the pipe; the bearer token authenticates the request.
Interactive docs and the OpenAPI schema are disabled (`docs_url`, `redoc_url`,
`openapi_url` are all `None`) — this file is the contract instead.

Setup, systemd units and the peer/topic workflow live in
[deploy/CONTROL_API.md](../deploy/CONTROL_API.md).
