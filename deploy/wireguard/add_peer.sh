#!/usr/bin/env bash
# Add one device (your phone) as a WireGuard peer and print the client config
# the app imports during onboarding (as text and as a scannable QR).
#
# Single-user model: we generate the peer keypair here for convenience. The peer
# only ever routes 10.13.13.1/32 (the VM) through the tunnel — split tunnel, so
# the phone's normal internet is untouched. Revoke a device by deleting its
# [Peer] block from wg0.conf and `wg syncconf`, which mirrors revoking its API
# token (api_devices.revoked_at).
#
# RUN AS ROOT ON THE VM.  Usage: add_peer.sh <name>
set -euo pipefail

NAME=${1:?usage: add_peer.sh <name>}
WG_DIR=/etc/wireguard
WG_PORT=${WG_PORT:-51820}
ENDPOINT_HOST=${ENDPOINT_HOST:-84.13.79.132}

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
SERVER_PUB=$(cat "$WG_DIR/server_public.key")

# Next free 10.13.13.x (server is .1).
used=$(grep -oE '10\.13\.13\.[0-9]+' "$WG_DIR/wg0.conf" | awk -F. '{print $4}' | sort -n | tail -1)
next=$(( ${used:-1} + 1 ))
PEER_ADDR="10.13.13.${next}"

umask 077
PEER_PRIV=$(wg genkey)
PEER_PUB=$(echo "$PEER_PRIV" | wg pubkey)

echo "== appending peer '$NAME' ($PEER_ADDR) to wg0.conf =="
cat >> "$WG_DIR/wg0.conf" <<EOF

# peer: ${NAME}
[Peer]
PublicKey = ${PEER_PUB}
AllowedIPs = ${PEER_ADDR}/32
EOF
wg syncconf wg0 <(wg-quick strip wg0)

CLIENT_CONF=$(cat <<EOF
[Interface]
PrivateKey = ${PEER_PRIV}
Address = ${PEER_ADDR}/32

[Peer]
PublicKey = ${SERVER_PUB}
Endpoint = ${ENDPOINT_HOST}:${WG_PORT}
AllowedIPs = 10.13.13.1/32
PersistentKeepalive = 25
EOF
)

echo
echo "===== client config for '$NAME' — import this in the app ====="
echo "$CLIENT_CONF"
echo "=============================================================="
echo
echo "QR (scan in the app's WireGuard step):"
echo "$CLIENT_CONF" | qrencode -t ansiutf8
echo
echo "The app then reaches the API at http://10.13.13.1:\${API_PORT} over this tunnel."
