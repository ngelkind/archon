#!/usr/bin/env bash
# Install self-hosted ntfy for Archon push, bound to the WireGuard interface.
#
# WHY self-hosted (not FCM): keeps Google off the path and lets push payloads
# stay content-free — the app is only woken, then fetches details over the
# authenticated tunnel. Reachable only from wg peers (listen-http on 10.13.13.1).
#
# RUN AS ROOT ON THE VM, AFTER setup_wireguard.sh.  Idempotent.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)

echo "== installing ntfy =="
export DEBIAN_FRONTEND=noninteractive
if ! command -v ntfy >/dev/null 2>&1; then
  # Official apt repo (works on Ubuntu 24.04 aarch64).
  mkdir -p /etc/apt/keyrings
  curl -fsSL https://archive.heckel.io/apt/pubkey.txt \
    | gpg --dearmor -o /etc/apt/keyrings/archive.heckel.io.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/archive.heckel.io.gpg] \
https://archive.heckel.io/apt debian main" > /etc/apt/sources.list.d/archive.heckel.io.list
  apt-get update -qq
  apt-get install -y -qq ntfy
fi

echo "== installing config =="
mkdir -p /etc/ntfy /var/lib/ntfy
install -m 644 "$HERE/server.yml" /etc/ntfy/server.yml

echo "== enabling ntfy =="
systemctl enable --now ntfy
systemctl restart ntfy

cat <<'EOF'

ntfy is up on http://10.13.13.1:8080 (wg-only).

NEXT:
  * Set NTFY_BASE_URL=http://10.13.13.1:8080 in /opt/archon/secrets/.env
  * The app registers its own topic at pairing (stored in api_devices.push_endpoint).
  * Verify from a wg peer:  curl -d "test" http://10.13.13.1:8080/mytopic
EOF
