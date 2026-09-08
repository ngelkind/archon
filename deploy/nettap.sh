#!/usr/bin/env bash
# Privileged packet tap for a live-probe run. SEPARATE from archon.service,
# which keeps its systemd sandbox (NoNewPrivileges, RestrictAddressFamilies):
# this needs CAP_NET_RAW/CAP_NET_ADMIN, so it must never be folded into the app
# unit. It captures the process's platform traffic around a probe run and
# summarises peers + TLS SNI into the probe report — the out-of-process
# counterpart to the in-process network ledger.
#
# Usage: nettap.sh start | stop | summary | capture <seconds>
set -uo pipefail

DIR=/opt/archon/data/nettap
mkdir -p "$DIR"
IFACE="${NETTAP_IFACE:-$(ip route 2>/dev/null | awk '/default/{print $5; exit}')}"
PCAP="$DIR/capture.pcap"
PIDF="$DIR/tcpdump.pid"

_start() {
  command -v tcpdump >/dev/null || { echo "nettap: tcpdump not installed" >&2; exit 3; }
  command -v conntrack >/dev/null 2>&1 && conntrack -F >/dev/null 2>&1
  nohup tcpdump -n -i "$IFACE" -w "$PCAP" \
    'tcp and (port 443 or port 80 or port 5222 or port 5223)' \
    >/dev/null 2>&1 &
  echo $! > "$PIDF"
  echo "nettap: capturing on ${IFACE:-?} (pid $(cat "$PIDF"))"
}

_stop() {
  if [ -f "$PIDF" ]; then
    kill "$(cat "$PIDF")" 2>/dev/null || true
    rm -f "$PIDF"
  fi
  echo "nettap: stopped"
}

_summary() {
  [ -f "$PCAP" ] || { echo "nettap: no capture at $PCAP" >&2; exit 3; }
  if command -v tshark >/dev/null 2>&1; then
    tshark -r "$PCAP" -Y 'tls.handshake.extensions_server_name' -T fields \
      -e tls.handshake.extensions_server_name 2>/dev/null \
      | sort | uniq -c | sort -rn > "$DIR/sni.txt" || true
  fi
  tcpdump -nr "$PCAP" 2>/dev/null | awk '{print $3}' \
    | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | sort | uniq -c | sort -rn \
    | head -40 > "$DIR/peers.txt" || true
  echo "nettap summary -> $DIR/sni.txt, $DIR/peers.txt"
  echo "--- TLS SNI ---";  cat "$DIR/sni.txt"   2>/dev/null || echo "(tshark not installed)"
  echo "--- peers ---";    cat "$DIR/peers.txt" 2>/dev/null
}

case "${1:-}" in
  start)   _start ;;
  stop)    _stop ;;
  summary) _summary ;;
  capture)
    secs="${2:-120}"
    _start; sleep "$secs"; _stop; _summary
    ;;
  *) echo "usage: nettap.sh start|stop|summary|capture <seconds>" >&2; exit 2 ;;
esac
