#!/usr/bin/env bash
# Post-deploy verification for one monitor host. Run it ON the host, after the restart.
#
# The scorer had a rollout check and the daemon had none, so "the files are new" was the
# only evidence a deploy ever produced. This asserts the parts that have actually failed
# silently before: a crash-looping unit, a half-copied package, a config the new code
# rejects, and an exception on the first tick.
#
# usage: check_monitor_rollout.sh [EXPECTED_FINGERPRINT]
#   TAPO_MONITOR_UNIT    systemd unit to inspect      (default tapo-monitor.service)
#   TAPO_MONITOR_PYTHON  interpreter of the venv      (default python3)
#   TAPO_MONITOR_CONFIG  config for the selfcheck     (default <package root>/cameras.yaml)
#   TAPO_MONITOR_ENV     env file to source first     (default: the unit's EnvironmentFile)
set -uo pipefail

expected_fingerprint="${1:-}"
unit="${TAPO_MONITOR_UNIT:-tapo-monitor.service}"
python_bin="${TAPO_MONITOR_PYTHON:-python3}"
# A deployed host is an rsync copy, not an installed package: tools/ sits beside
# tapo_monitor/, so the package root is one level up from this script.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config="${TAPO_MONITOR_CONFIG:-$root/cameras.yaml}"
failures=()

# Assert the credentials the service actually gets: read the env file from the unit
# itself rather than guessing a host-specific path.
env_file="${TAPO_MONITOR_ENV:-$(systemctl show -p EnvironmentFiles --value "$unit" 2>/dev/null \
    | tr ' ' '\n' | sed 's/^-//' | grep -m1 '^/' || true)}"
if [[ -n "$env_file" && -r "$env_file" ]]; then
    set -a
    # Host-specific path, so shellcheck cannot follow it.
    # shellcheck source=/dev/null
    . "$env_file"
    set +a
fi

run_cli() { (cd "$root" && PYTHONPATH="$root" "$python_bin" -m tapo_monitor.cli "$@"); }

fail() { echo "  $1: FAILED${2:+ ($2)}"; failures+=("$1"); }
pass() { echo "  $1: ok${2:+ ($2)}"; }
warn() { echo "  $1: unknown${2:+ ($2)}"; }

echo "monitor rollout check: $unit (package $root)"

state="$(systemctl show -p ActiveState --value "$unit" 2>/dev/null || true)"
sub_state="$(systemctl show -p SubState --value "$unit" 2>/dev/null || true)"
restarts="$(systemctl show -p NRestarts --value "$unit" 2>/dev/null || true)"
if [[ "$state" == "active" && "$sub_state" == "running" ]]; then
    pass unit "running, NRestarts=${restarts:-?}"
else
    # auto-restart is the crash loop this check exists for: `is-active` alone can report
    # "activating" for a unit that is dying every RestartSec seconds.
    fail unit "ActiveState=$state SubState=$sub_state"
fi

version_output="$(run_cli version 2>&1)" || {
    fail version "the deployed package cannot even report its version"
    version_output=""
}
fingerprint="$(printf '%s\n' "$version_output" | awk '/^package /{print $2}')"
if [[ -n "$fingerprint" ]]; then
    if [[ -z "$expected_fingerprint" ]]; then
        pass fingerprint "$fingerprint (nothing to compare against)"
    elif [[ "$fingerprint" == "$expected_fingerprint" ]]; then
        pass fingerprint "$fingerprint"
    else
        fail fingerprint "host has $fingerprint, expected $expected_fingerprint"
    fi
fi

if run_cli selfcheck "$config" > /tmp/monitor-selfcheck.$$ 2>&1; then
    pass selfcheck
else
    fail selfcheck "see output below"
fi
sed 's/^/    /' /tmp/monitor-selfcheck.$$
rm -f /tmp/monitor-selfcheck.$$

since="$(systemctl show -p ActiveEnterTimestamp --value "$unit" 2>/dev/null || true)"
journal="$(journalctl -u "$unit" ${since:+--since "$since"} --no-pager 2>/dev/null || true)"
if [[ -z "$journal" ]]; then
    warn journal "no readable journal for this unit"
else
    loaded="$(printf '%s\n' "$journal" | grep -o 'loaded [0-9]* camera(s)' | tail -1)"
    if [[ -n "$loaded" ]]; then
        pass startup "$loaded"
    else
        fail startup "no 'loaded N camera(s)' line since the unit started"
    fi
    # Content grep on purpose: systemd files Python stdout as INFO regardless of the
    # record's level, so `journalctl -p warning` returns nothing even when it should.
    problems="$(printf '%s\n' "$journal" | grep -ciE 'traceback|unhandled|AttributeError|ImportError|TypeError' || true)"
    if [[ "$problems" -eq 0 ]]; then
        pass journal "no exception since start"
    else
        fail journal "$problems exception line(s) since start"
    fi
fi

# Liveness is deliberately reported, not asserted: the daemon logs per decision, not per
# poll, so a quiet camera produces a quiet journal and that is not a failure.
health_state="$HOME/.local/state/tapo-monitor/health.json"
if [[ -f "$health_state" ]]; then
    pass health-state "last written $(date -r "$health_state" '+%Y-%m-%d %H:%M:%S')"
else
    warn health-state "not written yet (normal until the first observation)"
fi

if ((${#failures[@]})); then
    echo "monitor rollout check FAILED: ${failures[*]}"
    exit 1
fi
echo "monitor rollout check: ok"
