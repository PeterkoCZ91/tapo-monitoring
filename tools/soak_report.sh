#!/usr/bin/env bash
# One read-only report for a soak test before a release. Run it FROM THE WORKSTATION.
#
# A soak test asks the same questions of every host after N hours of running a candidate
# build: is the service up on the package we think, did it restart, did it throw, how many
# alerts did it send and how fast, what did the observe-only options say it would have
# done, and what does it cost. This asks them in one ssh login per host and prints one
# block each, optionally laid beside an earlier run so the deviations stand out.
#
# It only reads: systemctl show, journalctl, readlink and `tapo-monitor version` on the
# host; nothing is restarted or written there. The only local write is --baseline.
#
# Which machines are tested is configuration, not code (this repository is public):
# ssh aliases as arguments, or SOAK_HOSTS (space-separated; `label=alias` is accepted).
#
# usage: soak_report.sh [options] [HOST ...]
#   --since HOURS        journal window (default 24)
#   --user               hosts run the unit as a systemd USER unit (systemctl/journalctl --user)
#   --json               machine-readable report instead of the terminal one
#   --baseline FILE      compare with FILE; if it does not exist, save this run there
#   --update-baseline    with --baseline: after comparing, overwrite FILE with this run
#   --tolerance PERCENT  alert-volume deviation that counts (default 30)
#   --from-dir DIR       dry run: read DIR/<host>.raw instead of using ssh (fixtures)
#
#   SOAK_HOSTS    hosts when none are given as arguments
#   SOAK_UNIT     monitor unit               (default tapo-monitor.service)
#   SOAK_ROOT     package root on the host, relative to its $HOME when not absolute
#                 (default tapo-monitor; the code is read from <root>/current)
#   SOAK_PYTHON   interpreter on the host, same rule (default tapo-env/bin/python, then python3)
#   SOAK_TIMEOUT  seconds per host before it counts as unreachable (default 60)
#
# Exit status: 0 everything OK, 1 at least one finding or deviation, 2 bad invocation.
# An unreachable host is a finding and its own block, not a reason to abandon the report.
set -uo pipefail

die() { printf 'soak_report: %s\n' "$*" >&2; exit 2; }

usage() {
    cat >&2 <<'USAGE'
usage: soak_report.sh [--since HOURS] [--user] [--json] [--baseline FILE [--update-baseline]]
                      [--tolerance PERCENT] [--from-dir DIR] [HOST ...]

Hosts are ssh aliases, from the arguments or SOAK_HOSTS. Reads only.
See the header of this script for the environment knobs.
USAGE
    exit 2
}

since=24
scope=system
json=0
baseline=""
update_baseline=0
tolerance=30
from_dir=""
hosts=()

while (($#)); do
    case "$1" in
        --since)           since="${2:?--since needs hours}"; shift 2 ;;
        --user)            scope=user; shift ;;
        --json)            json=1; shift ;;
        --baseline)        baseline="${2:?--baseline needs a file}"; shift 2 ;;
        --update-baseline) update_baseline=1; shift ;;
        --tolerance)       tolerance="${2:?--tolerance needs a percentage}"; shift 2 ;;
        --from-dir)        from_dir="${2:?--from-dir needs a directory}"; shift 2 ;;
        -h|--help)         usage ;;
        -*)                die "unknown option $1" ;;
        *)                 hosts+=("$1"); shift ;;
    esac
done

if ((${#hosts[@]} == 0)) && [[ -n "${SOAK_HOSTS:-}" ]]; then
    read -r -a hosts <<<"$SOAK_HOSTS"
fi
((${#hosts[@]})) || usage

# Both values end up inside the remote command line, so they are validated, not trusted.
[[ "$since" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "--since wants a number of hours, got '$since'"
[[ "$tolerance" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "--tolerance wants a percentage, got '$tolerance'"
((update_baseline == 0)) || [[ -n "$baseline" ]] || die "--update-baseline needs --baseline"

unit="${SOAK_UNIT:-tapo-monitor.service}"
root="${SOAK_ROOT:-tapo-monitor}"
python_bin="${SOAK_PYTHON:-tapo-env/bin/python}"
host_timeout="${SOAK_TIMEOUT:-60}"
connect_timeout=$(( host_timeout / 4 ))
((connect_timeout > 3)) || connect_timeout=3
for value in "$unit" "$root" "$python_bin"; do
    [[ "$value" =~ ^[A-Za-z0-9_./@:-]+$ ]] || die "refusing unsafe value '$value'"
done

here="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
helper="$here/soak_report.py"

labels=()
targets=()
for entry in "${hosts[@]}"; do
    labels+=("${entry%%=*}")
    targets+=("${entry#*=}")
done

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# ── the probe that runs on each host ──────────────────────────────────────────────────
# Prints `@@section` markers and raw material; every number is computed locally. The last
# line is `@@end`: a probe that dies half-way (dropped ssh, killed journalctl) therefore
# has no end marker and the report says so instead of presenting partial counts as whole.
# The journal is filtered on the host (audit lines and the few patterns the report needs)
# so a busy day does not travel over a VPN link; the line total is counted before
# filtering. Arguments: unit scope root python since-hours.
remote_probe() {
    cat <<'PROBE'
unit="$1"; scope="$2"; root="$3"; py="$4"; since="$5"
sc=(systemctl); jc=(journalctl)
if [ "$scope" = user ]; then sc+=(--user); jc+=(--user); fi
case "$root" in /*) ;; *) root="$HOME/$root" ;; esac
case "$py" in /*) ;; *) py="$HOME/$py" ;; esac
[ -x "$py" ] || py=python3
echo "@@show"
"${sc[@]}" show "$unit" -p LoadState -p ActiveState -p SubState -p Result -p NRestarts \
    -p MemoryCurrent -p CPUUsageNSec -p ActiveEnterTimestampMonotonic 2>&1
echo "@@uptime"
cat /proc/uptime 2>/dev/null
echo "@@link"
readlink -f "$root/current" 2>/dev/null
echo "@@version"
(cd "$root/current" 2>/dev/null && PYTHONPATH=. "$py" -m tapo_monitor.cli version 2>&1)
echo "@@journal"
"${jc[@]}" -u "$unit" --since "$since hours ago" --no-pager -o short-iso 2>&1 | awk '
    /^-- / { next }
    { n++ }
    /audit |Traceback|ERROR|CRITICAL|PTZ stop guard|rotation stop .*shutdown tracking OFF unconfirmed|outbox|hub |systemd\[[0-9]+\]: (Started|Failed|.*Main process exited)|No journal files|insufficient permissions/ { print }
    END { print "@@total " n + 0 }'
echo "@@end"
PROBE
}

probe_host() {  # <target> <outfile>
    local target="$1" out="$2"
    # The script travels on stdin, so nothing host-specific is interpolated into a quoted
    # remote command line.
    remote_probe | timeout "$host_timeout" ssh -o BatchMode=yes -o ConnectTimeout="$connect_timeout" \
        "$target" bash -s -- "$unit" "$scope" "$root" "$python_bin" "$since" >"$out" 2>"$out.err"
}

probes=()
for i in "${!targets[@]}"; do
    label="${labels[$i]}"
    raw="$tmpdir/$i.raw"
    if [[ -n "$from_dir" ]]; then
        cp "$from_dir/${targets[$i]}.raw" "$raw" 2>/dev/null || : >"$raw"
    else
        probe_host "${targets[$i]}" "$raw" || : # unreachable/failed: the empty probe says so
    fi
    probes+=("$label=$raw")
done

args=(--since "$since" --tolerance "$tolerance")
((json)) && args+=(--json)
[[ -n "$baseline" ]] && args+=(--baseline "$baseline")
((update_baseline)) && args+=(--update-baseline)
python3 "$helper" "${args[@]}" "${probes[@]}"
