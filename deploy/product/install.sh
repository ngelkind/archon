#!/usr/bin/env bash
# Install / update the Archon PRODUCT service (multi-tenant).
#
# Run ON THE VM as a user with sudo. Idempotent: safe to re-run to update.
#
# This installs a SECOND, independent service alongside the owner's personal
# bot. It never touches /opt/archon (the personal deployment) — different tree,
# different database, different systemd unit. That separation is the point: a
# product incident must not be able to take the owner's assistant down.
set -euo pipefail

APP_DIR=/opt/archon-product/app
DATA_DIR=/opt/archon-product/data
SECRETS_DIR=/opt/archon-product/secrets
SERVICE_USER="${SUDO_USER:-$(id -un)}"
REPO_URL="${ARCHON_REPO_URL:-}"
BRANCH="${ARCHON_BRANCH:-feat/multitenant}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"

say "Refusing to clobber the personal deployment"
[[ "$APP_DIR" != "/opt/archon/app" ]] || die "APP_DIR must not be the personal bot's"

say "Creating directories"
mkdir -p "$APP_DIR" "$DATA_DIR" "$SECRETS_DIR"
chown -R "$SERVICE_USER":"$SERVICE_USER" /opt/archon-product
# Secrets hold every tenant's encrypted integration credentials and, while a
# WhatsApp session is live, its plaintext working file.
chmod 700 "$SECRETS_DIR"
chmod 750 "$DATA_DIR"

say "Fetching the code (branch: $BRANCH)"
if [[ -d "$APP_DIR/.git" ]]; then
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" fetch --all --prune
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" checkout "$BRANCH"
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" pull --ff-only
elif [[ -n "$REPO_URL" ]]; then
  sudo -u "$SERVICE_USER" git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  die "no checkout at $APP_DIR and ARCHON_REPO_URL is unset"
fi

say "Installing Python dependencies"
command -v uv >/dev/null 2>&1 || die "uv not found — install it first"
# --frozen so a deploy can never silently resolve different versions than the
# lockfile. If this fails, the lockfile is stale: fix it in the repo, not here.
sudo -u "$SERVICE_USER" env -C "$APP_DIR" uv sync --frozen

say "Checking the environment file"
if [[ ! -f "$SECRETS_DIR/.env" ]]; then
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 \
    "$APP_DIR/deploy/product/env.example" "$SECRETS_DIR/.env"
  cat <<'MSG'

  A template .env was installed. It is NOT yet usable — fill in:
    MULTITENANT_ENABLED=true, API_ENABLED=true
    JWT_SECRET, API_TOKEN_PEPPER, CREDENTIAL_ENCRYPTION_KEY  (openssl rand -hex 32)
    an LLM provider key
    GOOGLE_OAUTH_* (redirect URI = https://<public-host>/integrations/google/callback)
    PRODUCT_TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME

  The service FAILS TO START on placeholder secrets, by design.

MSG
else
  chmod 600 "$SECRETS_DIR/.env"
  echo "  existing .env kept (not overwritten)"
fi

say "Pre-flight: refusing to install a service that cannot boot"
missing=()
for var in MULTITENANT_ENABLED JWT_SECRET API_TOKEN_PEPPER CREDENTIAL_ENCRYPTION_KEY; do
  value="$(grep -E "^${var}=" "$SECRETS_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  [[ -n "$value" ]] || missing+=("$var")
done
if (( ${#missing[@]} )); then
  echo "  NOT enabling the service — unset: ${missing[*]}"
  echo "  Fill them in, then re-run this script."
  exit 0
fi

say "Installing the systemd unit"
install -m 644 "$APP_DIR/deploy/product/archon-product.service" \
  /etc/systemd/system/archon-product.service
# The unit hardcodes User=ubuntu; match it to the actual service user.
sed -i "s/^User=.*/User=${SERVICE_USER}/; s/^Group=.*/Group=${SERVICE_USER}/" \
  /etc/systemd/system/archon-product.service
systemctl daemon-reload
systemctl enable archon-product.service
systemctl restart archon-product.service

say "Status"
sleep 3
systemctl --no-pager --lines=20 status archon-product.service || true

cat <<'MSG'

Next:
  1. Confirm the API answers locally:
       curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/auth/me
     401 is CORRECT here (no token) — it proves the API is up and auth is on.
  2. Set up the tunnel: see deploy/product/PRODUCT_DEPLOY.md
  3. Logs: journalctl -u archon-product -f

MSG
