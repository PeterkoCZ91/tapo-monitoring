#!/bin/bash
# Mutual host watch — the external dead-man's switch for a fleet of monitor hosts.
#
# Every other notification this project sends is written by the host it reports on, so
# none of them can say "this host is dead" — and nobody notices an absent message. This
# script runs on a *peer*: it pings the hosts named in WATCH_TARGETS (plus an optional
# HTTP health check each) and alerts over Telegram when one stops answering. Point two
# hosts at each other and the loop is closed. Which host watches which deliberately
# lives in each host's env file, never in this repository.
#
# Driven by /etc/tapo-monitor/host-watch.env (also the systemd unit's EnvironmentFile):
#   WATCH_TARGETS   space-separated  name=ping_addr[,health=http://host:port/health]
#   WATCH_FAILS     consecutive misses before the first alert     (default 3)
#   WATCH_COOLDOWN  seconds between repeat alerts while still down (default 1800)
#   TAPO_ENV_FILE   env file carrying TELEGRAM_TOKEN / TELEGRAM_CHAT_ID
#                   (default /etc/tapo-monitor/secrets.env, same as pi_notify.sh)
#
# Ping and health are separate findings with separate state: a failing health URL on a
# host that still answers ping means "service dead, host alive" and alerts on its own.
# While ping is down the health check is skipped entirely — a dead host fails HTTP
# trivially, and one dead host should cost one alert, not two. The skipped episode is
# neither grown nor reset, so a service that stayed dead through a host outage still
# needs WATCH_FAILS fresh misses once the host is back.
#
# State (consecutive-miss counters, episode start, cooldown stamps) lives under
# /var/tmp/host-watch/, which survives a reboot of the watcher — a reboot must not
# forget that a peer is down, or the recovery message would lie about the duration.
set -uo pipefail

# When run by hand there is no EnvironmentFile=, so source the same file the unit uses.
# WATCH_STATE_DIR and HOST_WATCH_ENV_FILE exist so a dry run can redirect everything;
# production hosts have no reason to set either.
ENV_FILE="${HOST_WATCH_ENV_FILE:-/etc/tapo-monitor/host-watch.env}"
# Read as systemd reads it (no expansion, nothing executed), never sourced as shell.
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

SECRETS_FILE="${TAPO_ENV_FILE:-/etc/tapo-monitor/secrets.env}"
envfile_load "$SECRETS_FILE"

TARGETS="${WATCH_TARGETS:?WATCH_TARGETS missing (name=ping_addr[,health=url] ...)}"
FAILS="${WATCH_FAILS:-3}"
COOLDOWN="${WATCH_COOLDOWN:-1800}"
# The bot credential stays in its env var and is used inline below — assigning it to a
# local name trips the push-time credential scanner for no gain.
: "${TELEGRAM_TOKEN:?TELEGRAM_TOKEN missing}"
CHAT="${TELEGRAM_CHAT_ID:-${TELEGRAM_CHAT:?TELEGRAM_CHAT_ID missing}}"
STATE_DIR="${WATCH_STATE_DIR:-/var/tmp/host-watch}"

mkdir -p "$STATE_DIR"
NOW=$(date +%s)
failures=0

# Return 0 only when Telegram confirms "ok":true — the same contract as pi_notify.sh.
# Callers must treat anything else as "not sent": an unconfirmed alert must not arm a
# cooldown, and an unconfirmed recovery must not close an episode.
send_telegram() {
  local response
  # The URL carries the bot credential, so it goes in through a curl config on a pipe and
  # never appears in argv (visible to every local user in /proc and ps).
  response=$(curl -fsS --connect-timeout 5 --max-time 10 -X POST \
    --config <(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_TOKEN") \
    --data-urlencode "chat_id=${CHAT}" \
    --data-urlencode "text=$1" 2>&1) || true
  case "$response" in
    *'"ok":true'*) return 0 ;;
    *) echo "host-watch: Telegram send failed (not confirmed)" >&2; return 1 ;;
  esac
}

# Two echoes at a sub-second interval so a healthy pass costs about a second. Some ping
# builds allow sub-second intervals only to root; a refused flag is not a down host, so
# on a tool complaint (not a timeout) retry plainly with the echoes a second apart.
ping_ok() {
  local err
  if err=$(ping -n -q -c 2 -i 0.3 -W 2 "$1" 2>&1 >/dev/null); then
    return 0
  fi
  case "$err" in
    *nterval*|*permitted*|*privilege*|*nvalid*|*sage:*)
      ping -n -q -c 2 -W 2 "$1" >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

health_ok() {
  curl -fsS --max-time 10 -o /dev/null "$1" 2>/dev/null
}

minutes_since() {
  local since
  since=$(cat "$1" 2>/dev/null || echo "$NOW")
  echo $(( (NOW - since) / 60 ))
}

# One check = one state trio under $STATE_DIR: consecutive misses, the episode's first
# miss, and an "alerted" flag recording that a recovery message is owed on return.
#   $1 state slug   $2 "up"|"down"   $3 alert text   $4 recovery text
process() {
  local slug="$1" state="$2" down_msg="$3" up_msg="$4"
  local miss_f="$STATE_DIR/$slug.miss"
  local since_f="$STATE_DIR/$slug.down_since"
  local alerted_f="$STATE_DIR/$slug.alerted"
  local stamp_f="$STATE_DIR/$slug.last_alert"
  local misses last mins

  if [ "$state" = up ]; then
    rm -f "$miss_f"
    if [ -f "$alerted_f" ]; then
      mins=$(minutes_since "$since_f")
      # Recovery is delivery-gated like the alert itself: a failed send keeps the
      # episode open so the next pass retries, instead of closing it in silence.
      if send_telegram "$up_msg (down ${mins} min)"; then
        echo "host-watch: $slug recovery delivered"
        rm -f "$alerted_f" "$stamp_f" "$since_f"
      else
        failures=$((failures + 1))
      fi
    else
      rm -f "$since_f" "$stamp_f"
    fi
    return
  fi

  misses=$(( $(cat "$miss_f" 2>/dev/null || echo 0) + 1 ))
  echo "$misses" > "$miss_f"
  [ -f "$since_f" ] || echo "$NOW" > "$since_f"
  if [ "$misses" -lt "$FAILS" ]; then
    # Never alert on a lone miss: a lost echo pair is routine on a wireless hop, and
    # the daemon already reports its own camera problems. Log it so a later "why did
    # the alert take six minutes" has an answer in the journal.
    echo "host-watch: $slug miss $misses/$FAILS"
    return
  fi
  last=$(cat "$stamp_f" 2>/dev/null || echo 0)
  if [ $((NOW - last)) -lt "$COOLDOWN" ]; then
    echo "host-watch: $slug still down, cooldown active"
    return
  fi
  mins=$(minutes_since "$since_f")
  # The cooldown stamp is written only after Telegram confirms, mirroring pi_notify.sh:
  # an unconfirmed alert retries on the next pass instead of consuming its cooldown.
  if send_telegram "$down_msg (${mins} min)"; then
    echo "host-watch: $slug outage delivered"
    echo "$NOW" > "$stamp_f"
    : > "$alerted_f"
  else
    failures=$((failures + 1))
  fi
}

# Word splitting is the parse: WATCH_TARGETS is a space-separated list by contract.
# shellcheck disable=SC2086
for target in $TARGETS; do
  name="${target%%=*}"
  rest="${target#*=}"
  if [ -z "$name" ] || [ "$name" = "$target" ] || [ -z "$rest" ]; then
    echo "host-watch: cannot parse target '$target' (want name=ping_addr[,health=url])" >&2
    failures=$((failures + 1))
    continue
  fi
  addr="${rest%%,*}"
  health=""
  case "$rest" in
    *,health=*) health="${rest#*,health=}" ;;
  esac
  slug="${name//[^a-zA-Z0-9_-]/_}"

  if ping_ok "$addr"; then
    process "$slug.ping" up \
      "🔴 host-watch: $name unreachable" \
      "🟢 host-watch: $name reachable again"
    if [ -n "$health" ]; then
      if health_ok "$health"; then
        process "$slug.health" up \
          "🟠 host-watch: $name health check failing" \
          "🟢 host-watch: $name health check ok again"
      else
        process "$slug.health" down \
          "🟠 host-watch: $name health check failing" \
          "🟢 host-watch: $name health check ok again"
      fi
    fi
  else
    process "$slug.ping" down \
      "🔴 host-watch: $name unreachable" \
      "🟢 host-watch: $name reachable again"
  fi
done

# Down targets are findings, not failures of this script; the exit code reports only
# what broke in the watcher itself (unparseable target, unconfirmed Telegram send), so
# the unit's OnFailure hook fires for a blind watchman and stays quiet for a dead peer.
[ "$failures" -eq 0 ] || exit 1
exit 0
