# Enabling the Android control API (WireGuard + ntfy)

This turns on the in-process control API (`src/archon/api/`) and exposes it to
your phone **without adding a discoverable inbound service**. The only inbound
change is one UDP port for WireGuard, which is silent to unauthenticated
packets; the API binds to the tunnel interface, so it is unreachable off-tunnel.

Everything here runs **on the VM (84.13.79.132), by you, after review**. Nothing
in this repo touches the live VM automatically.

## Security shape
- **WireGuard**: one UDP port (51820). No handshake without your key → invisible
  to scanners. Split-tunnel: the phone routes only `10.13.13.1/32` (the VM)
  through it; the rest of its traffic is untouched.
- **API bind**: `API_BIND_HOST=10.13.13.1` (the wg0 address) → only wg peers can
  connect. Bearer tokens on top (stored as HMAC+pepper hashes, single-use pair
  codes, instant `revoked_at` kill).
- **ntfy**: bound to `10.13.13.1:8080` (wg-only). Push payloads are content-free
  — the app is woken with an opaque `action_id` and fetches details over the
  tunnel. No message text ever leaves the VM through push.
- **Egress monitor**: unaffected — no new outbound hosts (peers dial *in*; ntfy
  is local). No allowlist change needed.

## One-time, before deploy (on your workstation)
1. `feat/control-api` added `fastapi` + `uvicorn` to `pyproject.toml`, but the
   lockfile is stale (this machine had no `uv`). Regenerate and commit:
   ```
   uv lock            # updates uv.lock with fastapi/uvicorn
   git add uv.lock pyproject.toml && git commit -m "chore: lock control-api deps"
   ```
   CI runs `uv sync --frozen`; without this the deploy fails.
2. Merge `feat/control-api` (and the app work) when you're ready. Pushing to
   `main` auto-deploys — so do steps 3–6 first on a run where the API stays
   disabled (`API_ENABLED` defaults to false), then flip it on.

## On the VM
3. **WireGuard**:
   ```
   sudo deploy/wireguard/setup_wireguard.sh        # installs, keys, opens UDP 51820
   ```
   Then open **UDP 51820 ingress** in the OCI security list / NSG (console or the
   `oci` CLI snippet the script prints). Keep the existing SSH rule.
4. **Pair your phone as a peer** → prints a client config + QR the app imports:
   ```
   sudo deploy/wireguard/add_peer.sh pixel
   ```
5. **ntfy**:
   ```
   sudo deploy/ntfy/setup_ntfy.sh
   ```
6. **Enable the API** — add to `/opt/archon/secrets/.env` (note: `.env.example`
   is gitignored, so these are not delivered by git — set them by hand):
   ```
   API_ENABLED=true
   API_BIND_HOST=10.13.13.1
   API_PORT=8787
   API_TOKEN_PEPPER=<paste 32+ random bytes, e.g. `openssl rand -hex 32`>
   NTFY_BASE_URL=http://10.13.13.1:8080
   # NTFY_AUTH_TOKEN=<optional — only if you turn on ntfy auth>
   ```
   The exact push wire-format (ntfy JSON publish; `action_id` in an
   `action:<id>` tag) is defined in `src/archon/api/push.py` — the source of
   truth the app parses against.
   Then `sudo systemctl restart archon`.

## In the app
7. Onboarding: import the WireGuard config from step 4 (scan the QR), then send
   `/pair` to your control bot (`@YourControlBot`) and enter the 6-digit code. The
   app exchanges it for a device token and registers its ntfy topic.

## Verify
- From the phone (tunnel up): the app's `/status` loads; a confirm-gated action
  shows an approval card and, with the app closed, an ntfy push wakes it.
- From anywhere else: `nmap` the public IP → only SSH open; UDP 51820 shows
  filtered/no-response. `ss -ltnp` on the VM shows the API bound to `10.13.13.1`,
  not `0.0.0.0`.
- Approve once from the phone and once from Telegram on the same action → the
  second reports "already handled" (single-use claim).

## Rollback
- `API_ENABLED=false` + restart disables the API (bot unaffected).
- Revoke a device: delete its `[Peer]` block from `/etc/wireguard/wg0.conf`,
  `sudo wg syncconf wg0 <(wg-quick strip wg0)`, and set `revoked_at` on its
  `api_devices` row (or re-pair).
