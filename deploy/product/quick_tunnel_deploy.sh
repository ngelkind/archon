#!/usr/bin/env bash
# Bring the Archon PRODUCT service live behind an EPHEMERAL Cloudflare
# quick-tunnel (*.trycloudflare.com) — no domain, no Cloudflare account, no
# inbound ports opened. For BETA only.
#
#   Run it from your laptop, piping it to the VM:
#     ssh -i ~/.ssh/archon_vm ubuntu@84.13.79.132 'bash -s' < deploy/product/quick_tunnel_deploy.sh
#
# It runs alongside the owner's personal bot and shares nothing with it
# (separate tree /opt/archon-product, separate DB, separate port 8788).
#
# EPHEMERAL-URL CAVEAT: the *.trycloudflare.com hostname is random and CHANGES
# every time the tunnel service restarts. That is fine for Phase 1 (sign-up /
# sign-in). Once you add Google (Phase 2), a tunnel restart breaks Google OAuth
# until you re-register the new URL. A named tunnel on a real domain is the
# stable fix later; this is the free path to prove value first.
set -euo pipefail

# A non-interactive `ssh host 'bash -s'` shell does NOT source ~/.profile, so a
# user-installed uv (~/.local/bin) is off PATH. Put the usual spots back.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

APP_DIR=/opt/archon-product/app
DATA_DIR=/opt/archon-product/data
SECRETS_DIR=/opt/archon-product/secrets
ENV_FILE="$SECRETS_DIR/.env"
OWNER_ENV=/opt/archon/secrets/.env            # personal bot's env, read-only — for the Gemini key
# Private repo -> clone over SSH with the deploy key already on this box.
REPO_URL="${ARCHON_REPO_URL:-git@github.com:ngelkind/archon.git}"
BRANCH="${ARCHON_BRANCH:-feat/multitenant}"
PORT=8788
SVC_USER="$(id -un)"
DEPLOY_KEY="/home/$SVC_USER/.ssh/archon_deploy"
GIT_SSH="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
TUNNEL_UNIT=/etc/systemd/system/archon-quicktunnel.service

say(){ printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die(){ printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ "$APP_DIR" != "/opt/archon/app" ]] || die "refusing to touch the personal deployment"

# --- 1. Code -----------------------------------------------------------------
say "Fetching the code (branch: $BRANCH)"
command -v git >/dev/null 2>&1 || { sudo apt-get update -qq && sudo apt-get install -y -qq git; }
[[ -r "$DEPLOY_KEY" ]] || die "deploy key $DEPLOY_KEY missing — scp ~/.ssh/archon_deploy to the VM (see deploy/MIGRATION.md step 1)"
sudo mkdir -p /opt/archon-product
sudo chown "$SVC_USER":"$SVC_USER" /opt/archon-product
if [[ ! -d "$APP_DIR/.git" ]]; then
  GIT_SSH_COMMAND="$GIT_SSH" git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
# Pin the key into the repo config (absolute path) so EVERY later git op on this
# tree authenticates — including install.sh's fetch/pull, whatever user runs it.
git -C "$APP_DIR" config core.sshCommand "$GIT_SSH"
git -C "$APP_DIR" remote set-url origin "$REPO_URL"
git -C "$APP_DIR" fetch --all --prune -q
git -C "$APP_DIR" checkout -q "$BRANCH"
git -C "$APP_DIR" pull --ff-only -q

# --- 2. Secrets + .env (generated ONCE, never rotated on re-run) --------------
# Rotating CREDENTIAL_ENCRYPTION_KEY would orphan every stored credential, so
# an existing complete .env is preserved verbatim (this is what makes re-runs
# after Phase 2 safe — Google/Telegram keys you added are not clobbered).
say "Preparing secrets"
sudo mkdir -p "$SECRETS_DIR" "$DATA_DIR"
sudo chown -R "$SVC_USER":"$SVC_USER" "$SECRETS_DIR" "$DATA_DIR"
sudo chmod 700 "$SECRETS_DIR"; sudo chmod 750 "$DATA_DIR"

have(){ sudo grep -qE "^$1=.+" "$ENV_FILE" 2>/dev/null; }
if [[ -f "$ENV_FILE" ]] && have JWT_SECRET && have API_TOKEN_PEPPER \
    && have CREDENTIAL_ENCRYPTION_KEY && have GEMINI_API_KEY; then
  echo "  existing complete .env kept (not overwritten)"
else
  GEMINI_KEY=""
  if sudo test -r "$OWNER_ENV"; then
    GEMINI_KEY="$(sudo grep -E '^GEMINI_API_KEY=' "$OWNER_ENV" | tail -1 | cut -d= -f2- || true)"
  fi
  [[ -n "$GEMINI_KEY" ]] || die "no GEMINI_API_KEY in $OWNER_ENV — set one there or edit $ENV_FILE, then re-run"
  say "Writing a fresh $ENV_FILE (Phase 1: sign-up ready, integrations added later)"
  sudo tee "$ENV_FILE" >/dev/null <<EOF
MULTITENANT_ENABLED=true
API_ENABLED=true
API_BIND_HOST=127.0.0.1
API_PORT=$PORT
JWT_SECRET=$(openssl rand -hex 32)
API_TOKEN_PEPPER=$(openssl rand -hex 32)
CREDENTIAL_ENCRYPTION_KEY=$(openssl rand -hex 32)
ARCHON_DATA=$DATA_DIR
ARCHON_SECRETS=$SECRETS_DIR
LLM_ACTIVE_PROVIDER=gemini
GEMINI_API_KEY=$GEMINI_KEY
LLM_DAILY_BUDGET_USD=3.0
TIMEZONE=Asia/Jerusalem
EOF
fi
sudo chown "$SVC_USER":"$SVC_USER" "$ENV_FILE"; sudo chmod 600 "$ENV_FILE"

# --- 3. Service (deps + systemd) via the existing installer -------------------
command -v uv >/dev/null 2>&1 || die "uv not found for $SVC_USER — install it, then re-run"
# install.sh checks for uv as root (sudo secure_path), where a ~/.local/bin uv is
# invisible. Symlink it into a system path so root and `sudo -u` both resolve it.
sudo test -x /usr/local/bin/uv || sudo ln -sf "$(command -v uv)" /usr/local/bin/uv
say "Installing dependencies + systemd unit"
sudo ARCHON_REPO_URL="$REPO_URL" ARCHON_BRANCH="$BRANCH" bash "$APP_DIR/deploy/product/install.sh"

say "Local health check (401 = up and rejecting anonymous = correct)"
# The app runs migrations + boots subsystems before the API binds (~8s), so poll
# rather than fire once — a single immediate curl races the boot and false-fails.
code=000
for _ in $(seq 1 20); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/auth/me" 2>/dev/null || echo 000)"
  [[ "$code" == "401" ]] && break
  sleep 2
done
echo "  GET /auth/me -> $code"
[[ "$code" == "401" ]] || die "service not answering 401 on :$PORT — check: journalctl -u archon-product -n 50"

# --- 4. Cloudflare quick tunnel ----------------------------------------------
if ! command -v cloudflared >/dev/null 2>&1; then
  say "Installing cloudflared"
  arch="$(dpkg --print-architecture)"   # arm64 on the Oracle VM
  if ! (sudo mkdir -p --mode=0755 /usr/share/keyrings \
        && curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null \
        && echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null \
        && sudo apt-get update -qq && sudo apt-get install -y -qq cloudflared); then
    echo "  apt repo failed; falling back to direct .deb ($arch)"
    tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/cf.deb" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}.deb"
    sudo dpkg -i "$tmp/cf.deb"
  fi
fi

say "Installing the quick-tunnel service"
sudo tee "$TUNNEL_UNIT" >/dev/null <<EOF
[Unit]
Description=Archon product Cloudflare quick tunnel (ephemeral *.trycloudflare.com)
After=network-online.target archon-product.service
Wants=network-online.target
[Service]
User=$SVC_USER
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:$PORT
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now archon-quicktunnel.service
sudo systemctl restart archon-quicktunnel.service   # force a fresh URL on this run

say "Waiting for the public URL"
URL=""
for _ in $(seq 1 30); do
  URL="$(sudo journalctl -u archon-quicktunnel --no-pager 2>/dev/null \
         | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)"
  [[ -n "$URL" ]] && break
  sleep 2
done
[[ -n "$URL" ]] || die "tunnel came up but no URL in the journal yet — check: journalctl -u archon-quicktunnel -n 50"

say "Verifying from the public internet"
ext="$(curl -sS -o /dev/null -w '%{http_code}' "$URL/auth/me" || echo 000)"
echo "  GET $URL/auth/me -> $ext (401 = live)"

cat <<EOF

============================================================================
  ARCHON PRODUCT IS LIVE (Phase 1 — sign-up / sign-in)

    $URL

  Sign up:
    curl -sS -X POST $URL/auth/signup -H 'content-type: application/json' \\
      -d '{"email":"you@example.com","password":"a long passphrase"}'

  Phase 2 (integrations), when you are ready:
    * Google : add to $ENV_FILE
        GOOGLE_OAUTH_CLIENT_ID / _SECRET  (reuse calibot's client)
        GOOGLE_OAUTH_REDIRECT_URI=$URL/integrations/google/callback
      and register that EXACT redirect URI on the Google client.
    * Telegram userbot : add TELEGRAM_API_ID / TELEGRAM_API_HASH (my.telegram.org)
    then:  sudo systemctl restart archon-product

  Manage:
    journalctl -u archon-product -f          # app logs
    journalctl -u archon-quicktunnel -f      # tunnel + current URL
    sudo systemctl restart archon-product    # after any .env change

  NOTE: restarting archon-quicktunnel changes $URL. Back up together:
    $DATA_DIR/archon.db  AND  CREDENTIAL_ENCRYPTION_KEY (in $ENV_FILE).
============================================================================
EOF
