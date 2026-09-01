#!/usr/bin/env bash
# One read-only table for the whole fleet. Run it FROM THE WORKSTATION.
#
# The daily review used to be thirty ad-hoc ssh one-liners, phrased a little differently
# every morning, so yesterday's answer could never be laid beside today's. This asks every
# host the same questions in the same order and prints one row each: the unit — including
# the `auto-restart` crash loop that `is-active` reports as healthy — the fingerprint the
# running package reports against the one its env file says it should be, whether today's
# digest went out, the host-watch timer, disk/load/temperature, and how many frames last
# night's pan guard and Telegram archive kept. The scorer gets its own line.
#
# It only reads. No restart, no deploy, no sudo, nothing written on any host. The single
# write is a local state file remembering the scorer's counters, so the next run can say
# how far they moved rather than printing a number nobody can compare against.
#
# Which machines make up the fleet is configuration, not code — this repository is public,
# so the targets come from the environment exactly as WATCH_TARGETS feeds host_watch.sh:
#
#   TAPO_FLEET_HOSTS      space-separated ssh targets, each `target` or `label=target`
#                         (required — with nothing set this prints usage and exits)
#   TAPO_FLEET_SCORER_URL scorer base URL          (default http://127.0.0.1:8766)
#   TAPO_FLEET_NIGHT      window the frame counts cover, HH:MM-HH:MM  (default 18:00-08:00)
#   TAPO_FLEET_UNIT       monitor unit to inspect  (default tapo-monitor.service)
#   TAPO_FLEET_WATCH_UNIT host-watch timer         (default host-watch.timer)
#   TAPO_FLEET_ROOT       package root on the host, relative to its $HOME when not
#                         absolute                 (default tapo-monitor)
#   TAPO_FLEET_PYTHON     venv interpreter on the host, same rule
#                         (default tapo-env/bin/python, falls back to python3)
#   TAPO_FLEET_TIMEOUT    seconds per host before it counts as unreachable  (default 60)
#   TAPO_FLEET_STATE_DIR  where the scorer counters are remembered
#
# Exit status: 0 nothing to report, 1 at least one finding, 2 bad invocation. A host that
# does not answer is a finding and its own row, never a reason to abandon the table — the
# fleet has a Pi that can take ten seconds to log in and a host that is sometimes off.
set -uo pipefail

die() { printf 'fleet_status: %s\n' "$*" >&2; exit 2; }

usage() {
    cat >&2 <<'USAGE'
usage: TAPO_FLEET_HOSTS="target [label=target ...]" fleet_status.sh [options]

  --night HH:MM-HH:MM   window the panlimit/sent counts cover (default 18:00-08:00)
  --scorer URL          scorer base URL (default http://127.0.0.1:8766)
  --no-scorer           skip the scorer row entirely

Reads only. See the header of this script for the full list of environment knobs.
USAGE
    exit 2
}

night="${TAPO_FLEET_NIGHT:-18:00-08:00}"
scorer_url="${TAPO_FLEET_SCORER_URL:-http://127.0.0.1:8766}"
want_scorer=1

while (($#)); do
    case "$1" in
        --night)      night="${2:?--night needs HH:MM-HH:MM}"; shift 2 ;;
        --scorer)     scorer_url="${2:?--scorer needs a URL}"; shift 2 ;;
        --no-scorer)  want_scorer=0; shift ;;
        -h|--help)    usage ;;
        *)            die "unknown argument $1" ;;
    esac
done

[[ -n "${TAPO_FLEET_HOSTS:-}" ]] || usage

# The window is interpolated into a `date -d` string on the host, so it is validated here
# rather than trusted: a fleet list is configuration, but it is still input.
[[ "$night" =~ ^(([01][0-9]|2[0-3]):[0-5][0-9])-(([01][0-9]|2[0-3]):[0-5][0-9])$ ]] \
    || die "--night wants HH:MM-HH:MM, got '$night'"
night_start="${BASH_REMATCH[1]}"
night_end="${BASH_REMATCH[3]}"

unit="${TAPO_FLEET_UNIT:-tapo-monitor.service}"
watch_unit="${TAPO_FLEET_WATCH_UNIT:-host-watch.timer}"
root="${TAPO_FLEET_ROOT:-tapo-monitor}"
python_bin="${TAPO_FLEET_PYTHON:-tapo-env/bin/python}"
host_timeout="${TAPO_FLEET_TIMEOUT:-60}"
connect_timeout=$(( host_timeout / 4 ))
((connect_timeout > 3)) || connect_timeout=3

read -r -a entries <<<"$TAPO_FLEET_HOSTS"
((${#entries[@]})) || usage

labels=()
targets=()
for entry in "${entries[@]}"; do
    labels+=("${entry%%=*}")
    targets+=("${entry#*=}")
done

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# ── the probe that runs on each host ──────────────────────────────────────────────────
# Everything the table needs, collected in one login, printed as key=value lines. Kept to
# systemctl/stat/find/df and one `tapo-monitor version`: no writes, no sudo, no journal
# dump — a status check that needs privileges is a status check nobody runs.
probe_host() {
    local remote_args
    remote_args="$(printf ' %q' "$root" "$unit" "$python_bin" "$watch_unit" \
        "$night_start" "$night_end")"
    # shellcheck disable=SC2029  # client-side expansion is the point: the arguments are
    # %q-quoted here and executed on the host.
    timeout "$host_timeout" ssh -o BatchMode=yes -o ConnectTimeout="$connect_timeout" \
        "$1" "bash -s --$remote_args" <<'REMOTE'
set -u
export LC_ALL=C
root="$1"; unit="$2"; python_bin="$3"; watch_unit="$4"; night_start="$5"; night_end="$6"

case "$root" in /*) ;; *) root="$HOME/$root" ;; esac
case "$python_bin" in /*) ;; *) python_bin="$HOME/$python_bin" ;; esac
[ -x "$python_bin" ] || python_bin="python3"

emit() { printf '%s=%s\n' "$1" "$2"; }
notes=""
note() { notes="${notes:+$notes | }$1"; }
prop() { printf '%s\n' "$1" | sed -n "s/^$2=//p" | head -1; }

now=$(date +%s)
emit now "$now"
emit today "$(date +%F)"
emit now_hm "$(date +%H:%M)"

unit_props=$(systemctl show -p LoadState -p ActiveState -p SubState -p NRestarts \
    -p ActiveEnterTimestampMonotonic -p EnvironmentFiles "$unit" 2>/dev/null)
emit unit_load "$(prop "$unit_props" LoadState)"
emit unit_active "$(prop "$unit_props" ActiveState)"
emit unit_sub "$(prop "$unit_props" SubState)"
emit unit_restarts "$(prop "$unit_props" NRestarts)"

# Uptime from the monotonic stamp and /proc/uptime rather than the printed timestamp:
# arithmetic cannot be broken by the host's locale, and half this fleet reports its dates
# in Czech.
started=""
mono=$(prop "$unit_props" ActiveEnterTimestampMonotonic)
case "$mono" in
    ''|0|*[!0-9]*) emit unit_uptime "-" ;;
    *)
        up=$(cut -d' ' -f1 /proc/uptime 2>/dev/null)
        up=${up%%.*}
        case "$up" in
            ''|*[!0-9]*) emit unit_uptime "-" ;;
            *)
                started=$(( now - up + mono / 1000000 ))
                emit unit_uptime "$(( now - started ))"
                ;;
        esac
        ;;
esac

# The unit names its own env file; guessing a path per host is how the checks drifted
# apart in the first place. Only the three variables below are ever read out of it — the
# same file holds the camera and Telegram credentials, which must not travel back here.
env_file=$(prop "$unit_props" EnvironmentFiles | tr ' ' '\n' | sed 's/^-//' | grep -m1 '^/')
envget() {
    [ -n "$env_file" ] && [ -r "$env_file" ] || return 0
    sed -n "s/^[[:space:]]*$1=//p" "$env_file" | tail -1 | tr -d '"\047\r' | awk '{$1=$1; print}'
}

link=$(readlink "$root/current" 2>/dev/null)
release=${link##*/}
emit release "${release:--}"
link_mtime=$(stat -c %Y "$root/current" 2>/dev/null)

pkg_dir="$root/current"
[ -d "$pkg_dir/tapo_monitor" ] || pkg_dir="$root"
fp=$(cd "$pkg_dir" 2>/dev/null && timeout 60 "$python_bin" -m tapo_monitor.cli version 2>/dev/null \
    | awk '/^package /{print $2}')
fp_source=cli
if [ -z "$fp" ]; then
    # The release directory carries the fingerprint it was deployed as, which still
    # answers "which build is on disk" when the interpreter or the package is broken —
    # flagged as second-hand, because it cannot notice a file edited in place afterwards.
    case "$release" in
        *-*) fp=${release##*-}; fp_source=release ;;
        *)   fp="-"; fp_source=none ;;
    esac
fi
emit fp_running "$fp"
emit fp_source "$fp_source"
emit fp_expected "$(envget TAPO_EXPECTED_FINGERPRINT)"

# A deploy switches `current` and then restarts. The reverse order means the daemon is
# still executing the release before this one, and every other check here would agree
# with the deploy while the running code disagrees with all of them.
if [ -n "$link_mtime" ] && [ -n "$started" ] && [ "$link_mtime" -gt "$started" ]; then
    note "current was switched after the unit last started: the daemon still runs the previous release"
fi

review_dir=$(envget TAPO_REVIEW_LOG_DIR)
[ -n "$review_dir" ] || review_dir="$root/review-log"
emit digest_time "$(envget TAPO_REVIEW_DIGEST_TIME)"
emit digest_last "$(head -c 32 "$review_dir/.digest-sent" 2>/dev/null | tr -dc '0-9-')"
# The stamp file names the day; its mtime is the minute the send was confirmed, which is
# what tells a digest that went out late from one that went out on time.
digest_mtime=$(stat -c %Y "$review_dir/.digest-sent" 2>/dev/null)
case "$digest_mtime" in
    ''|*[!0-9]*) emit digest_at "-" ;;
    *)           emit digest_at "$(date -d "@$digest_mtime" '+%H:%M')" ;;
esac

watch_props=$(systemctl show -p LoadState -p ActiveState -p SubState -p LastTriggerUSec \
    "$watch_unit" 2>/dev/null)
emit watch_load "$(prop "$watch_props" LoadState)"
emit watch_active "$(prop "$watch_props" ActiveState)"
watch_age="-"
last_trigger=$(prop "$watch_props" LastTriggerUSec)
case "$last_trigger" in
    ''|n/a) ;;
    *)
        # This one systemd prints as a formatted date whatever you ask for, so it is
        # parsed — under LC_ALL=C, which is why the export above is not decoration.
        t=$(date -d "$last_trigger" +%s 2>/dev/null)
        case "$t" in
            ''|*[!0-9]*) ;;
            *) watch_age=$(( now - t )) ;;
        esac
        ;;
esac
emit watch_age "$watch_age"
emit watch_result "$(systemctl show -p Result --value "${watch_unit%.timer}.service" 2>/dev/null)"

# "Last night" is a window that has to close: counting the last 24 hours would fold this
# morning's daylight traffic into a number meant to describe the dark.
start_ts=$(date -d "$(date +%F) $night_start" +%s 2>/dev/null)
end_ts=$(date -d "$(date +%F) $night_end" +%s 2>/dev/null)
case "$start_ts$end_ts" in
    ''|*[!0-9]*) start_ts=$(( now - 86400 )); end_ts=$now ;;
    *)
        [ "$end_ts" -le "$start_ts" ] && end_ts=$(( end_ts + 86400 ))
        while [ "$start_ts" -gt "$now" ]; do
            start_ts=$(( start_ts - 86400 ))
            end_ts=$(( end_ts - 86400 ))
        done
        [ "$end_ts" -gt "$now" ] && end_ts=$now
        ;;
esac
emit night_label "$(date -d "@$start_ts" '+%m-%d %H:%M') -> $(date -d "@$end_ts" '+%m-%d %H:%M')"

count_window() {
    [ -d "$1" ] || { printf '%s' '-'; return; }
    find "$1" -maxdepth 1 -type f -name '*.jpg' -newermt "@$2" ! -newermt "@$3" 2>/dev/null | wc -l
}
sent_dir=$(envget TAPO_SENT_LOG_DIR)
[ -n "$sent_dir" ] || sent_dir="$root/sent-log"
# Same rule as sentlog.panlimit_dir_from_env: beside the review log, never inside it.
panlimit_dir="$(dirname "$review_dir")/panlimit-log"
emit panlimit_night "$(count_window "$panlimit_dir" "$start_ts" "$end_ts")"
emit sent_night "$(count_window "$sent_dir" "$start_ts" "$end_ts")"

df_line=$(df -P -h "$root" 2>/dev/null | awk 'NR==2 {print $5" "$4}')
emit disk_pct "${df_line%% *}"
emit disk_avail "${df_line##* }"
emit load1 "$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)"

# Zone 0 is the CPU on a Pi and a mainboard sensor on a PC, which is why the package zone
# is preferred where one exists: 28 °C and 48 °C from the same machine are not the same
# reading, and a table nobody can compare row to row is the thing being replaced.
temp=""
for zone in /sys/class/thermal/thermal_zone*; do
    [ -r "$zone/temp" ] || continue
    case "$(cat "$zone/type" 2>/dev/null)" in
        cpu-thermal|x86_pkg_temp|coretemp|soc-thermal|soc_thermal)
            temp=$(cat "$zone/temp" 2>/dev/null)
            break
            ;;
    esac
    [ -n "$temp" ] || temp=$(cat "$zone/temp" 2>/dev/null)
done
case "$temp" in
    ''|*[!0-9]*) emit temp_c "-" ;;
    *)           emit temp_c "$(( temp / 1000 )).$(( temp / 100 % 10 ))C" ;;
esac

emit notes "$notes"
REMOTE
}

# ── ask every host at once ────────────────────────────────────────────────────────────
# Sequentially, one sleeping Pi would hold the whole table hostage for its full timeout.
for idx in "${!targets[@]}"; do
    {
        probe_host "${targets[idx]}" >"$tmpdir/$idx.out" 2>"$tmpdir/$idx.err"
        printf '%s\n' "$?" >"$tmpdir/$idx.rc"
    } &
done
wait

declare -A F=()
declare -a unreachable=()
for idx in "${!targets[@]}"; do
    rc="$(cat "$tmpdir/$idx.rc" 2>/dev/null || echo 1)"
    if [[ "$rc" != "0" || ! -s "$tmpdir/$idx.out" ]]; then
        unreachable[idx]=1
        F["$idx/why"]="$(tr '\n' ' ' <"$tmpdir/$idx.err" 2>/dev/null | cut -c1-120)"
        [[ -n "${F["$idx/why"]}" ]] || F["$idx/why"]="no answer within ${host_timeout}s"
        continue
    fi
    unreachable[idx]=0
    while IFS= read -r line; do
        [[ "$line" == *=* ]] || continue
        F["$idx/${line%%=*}"]="${line#*=}"
    done <"$tmpdir/$idx.out"
done

# ── formatting ────────────────────────────────────────────────────────────────────────
f() { local v="${F["$1/$2"]:-}"; printf '%s' "${v:--}"; }

fmt_age() {
    local s="${1:-}"
    case "$s" in ''|-|*[!0-9]*) printf '%s' '-'; return ;; esac
    if   ((s >= 172800)); then printf '%dd%dh' $((s / 86400)) $((s % 86400 / 3600))
    elif ((s >= 3600));   then printf '%dh%02dm' $((s / 3600)) $((s % 3600 / 60))
    elif ((s >= 60));     then printf '%dm' $((s / 60))
    else                       printf '%ds' "$s"
    fi
}

findings=()
finding() { findings+=("$1: $2"); }

unit_cell() {
    local active sub
    active="$(f "$1" unit_active)" sub="$(f "$1" unit_sub)"
    case "$active/$sub" in
        active/running)            printf 'running' ;;
        activating/auto-restart|*/auto-restart)
                                   printf 'AUTO-RESTART' ;;
        */*) printf '%.13s' "$active/$sub" ;;
    esac
}

expect_cell() {
    local running expected
    running="$(f "$1" fp_running)" expected="$(f "$1" fp_expected)"
    if [[ "$expected" == "-" ]]; then printf 'unset'
    elif [[ "$running" == "$expected" ]]; then printf 'ok'
    else printf 'DRIFT'
    fi
}

digest_cell() {
    local due last today now_hm yesterday
    due="$(f "$1" digest_time)" last="$(f "$1" digest_last)"
    today="$(f "$1" today)" now_hm="$(f "$1" now_hm)"
    [[ "$due" == "-" ]] && { printf 'off'; return; }
    [[ "$last" == "$today" ]] && { printf 'today'; return; }
    yesterday="$(date -d "$today -1 day" +%F 2>/dev/null)"
    # Before the configured hour, "not sent today" is the only correct state there is.
    if [[ "$now_hm" < "$due" && "$last" == "$yesterday" ]]; then printf 'pending'
    elif [[ "$last" == "$yesterday" ]]; then printf 'LATE'
    else printf 'MISSING'
    fi
}

watch_cell() {
    local load active result
    load="$(f "$1" watch_load)" active="$(f "$1" watch_active)" result="$(f "$1" watch_result)"
    case "$load" in
        loaded) ;;
        *) printf 'none'; return ;;
    esac
    if [[ "$active" != "active" ]]; then printf 'INACTIVE'
    elif [[ "$result" != "success" && "$result" != "-" ]]; then printf 'FAIL %s' "$(fmt_age "$(f "$1" watch_age)")"
    else printf 'ok %s' "$(fmt_age "$(f "$1" watch_age)")"
    fi
}

width=4
for label in "${labels[@]}"; do ((${#label} > width)) && width=${#label}; done

printf 'tapo fleet status — %s, night window %s-%s\n\n' \
    "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$night_start" "$night_end"
# shellcheck disable=SC2059  # the only expansion in the format is the host-column width
printf "%-${width}s  %-13s %4s %7s  %-12s %-6s %-7s %-9s %5s %5s %6s %7s %5s\n" \
    HOST UNIT RST UPTIME FINGERPRINT EXPECT DIGEST HOSTWATCH DISK LOAD TEMP PANLIM SENT

for idx in "${!targets[@]}"; do
    label="${labels[idx]}"
    if ((unreachable[idx])); then
        # shellcheck disable=SC2059
        printf "%-${width}s  %s\n" "$label" "UNREACHABLE"
        finding "$label" "unreachable — ${F["$idx/why"]}"
        continue
    fi
    # shellcheck disable=SC2059
    printf "%-${width}s  %-13s %4s %7s  %-12s %-6s %-7s %-9s %5s %5s %6s %7s %5s\n" \
        "$label" \
        "$(unit_cell "$idx")" \
        "$(f "$idx" unit_restarts)" \
        "$(fmt_age "$(f "$idx" unit_uptime)")" \
        "$(f "$idx" fp_running)" \
        "$(expect_cell "$idx")" \
        "$(digest_cell "$idx")" \
        "$(watch_cell "$idx")" \
        "$(f "$idx" disk_pct)" \
        "$(f "$idx" load1)" \
        "$(f "$idx" temp_c)" \
        "$(f "$idx" panlimit_night)" \
        "$(f "$idx" sent_night)"
done

# ── scorer ────────────────────────────────────────────────────────────────────────────
# Its own line because it has none of the columns above: one process serving every site,
# where "is it up" is far less interesting than "did it do any work since last time".
if ((want_scorer)); then
    scorer_line=""
    health="$(curl -fsS --max-time 10 "${scorer_url%/}/health" 2>/dev/null)"
    metrics="$(curl -fsS --max-time 10 "${scorer_url%/}/metrics" 2>/dev/null)"
    if [[ "$health" != *'"ok": true'* && "$health" != *'"ok":true'* ]]; then
        scorer_line="/health did not answer ok"
        finding scorer "$scorer_line"
    else
        state_dir="${TAPO_FLEET_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/tapo-fleet-status}"
        prev_file="$state_dir/scorer.prev"
        prev="$(cat "$prev_file" 2>/dev/null)"
        # The counters are cumulative, so a single reading says nothing. Remembering the
        # last one locally (never on a host) turns them into "what happened since the
        # previous review", which is the question the daily pass is actually asking.
        scorer_line="$(SCORER_PREV="$prev" python3 - "$metrics" <<'PY' 2>/dev/null
import json, os, sys, time

try:
    m = json.loads(sys.argv[1])
except (IndexError, ValueError):
    print("metrics did not parse")
    raise SystemExit(0)

now = int(time.time())
requests = int(m.get("requests", 0))
failed = int(m.get("failed", 0))
instance = str(m.get("instance_id", ""))
up = int(float(m.get("uptime_seconds", 0)))
parts = ["ok", "up %dh%02dm" % (up // 3600, up % 3600 // 60)]

prev = (os.environ.get("SCORER_PREV") or "").split()
if len(prev) >= 4 and prev[0].isdigit() and prev[1].isdigit():
    ago = now - int(prev[0])
    delta = requests - int(prev[1])
    if instance and prev[3] != instance:
        parts.append("restarted since the last check")
        parts.append("requests %d" % requests)
    else:
        parts.append("requests %d (+%d in %dm)" % (requests, delta, ago // 60))
else:
    parts.append("requests %d (first run, nothing to compare)" % requests)

parts.append("failed %d" % failed)
parts.append("p95 %.2fs" % float(m.get("request_seconds_p95", 0.0)))
print(", ".join(parts))
print("STATE %d %d %d %s" % (now, requests, failed, instance), file=sys.stderr)
PY
        )"
        # Refresh the remembered counters only when this run actually read them.
        if [[ -n "$metrics" ]]; then
            mkdir -p "$state_dir" 2>/dev/null && SCORER_PREV="$prev" python3 - "$metrics" \
                >"$prev_file" 2>/dev/null <<'PY'
import json, sys, time
try:
    m = json.loads(sys.argv[1])
except (IndexError, ValueError):
    raise SystemExit(1)
print("%d %d %d %s" % (int(time.time()), int(m.get("requests", 0)),
                       int(m.get("failed", 0)), m.get("instance_id", "-")))
PY
        fi
        [[ -n "$scorer_line" ]] || scorer_line="ok (metrics unavailable)"
    fi
    # shellcheck disable=SC2059
    printf "%-${width}s  %s\n" scorer "$scorer_line"
fi

# ── details and findings ──────────────────────────────────────────────────────────────
printf '\ndetails\n'
for idx in "${!targets[@]}"; do
    label="${labels[idx]}"
    ((unreachable[idx])) && continue
    detail="release $(f "$idx" release)"
    [[ "$(f "$idx" fp_source)" == "cli" ]] || detail="$detail, fingerprint read from the release name (the package could not report it)"
    printf '  %s: %s\n' "$label" "$detail"
    printf '  %s  digest due %s, last sent %s %s; frames %s; %s free\n' \
        "${label//?/ }" "$(f "$idx" digest_time)" "$(f "$idx" digest_last)" \
        "$(f "$idx" digest_at)" "$(f "$idx" night_label)" "$(f "$idx" disk_avail)"

    unit_state="$(f "$idx" unit_active)/$(f "$idx" unit_sub)"
    if [[ "$(f "$idx" unit_load)" != "loaded" ]]; then
        # A unit systemd has never heard of is inactive/dead too; reporting both reads as
        # two problems and sends the next person looking for the second one.
        finding "$label" "$unit is $(f "$idx" unit_load)"
    else
        case "$unit_state" in
            active/running) ;;
            */auto-restart) finding "$label" "$unit is crash-looping ($unit_state) — is-active alone would call this healthy" ;;
            *)              finding "$label" "$unit is $unit_state" ;;
        esac
    fi
    case "$(expect_cell "$idx")" in
        DRIFT) finding "$label" "fingerprint drift: running $(f "$idx" fp_running), env file expects $(f "$idx" fp_expected)" ;;
        unset) printf '  %s  no TAPO_EXPECTED_FINGERPRINT set — this host is not enrolled in the drift check\n' "${label//?/ }" ;;
    esac
    case "$(digest_cell "$idx")" in
        LATE)    finding "$label" "digest was due at $(f "$idx" digest_time) and has not gone out; last was $(f "$idx" digest_last)" ;;
        MISSING) finding "$label" "no digest since $(f "$idx" digest_last)" ;;
    esac
    case "$(watch_cell "$idx")" in
        INACTIVE) finding "$label" "$watch_unit is installed but not active" ;;
        FAIL*)    finding "$label" "last $watch_unit run ended $(f "$idx" watch_result)" ;;
    esac
    pct="$(f "$idx" disk_pct)"
    pct="${pct%\%}"
    [[ "$pct" =~ ^[0-9]+$ ]] && ((pct >= 90)) && finding "$label" "disk ${pct}% full"
    notes="$(f "$idx" notes)"
    [[ "$notes" == "-" ]] || finding "$label" "$notes"
done

if ((${#findings[@]})); then
    printf '\nfindings\n'
    printf '  %s\n' "${findings[@]}"
    exit 1
fi
printf '\nfindings: none\n'
