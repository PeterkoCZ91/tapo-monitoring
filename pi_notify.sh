#!/bin/bash
set -u

ENV_FILE="${TAPO_ENV_FILE:-/etc/tapo-monitor/secrets.env}"
# Safe EnvironmentFile reader, inlined: this script is installed standalone. Keep in
# sync with tools/lib/envfile.sh (no expansion, nothing executed, KEY=[A-Za-z_][A-Za-z0-9_]*).
# Echo "KEY<TAB>VALUE" pairs, one per valid line.
_envfile_parse() {
    local file="$1" line key val
    [[ -r "$file" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" || "$line" == \#* || "$line" == \;* ]] && continue
        [[ "$line" == *=* ]] || continue
        key="${line%%=*}"
        val="${line#*=}"
        key="${key%"${key##*[![:space:]]}"}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        val="${val#"${val%%[![:space:]]*}"}"
        if [[ ${#val} -ge 2 && ( "${val:0:1}" == '"' || "${val:0:1}" == "'" ) \
              && "${val: -1}" == "${val:0:1}" ]]; then
            val="${val:1:${#val}-2}"
        fi
        printf '%s\t%s\n' "$key" "$val"
    done <"$file"
}

envfile_load() {
    local key val
    while IFS=$'\t' read -r key val; do
        export "$key=$val"
    done < <(_envfile_parse "$1")
}

envfile_get() {
    local want="$2" key val out=""
    while IFS=$'\t' read -r key val; do
        [[ "$key" == "$want" ]] && out="$val"
    done < <(_envfile_parse "$1")
    printf '%s' "$out"
}

envfile_load "$ENV_FILE"

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
  # /run is root-owned: a world-writable /tmp lock could be planted by any local user and
  # was then evaluated in $((...)) as root. TAPO_NOTIFY_LOCK_DIR exists for tests.
  LOCK="${TAPO_NOTIFY_LOCK_DIR:-/run}/notify_cooldown_${SERVICE//[^a-zA-Z0-9]/_}"
  if [ -f "$LOCK" ] && [ ! -L "$LOCK" ]; then
    LAST=$(head -c 32 "$LOCK" 2>/dev/null || true)
    NOW=$(date +%s)
    # A malformed stamp is ignored (treated as no cooldown), never evaluated.
    if [[ $LAST =~ ^[0-9]+$ ]] && [ $((NOW - LAST)) -lt 1800 ]; then
      echo "notify cooldown active for $SERVICE"
      exit 0
    fi
  fi
fi

# Network-online can precede working DNS or the route used for notifications.
# Retry startup connectivity failures briefly; an API refusal is final.
RESPONSE=""
for attempt in 1 2 3; do
  # The URL embeds the bot credential: keep it out of argv via a curl config on a pipe.
  if RESPONSE=$(curl -fsS --connect-timeout 5 --max-time 10 -X POST \
      --config <(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$auth_value") \
      --data-urlencode "chat_id=${CHAT}" --data-urlencode "text=${MSG}" \
      --data-urlencode "parse_mode=HTML" 2>/dev/null); then
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
