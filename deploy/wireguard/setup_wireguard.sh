#!/usr/bin/env bash
# Set up the WireGuard server that carries the Android control app's traffic.
#
# WHY: the app must reach the in-process control API, but Archon's posture is
# "no discoverable inbound service". WireGuard opens exactly ONE UDP port whose
# Noise handshake gives no reply to an unauthenticated packet — to a scanner it
# is indistinguishable from closed. The API then binds to the wg0 address only
# (API_BIND_HOST=10.13.13.1), so it is unreachable except from a peer holding a
# key. Nothing else is exposed; this is the only inbound change to the VM.
#
# RUN AS ROOT ON THE VM (84.13.79.132). Review before running. Idempotent.
set -euo pipefail

WG_DIR=/etc/wireguard
WG_ADDR=${WG_ADDR:-10.13.13.1/24}
WG_PORT=${WG_PORT:-51820}

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

echo "== installing wireguard + qrencode =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wireguard qrencode iptables-persistent

umask 077
mkdir -p "$WG_DIR"
if [[ ! -f "$WG_DIR/server_private.key" ]]; then
  echo "== generating server keypair =="
  wg genkey | tee "$WG_DIR/server_private.key" | wg pubkey > "$WG_DIR/server_public.key"
  chmod 600 "$WG_DIR/server_private.key"
fi
SERVER_PRIV=$(cat "$WG_DIR/server_private.key")
SERVER_PUB=$(cat "$WG_DIR/server_public.key")

if [[ ! -f "$WG_DIR/wg0.conf" ]]; then
  echo "== writing $WG_DIR/wg0.conf =="
  cat > "$WG_DIR/wg0.conf" <<EOF
# Archon control tunnel. Peers are appended by add_peer.sh.
# Split-tunnel: peers route only 10.13.13.0/24 here, not their internet.
[Interface]
Address = ${WG_ADDR}
ListenPort = ${WG_PORT}
PrivateKey = ${SERVER_PRIV}
EOF
  chmod 600 "$WG_DIR/wg0.conf"
else
  echo "wg0.conf already exists — leaving it untouched"
fi

echo "== opening UDP ${WG_PORT} in the host firewall =="
# OCI Ubuntu images ship an INPUT REJECT rule; insert an ACCEPT before it.
if ! iptables -C INPUT -p udp --dport "$WG_PORT" -j ACCEPT 2>/dev/null; then
  iptables -I INPUT -p udp --dport "$WG_PORT" -j ACCEPT
  netfilter-persistent save
fi

echo "== enabling wg-quick@wg0 =="
systemctl enable --now wg-quick@wg0
systemctl restart wg-quick@wg0 || true

cat <<EOF

WireGuard is up on UDP ${WG_PORT}.  Server public key:

    ${SERVER_PUB}

STILL REQUIRED (not done by this script):
  1. Open UDP ${WG_PORT} INGRESS in the OCI security list / NSG for this VM.
     Example (from a machine with the OCI CLI configured):
       oci network security-list update --security-list-id <SL_OCID> \\
         --ingress-security-rules '[{"protocol":"17","source":"0.0.0.0/0",
           "udpOptions":{"destinationPortRange":{"min":${WG_PORT},"max":${WG_PORT}}}}]'
     (17 = UDP. Merge with existing rules; do not drop the SSH rule.)
  2. Add your phone as a peer:  deploy/wireguard/add_peer.sh pixel
  3. Set API_BIND_HOST=${WG_ADDR%/*} in /opt/archon/secrets/.env (see deploy/CONTROL_API.md).
EOF
