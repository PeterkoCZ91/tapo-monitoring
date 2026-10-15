#!/bin/bash
set -u

ENV_FILE="${TAPO_ENV_FILE:-/etc/tapo-monitor/secrets.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # The env file path is host-specific, so shellcheck cannot follow it.
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
fi

if [ -n "${TAPO_OPS_TELEGRAM_TOKEN:-}" ] || [ -n "${TAPO_OPS_TELEGRAM_CHAT_ID:-}" ]; then
  auth_value="${TAPO_OPS_TELEGRAM_TOKEN:?operational token missing}"
  CHAT="${TAPO_OPS_TELEGRAM_CHAT_ID:?operational chat missing}"
else
  auth_value="${TELEGRAM_TOKEN:?TELEGRAM_TOKEN missing}"
  CHAT="${TELEGRAM_CHAT_ID:-${TELEGRAM_CHAT:?TELEGRAM_CHAT_ID missing}}"
fi
MSG="$1"
SERVICE="${2:-}"
LOCK=""

# Cooldown 30 minut per sluzba; lock se zapise az po uspesnem Telegram odeslani.
if [ -n "$SERVICE" ]; then
  LOCK="/tmp/notify_cooldown_${SERVICE//[^a-zA-Z0-9]/_}"
  if [ -f "$LOCK" ]; then
    LAST=$(cat "$LOCK")
    NOW=$(date +%s)
    if [ $((NOW - LAST)) -lt 1800 ]; then
      echo "notify cooldown active for $SERVICE"
      exit 0
    fi
  fi
fi

# Network-online can precede working DNS or the route used for notifications.
# Retry startup connectivity failures briefly; an API refusal is final.
RESPONSE=""
for attempt in 1 2 3; do
  if RESPONSE=$(curl -fsS --connect-timeout 5 --max-time 10 -X POST "https://api.telegram.org/bot${auth_value}/sendMessage"   --data-urlencode "chat_id=${CHAT}"   --data-urlencode "text=${MSG}"   --data-urlencode "parse_mode=HTML" 2>/dev/null); then
    break
  else
    curl_status=$?
    RESPONSE=""
  fi
  case "$curl_status" in
    5|6|7|28)
      [ "$attempt" -lt 3 ] || break
      sleep 5
      ;;
    *) break ;;
  esac
done

case "$RESPONSE" in
  *'"ok":true'*)
    [ -n "$LOCK" ] && date +%s > "$LOCK"
    exit 0
    ;;
  *)
    echo "Telegram send failed (not confirmed)" >&2
    exit 1
    ;;
esac
