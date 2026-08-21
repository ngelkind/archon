# Archon product deploy (multi-tenant, public HTTPS via Cloudflare Tunnel)

Brings up the **product** service — accounts, per-tenant data, Google/Telegram/
WhatsApp linking — reachable on a public HTTPS hostname, with **no inbound
ports opened**.

It runs **alongside** the owner's personal bot and shares nothing with it:
separate systemd unit, separate database, separate data directory, separate
port. The personal bot keeps its WireGuard-only API on `10.13.13.1:8787` and is
not modified by anything here.

> **The one detail that breaks deploys:** `GOOGLE_OAUTH_REDIRECT_URI` must be
> the **public tunnel hostname + `/integrations/google/callback`**, and the
> byte-identical string must be registered on the Google OAuth client. Google
> compares it verbatim. A mismatch does not fail at boot — it fails with
> `redirect_uri_mismatch` the first time a user tries to link Google.

---

## 0. Before you start

You need:

- A domain on Cloudflare (any plan, including free) — say `archon.example.com`.
- A Google Cloud project with an OAuth client of type **Web application**.
- A **new** Telegram bot from [@BotFather](https://t.me/BotFather) — *not* the
  owner's personal control bot. Product users connect this one as their
  Business chatbot.
- Outbound access to Cloudflare on **TCP/UDP 7844** (`cloudflared` dials out;
  nothing is opened inbound).

Generate the three secrets now — you will paste them in step 2:

```sh
for k in JWT_SECRET API_TOKEN_PEPPER CREDENTIAL_ENCRYPTION_KEY; do
  printf '%s=%s\n' "$k" "$(openssl rand -hex 32)"
done
```

> **Back up `CREDENTIAL_ENCRYPTION_KEY` with the database, not in it.** It wraps
> every tenant's Google refresh token, Telegram connection and WhatsApp session.
> Lose it and every user must re-link every integration; a database restore
> without it is useless.

---

## 1. Install the service

```sh
sudo ARCHON_REPO_URL=<git-url> ARCHON_BRANCH=feat/multitenant \
  bash deploy/product/install.sh
```

Creates `/opt/archon-product/{app,data,secrets}`, syncs dependencies from the
lockfile, installs a template `.env`, and — once the required secrets are
filled in — enables `archon-product.service`.

Re-run it any time to update; it never overwrites an existing `.env`.

## 2. Fill in the environment

Edit `/opt/archon-product/secrets/.env` (mode `600`). Full annotated list:
[`deploy/product/env.example`](env.example). Minimum to boot:

```ini
MULTITENANT_ENABLED=true
API_ENABLED=true
API_BIND_HOST=127.0.0.1
API_PORT=8788

JWT_SECRET=<openssl rand -hex 32>
API_TOKEN_PEPPER=<openssl rand -hex 32>
CREDENTIAL_ENCRYPTION_KEY=<openssl rand -hex 32>

ARCHON_DATA=/opt/archon-product/data
ARCHON_SECRETS=/opt/archon-product/secrets

LLM_ACTIVE_PROVIDER=gemini
GEMINI_API_KEY=<key>
```

Then integrations (needed before those features work, not to boot):

```ini
GOOGLE_OAUTH_CLIENT_ID=<...>
GOOGLE_OAUTH_CLIENT_SECRET=<...>
GOOGLE_OAUTH_REDIRECT_URI=https://archon.example.com/integrations/google/callback

PRODUCT_TELEGRAM_BOT_TOKEN=<from BotFather>
TELEGRAM_BOT_USERNAME=ArchonProductBot
```

Restart and confirm:

```sh
sudo systemctl restart archon-product
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/auth/me   # 401 = correct
journalctl -u archon-product -n 50 --no-pager
```

**A `401` here is the success signal** — the API is up and rejecting an
unauthenticated request. A connection refusal means it did not start; check the
journal. The service **refuses to boot** if any of the three secrets is unset or
still at its in-repo default — that is deliberate, so a public listener can
never run on a known key.

Expected health on a fresh product box (`/status`, or the journal):

| Subsystem | Expected | Meaning |
|---|---|---|
| `control_bot` | `disabled (no owner Telegram credentials)` | correct — no owner here |
| `whatsapp` | `no session (…)` | correct — the *owner's* client; tenants use their own |
| `tg_userbot` | `not configured (…)` | correct — deliberately not offered to tenants |
| `pipeline`, `scheduler`, `subbots` | `running` | |
| `gmail` | `polling N tenant(s)` | runs even with no owner token |
| `product_bot` | `polling @YourBot` | |
| `api` | `running` | |

## 3. Cloudflare Tunnel

Installs `cloudflared`, creates a named tunnel, points a hostname at it, and
runs it as a service. Commands below follow Cloudflare's
[locally-managed tunnel guide](https://developers.cloudflare.com/tunnel/advanced/local-management/create-local-tunnel/).

**Install** (Debian/Ubuntu, from Cloudflare's package repo):

```sh
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
```

**Authenticate and create the tunnel** (as your normal user, not root — the
credentials land in `~/.cloudflared`):

```sh
cloudflared tunnel login          # opens a browser; pick the zone
cloudflared tunnel create archon-product
```

That prints a **tunnel UUID** and writes `~/.cloudflared/<UUID>.json`.

**Route the hostname** (creates the CNAME to `<UUID>.cfargotunnel.com`):

```sh
cloudflared tunnel route dns archon-product archon.example.com
```

**Write `~/.cloudflared/config.yml`:**

```yaml
tunnel: <UUID>
credentials-file: /home/ubuntu/.cloudflared/<UUID>.json

ingress:
  - hostname: archon.example.com
    service: http://127.0.0.1:8788
  # Required final catch-all.
  - service: http_status:404
```

Validate before installing the service:

```sh
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://archon.example.com/auth/me
```

**Run as a service:**

```sh
sudo cloudflared --config /home/ubuntu/.cloudflared/config.yml service install
sudo systemctl start cloudflared
sudo systemctl status cloudflared
```

> **Gotcha, straight from Cloudflare's docs:** run under `sudo`, `$HOME` is
> `/root`, so `cloudflared` will not find a config in `/home/<user>/.cloudflared`.
> Pass `--config` explicitly, as above. Skipping this is the usual cause of a
> service that starts but routes nothing.
>
> After editing `config.yml`: `sudo systemctl restart cloudflared`.

**Verify from the outside:**

```sh
curl -sS -o /dev/null -w '%{http_code}\n' https://archon.example.com/auth/me   # 401
```

## 4. Google OAuth client

In Google Cloud Console → **APIs & Services → Credentials** → your Web
application client, add to **Authorized redirect URIs**, exactly:

```
https://archon.example.com/integrations/google/callback
```

and set the identical string as `GOOGLE_OAUTH_REDIRECT_URI` in `.env`. Enable
the **Gmail API** and **Google Calendar API** on the project.

The consent screen starts as an **unverified test app**, capped at **100 test
users**, which is fine for beta. Gmail and Calendar are *restricted* scopes, so
general availability needs Google verification plus a CASA security assessment —
weeks of lead time, so start it before you need it.

Restart after changing `.env`: `sudo systemctl restart archon-product`.

## 5. Telegram product bot

With @BotFather, on the **product** bot:

- `/setinline` off, `/setjoingroups` off (it is a 1:1 assistant).
- Business mode must be enabled for users to select it under
  **Settings → Telegram Business → Chatbots**.

Users then: link in the app → send `/start <CODE>` to the bot → connect it in
Business settings.

> **Unresolved:** whether connecting a Business chatbot requires the *user* to
> have Telegram Premium. The docs conflict and it needs a real account to
> settle. The code works either way — a Premium-gated user simply never
> produces a connection update, and the Bot API front door (DM the bot) still
> works. Confirm this before promising Business features in the app copy.

## 6. Smoke test the public surface

```sh
BASE=https://archon.example.com
curl -sS -X POST $BASE/auth/signup -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"a long passphrase"}'
# -> {"access_token": "...", ...}
TOKEN=<paste>
curl -sS $BASE/auth/me -H "Authorization: Bearer $TOKEN"
curl -sS -X POST $BASE/integrations/google/authorize -H "Authorization: Bearer $TOKEN"
# -> {"authorize_url": "https://accounts.google.com/...", "state": "..."}
```

Open that `authorize_url` in a browser, consent, and you should land on the
callback page saying the account is linked.

## 7. Operations

```sh
sudo systemctl status archon-product     # product service
journalctl -u archon-product -f
sudo systemctl status cloudflared        # the tunnel
sudo systemctl restart archon-product    # after any .env change
```

**Back up together, always:** `/opt/archon-product/data/archon.db` **and**
`CREDENTIAL_ENCRYPTION_KEY`. The database alone cannot decrypt a single
integration credential.

### Known limits at this stage

- **WhatsApp message flow is not wired yet** (see task #12). Linking, consent,
  session storage and isolation are in place; the inbound/send event handlers
  and the pairing QR need a live session to validate and are deliberately not
  claimed as working.
- **Gmail polling is serial across tenants** on one interval in one process, so
  latency grows with tenant count. Gmail push (Pub/Sub) is the real fix and is
  now possible — this deploy provides the public HTTPS endpoint it needs.
- **SQLite, one writer.** Fine for a beta; concurrent tenants doing agent turns
  serialise. Postgres is the planned move.
- **Rate limits are per process.** Correct on one box; they become per-replica
  the moment there is more than one.
- **WhatsApp sessions bound capacity**, not CPU or the database. Each holds a Go
  runtime, a websocket and a SQLite handle. Measure real RSS per session on this
  box before setting a beta user cap.
