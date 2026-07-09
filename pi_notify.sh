#!/bin/bash
set -u

ENV_FILE="${TAPO_ENV_FILE:-/etc/tapo-monitor/secrets.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

TOKEN="${TELEGRAM_TOKEN:?TELEGRAM_TOKEN missing}"
CHAT="${TELEGRAM_CHAT_ID:-${TELEGRAM_CHAT:?TELEGRAM_CHAT_ID missing}}"
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

RESPONSE=$(curl -fsS -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage"   --data-urlencode "chat_id=${CHAT}"   --data-urlencode "text=${MSG}"   --data-urlencode "parse_mode=HTML")

case "$RESPONSE" in
  *'"ok":true'*)
    [ -n "$LOCK" ] && date +%s > "$LOCK"
    exit 0
    ;;
  *)
    echo "Telegram send failed: $RESPONSE" >&2
    exit 1
    ;;
esac
