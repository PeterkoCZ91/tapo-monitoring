#!/usr/bin/env bash
# Roll a host back (or forward) to a release that already exists on it. Run it FROM THE
# WORKSTATION. Nothing is copied and nothing is rebuilt: every release directory under
# ~/tapo-monitor/releases/ already passed its deploy-time selfcheck, so recovering from
# a bad deploy is re-pointing the `current` symlink and restarting — seconds, not a hunt
# for the right tarball while the unit crash-loops.
#
# usage: rollback_release.sh <ssh-host> [release-name] [options]
#   release-name         a directory name from the listing (e.g. 20260901T101500Z-3f9c2d81a0b4).
#                        Omit it to list the releases the host has, newest last.
#   --unit NAME          systemd unit to restart (default tapo-monitor.service)
#   --python PATH        venv interpreter on the host (default ~/tapo-env/bin/python)
#   --restart-cmd CMD    how to restart on the host (default: sudo systemctl restart <unit>).
#                        Must be non-interactive. A host whose operator has no sudo passes
#                        e.g. --restart-cmd 'systemctl --user restart tapo-monitor'.
#
# Manual dry run: same recipe as deploy_release.sh — an expendable ~/tapo-monitor on any
# ssh-reachable account, --restart-cmd true; deploy twice, roll back to the first.
set -euo pipefail

die() { echo "rollback_release: $*" >&2; exit 1; }

# shellcheck disable=SC2088  # the tilde is deliberately literal here: the remote side
# expands it against the host's $HOME, not the workstation's.
host="" release="" unit="tapo-monitor.service" python_bin="~/tapo-env/bin/python" restart_cmd=""
while (($#)); do
    case "$1" in
        --unit)        unit="${2:?--unit needs a value}"; shift 2 ;;
        --python)      python_bin="${2:?--python needs a value}"; shift 2 ;;
        --restart-cmd) restart_cmd="${2:?--restart-cmd needs a value}"; shift 2 ;;
        -*)            die "unknown option $1" ;;
        *)             if [[ -z "$host" ]]; then host="$1"
                       elif [[ -z "$release" ]]; then release="$1"
                       else die "unexpected argument $1"; fi; shift ;;
    esac
done
[[ -n "$host" ]] || die "usage: rollback_release.sh <ssh-host> [release-name] [options]"
restart_cmd="${restart_cmd:-sudo systemctl restart $unit}"

# ── no release named: show what the host has to offer ─────────────────────────────────
if [[ -z "$release" ]]; then
    # shellcheck disable=SC2029  # client-side expansion is the point: the arguments are
    # %q-quoted (or a literal command) composed here and executed on the host.
    ssh "$host" bash -s <<'REMOTE'
set -euo pipefail
root="$HOME/tapo-monitor"
[[ -d "$root/releases" ]] || { echo "rollback_release: no $root/releases on this host — nothing was ever deployed with deploy_release.sh" >&2; exit 1; }
current_target="$(readlink -f "$root/current" 2>/dev/null || true)"
found=0
printf '%-32s %-14s %s\n' "release" "fingerprint" ""
while IFS= read -r dir; do
    found=1
    name="${dir##*/}"
    marker=""
    [[ "$(readlink -f "$dir")" == "$current_target" ]] && marker="<- current"
    # The name is <UTC-ts>-<fingerprint>: the digest the deploy verified is part of
    # the directory name, so the listing needs no interpreter to show it.
    printf '%-32s %-14s %s\n' "$name" "${name#*-}" "$marker"
done < <(find "$root/releases" -mindepth 1 -maxdepth 1 -type d | sort)
((found)) || { echo "rollback_release: $root/releases is empty" >&2; exit 1; }
REMOTE
    exit 0
fi

# ── re-point, restart, verify ──────────────────────────────────────────────────────────
remote_args="$(printf ' %q' "$release" "$unit")"
# shellcheck disable=SC2029  # client-side expansion is the point: the arguments are
# %q-quoted (or a literal command) composed here and executed on the host.
ssh "$host" "bash -s --$remote_args" <<'REMOTE'
set -euo pipefail
release="$1" unit="$2"
root="$HOME/tapo-monitor"
[[ -d "$root/releases/$release" ]] || { echo "rollback_release: no $root/releases/$release on this host — run without a release name to list them" >&2; exit 1; }
if [[ -e "$root/current" && ! -L "$root/current" ]]; then
    echo "rollback_release: $root/current is not a symlink — this host still has the rsync layout" >&2
    exit 1
fi
# Same switch as the deploy: build the link aside, rename over — atomic, no window
# in which `current` does not exist.
tmp_link="$root/.current.next.$$"
ln -s "releases/$release" "$tmp_link"
mv -T "$tmp_link" "$root/current"
echo "rollback_release: current -> releases/$release"

# A rollback is the new intent: without this, the next digest would report the release
# the operator deliberately returned to as drift. Update-only, same as the deploy.
env_file="$(systemctl show -p EnvironmentFiles --value "$unit" 2>/dev/null \
    | tr ' ' '\n' | sed 's/^-//' | grep -m1 '^/' || true)"
if [[ -n "$env_file" && -w "$env_file" ]] \
        && grep -q '^TAPO_EXPECTED_FINGERPRINT=' "$env_file"; then
    sed -i "s/^TAPO_EXPECTED_FINGERPRINT=.*/TAPO_EXPECTED_FINGERPRINT=${release##*-}/" "$env_file"
    echo "rollback_release: TAPO_EXPECTED_FINGERPRINT -> ${release##*-}"
fi
REMOTE

echo "rollback_release: restarting via: $restart_cmd"
# shellcheck disable=SC2029  # client-side expansion is the point: the arguments are
# %q-quoted (or a literal command) composed here and executed on the host.
ssh "$host" "$restart_cmd"

remote_args="$(printf ' %q' "$release" "$python_bin")"
# shellcheck disable=SC2029  # client-side expansion is the point: the arguments are
# %q-quoted (or a literal command) composed here and executed on the host.
ssh "$host" "bash -s --$remote_args" <<'REMOTE'
set -euo pipefail
release="$1" python_bin="$2"
root="$HOME/tapo-monitor"
python_bin="${python_bin/#~\//$HOME/}"
[[ -x "$python_bin" ]] || { echo "rollback_release: no interpreter at $python_bin on the host" >&2; exit 1; }

# The expected digest is the release name's own suffix — the deploy that created the
# directory verified it, so a mismatch now means the tree changed since.
expected="${release#*-}"
actual="$(cd "$root/current" && "$python_bin" -m tapo_monitor.cli version | awk '/^package /{print $2}')"
if [[ ! "$expected" =~ ^[0-9a-f]{12}$ ]]; then
    echo "rollback_release: NOTE: '$release' carries no fingerprint to verify against; current reports package $actual"
elif [[ "$actual" != "$expected" ]]; then
    echo "rollback_release: VERIFY FAILED — current reports $actual, the release name says $expected" >&2
    exit 1
else
    echo "rollback_release: verified — current runs package $actual"
fi
REMOTE

echo "rollback_release: done — follow up on the host with:"
echo "  ~/tapo-monitor/current/tools/check_monitor_rollout.sh ${release#*-}"
