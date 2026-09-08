#!/usr/bin/env bash
# Egress monitor (SECURITY.md): snapshot outbound connections of the archon
# process tree, flag remotes whose reverse-DNS/org is outside the expected
# set, and alert the owner through the control bot. Detection, not blocking.
set -u

ENV_FILE=/opt/archon/secrets/.env
LOG=/opt/archon/data/egress.log
STATE=/opt/archon/data/egress-known.txt
touch "$STATE"

# Expected destinations: Telegram DCs, WhatsApp/Meta, Google, Anthropic,
# OpenAI, OpenRouter, GitHub, Oracle metadata, DNS.
EXPECTED_RDNS='telegram|whatsapp|facebook|fbcdn|meta|1e100\.net|google|googleapis|anthropic|openai|openrouter|github|oracle|cloudflare|akamai|amazonaws'
EXPECTED_NETS='^(149\.154\.|91\.108\.|157\.240\.|31\.13\.|127\.|10\.|169\.254\.|172\.1[6-9]\.|172\.2[0-9]\.|172\.3[01]\.|192\.168\.)'

remotes=$(ss -tupn 2>/dev/null | grep -E 'python|archon' | \
  grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+\s*$' | cut -d: -f1 | sort -u)

unexpected=""
for ip in $remotes; do
  echo "$ip" | grep -qE "$EXPECTED_NETS" && continue
  grep -qxF "$ip" "$STATE" && continue
  rdns=$(timeout 3 getent hosts "$ip" 2>/dev/null | awk '{print $2}' | head -1)
  if echo "${rdns:-}" | grep -qiE "$EXPECTED_RDNS"; then
    echo "$ip" >> "$STATE"   # verified once, don't re-resolve every tick
    continue
  fi
  unexpected="$unexpected $ip(${rdns:-no-rdns})"
done

if [ -n "$unexpected" ]; then
  echo "$(date -Is) UNEXPECTED egress:$unexpected" >> "$LOG"
  TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
  OWNER=$(grep -E '^TELEGRAM_OWNER_ID=' "$ENV_FILE" | cut -d= -f2-)
  if [ -n "$TOKEN" ] && [ -n "$OWNER" ]; then
    if ! curl -fsS -m 10 "https://api.telegram.org/bot${TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${OWNER}" \
      --data-urlencode "text=🔎 Egress monitor: unexpected outbound connection(s):${unexpected}" \
      >/dev/null 2>&1; then
      # No `|| true`: a monitor that cannot reach the owner about unexpected
      # egress has failed at its one job — surface it (systemctl status) instead
      # of hiding it.
      echo "$(date -Is) egress-monitor: FAILED to deliver alert" >> "$LOG"
      exit 1
    fi
  fi
fi
exit 0
