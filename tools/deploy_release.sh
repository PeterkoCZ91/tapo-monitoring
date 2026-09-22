#!/usr/bin/env bash
# Deploy one git ref to a host as a self-contained release directory, then switch to it
# atomically. Run it FROM THE WORKSTATION, inside the repo checkout.
#
# The rsync-in-place path this replaces overwrote the only copy of whatever the host was
# running, so "roll back" meant finding the right tarball while the unit crash-looped.
# Here every deploy lands in its own directory, proves itself with a selfcheck *before*
# it can take over, and only then becomes `current` — one rename, no partially-switched
# state. Rollback is tools/rollback_release.sh re-pointing the same symlink.
#
# Host layout it creates and maintains:
#   ~/tapo-monitor/
#     cameras.yaml                        host-owned config, shared by all releases
#     current -> releases/<UTC-ts>-<fp>/  what the systemd unit runs
#     releases/<UTC-ts>-<fp>/             one extracted full package per deploy
#       config-snapshot/                  cameras.yaml + env file as this release saw them
#
# usage: deploy_release.sh <ssh-host> [git-ref] [options]
#   git-ref              what to ship; must be committed — the tree comes from
#                        `git archive`, so uncommitted edits can never deploy (default HEAD)
#   --unit NAME          systemd unit to restart and read the EnvironmentFile from
#                        (default tapo-monitor.service)
#   --python PATH        venv interpreter on the host (default ~/tapo-env/bin/python)
#   --restart-cmd CMD    how to restart on the host (default: sudo systemctl restart <unit>).
#                        Must be non-interactive. A host whose operator has no sudo passes
#                        e.g. --restart-cmd 'systemctl --user restart tapo-monitor'.
#   --env-file PATH      env file on the host to snapshot and source for the selfcheck
#                        (default: discovered from the unit's EnvironmentFile=)
#   --health-wait SECS   after the restart, wait this long and require the unit to be
#                        active and not crash-looping; if it is not, re-point `current`
#                        at the release that was live before and restart again
#                        (default 30, 0 disables; skipped for --restart-cmd true)
#
# Manual dry run (nothing needs to be a production host): point it at any ssh-reachable
# account whose ~/tapo-monitor is expendable, seed ~/tapo-monitor/cameras.yaml and an env
# file carrying the credential vars that config names, and pass --restart-cmd true plus
# --env-file <that file>; every step short of the systemd restart then runs for real.
set -euo pipefail

KEEP_RELEASES=5   # per host, newest first; the one `current` points to is never pruned

die() { echo "deploy_release: $*" >&2; exit 1; }

# shellcheck disable=SC2088  # the tilde is deliberately literal here: the remote side
# expands it against the host's $HOME, not the workstation's.
host="" ref="" unit="tapo-monitor.service" python_bin="~/tapo-env/bin/python"
restart_cmd="" env_file="" health_wait=30
while (($#)); do
    case "$1" in
        --unit)        unit="${2:?--unit needs a value}"; shift 2 ;;
        --python)      python_bin="${2:?--python needs a value}"; shift 2 ;;
        --restart-cmd) restart_cmd="${2:?--restart-cmd needs a value}"; shift 2 ;;
        --env-file)    env_file="${2:?--env-file needs a value}"; shift 2 ;;
        --health-wait) health_wait="${2:?--health-wait needs a value}"; shift 2 ;;
        -*)            die "unknown option $1" ;;
        # "$ref is still empty" is the test for "no ref given yet": using the default
        # value as the sentinel meant an explicit HEAD reopened the slot, so a third
        # argument became the ref and the deploy shipped something the command line
        # never named.
        *)             if [[ -z "$host" ]]; then host="$1"
                       elif [[ -z "$ref" ]]; then ref="$1"
                       else die "unexpected argument $1"; fi; shift ;;
    esac
done
[[ -n "$host" ]] || die "usage: deploy_release.sh <ssh-host> [git-ref] [options]"
ref="${ref:-HEAD}"
restart_cmd="${restart_cmd:-sudo systemctl restart $unit}"
[[ "$health_wait" =~ ^[0-9]+$ ]] || die "--health-wait needs a whole number of seconds"

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

# ── stage: the committed tree, never the working copy ─────────────────────────────────
git -C "$repo" archive --format=tar "$ref" | tar -C "$stage" -xf - \
    || die "git archive $ref failed — is it committed?"

# The same fingerprint the host will report: computed by the staged CLI over the staged
# modules, so what we verify later is exactly what we shipped, not what we think we did.
fingerprint="$(cd "$stage" && python3 -m tapo_monitor.cli version | awk '/^package /{print $2}')"
[[ "$fingerprint" =~ ^[0-9a-f]{12}$ ]] \
    || die "could not fingerprint the staged tree (got '$fingerprint')"
release="$(date -u +%Y%m%dT%H%M%SZ)-$fingerprint"
echo "deploy_release: shipping $ref as releases/$release to $host"

# ── transfer: full package into its own directory, touching nothing that runs ─────────
# shellcheck disable=SC2029  # the release name is generated locally on purpose; only
# $HOME stays escaped for the host.
tar -C "$stage" -cf - . | ssh "$host" "set -e
    dir=\"\$HOME/tapo-monitor/releases/$release\"
    mkdir -p \"\$dir\" && tar -C \"\$dir\" -xf -"

# ── on the host: snapshot config+env, selfcheck inside the release, atomic switch ─────
remote_args="$(printf ' %q' "$release" "$unit" "$python_bin" "$env_file")"
# shellcheck disable=SC2029  # client-side expansion is the point: the arguments are
# %q-quoted (or a literal command) composed here and executed on the host.
ssh "$host" "bash -s --$remote_args" <<'REMOTE'
set -euo pipefail
release="$1" unit="$2" python_bin="$3" env_file="$4"
root="$HOME/tapo-monitor"
release_dir="$root/releases/$release"
python_bin="${python_bin/#~\//$HOME/}"
config="$root/cameras.yaml"

[[ -x "$python_bin" ]] || { echo "deploy_release: no interpreter at $python_bin on the host" >&2; exit 1; }
[[ -f "$config" ]] || { echo "deploy_release: no $config on the host — the release layout expects the host-owned config there" >&2; exit 1; }
if [[ -e "$root/current" && ! -L "$root/current" ]]; then
    echo "deploy_release: $root/current exists and is not a symlink — this host still has the rsync layout; migrate first (docs/operations.md, 'Release deploys and rollback')" >&2
    exit 1
fi

if [[ -z "$env_file" ]]; then
    env_file="$(systemctl show -p EnvironmentFiles --value "$unit" 2>/dev/null \
        | tr ' ' '\n' | sed 's/^-//' | grep -m1 '^/' || true)"
fi

# Snapshot what this release was validated against: cameras.yaml is shared across
# releases, so without the copy "what config did the rolled-back release pass with"
# has no answer. 600 because the env file holds credentials.
snapshot="$release_dir/config-snapshot"
mkdir -p "$snapshot"
cp "$config" "$snapshot/cameras.yaml"
if [[ -n "$env_file" ]]; then
    [[ -r "$env_file" ]] || { echo "deploy_release: env file $env_file is not readable on the host" >&2; exit 1; }
    cp "$env_file" "$snapshot/${env_file##*/}"
else
    echo "deploy_release: NOTE: unit $unit names no EnvironmentFile; snapshot holds only cameras.yaml"
fi
chmod 600 "$snapshot"/*

# Selfcheck FROM INSIDE the release (with -m, the cwd package wins over any installed
# copy) and under the env the unit will see — a release that cannot pass here must
# never become `current`.
(
    if [[ -n "$env_file" ]]; then
        set -a
        # shellcheck source=/dev/null
        . "$env_file"
        set +a
    fi
    cd "$release_dir"
    "$python_bin" -m tapo_monitor.cli selfcheck "$config"
) || { echo "deploy_release: selfcheck FAILED in releases/$release — 'current' was not switched (the directory is kept for inspection)" >&2; exit 1; }

# Atomic switch: `ln -sfn` onto an existing link is unlink+symlink, two syscalls with a
# no-`current` window between them that a unit restart could land in. Build the new link
# aside and rename it over — rename(2) is atomic.
# Remember what `current` pointed at, for the health check after the restart.
prev_target="$(readlink "$root/current" 2>/dev/null || true)"
printf '%s\n' "${prev_target#releases/}" > "$root/.previous_release"
tmp_link="$root/.current.next.$$"
ln -s "releases/$release" "$tmp_link"
mv -T "$tmp_link" "$root/current"
echo "deploy_release: current -> releases/$release"

# Keep the digest's drift check pointing at what this deploy intended — the fingerprint
# is the release name's own suffix. Update-only: a host that never opted into
# TAPO_EXPECTED_FINGERPRINT stays unenrolled, and the daemon reads the new value on the
# restart that follows.
if [[ -n "$env_file" && -w "$env_file" ]] \
        && grep -q '^TAPO_EXPECTED_FINGERPRINT=' "$env_file"; then
    sed -i "s/^TAPO_EXPECTED_FINGERPRINT=.*/TAPO_EXPECTED_FINGERPRINT=${release##*-}/" "$env_file"
    echo "deploy_release: TAPO_EXPECTED_FINGERPRINT -> ${release##*-}"
fi
REMOTE

# ── restart, then believe only what the host reports back ─────────────────────────────
echo "deploy_release: restarting via: $restart_cmd"
# What was live before this deploy (written just before the switch) and how often
# systemd had already restarted the unit — the reference for the health check below.
# shellcheck disable=SC2029  # unit is composed here on purpose, it is a plain unit name.
prev_release="$(ssh "$host" 'cat "$HOME/tapo-monitor/.previous_release" 2>/dev/null' || true)"
# shellcheck disable=SC2029
restarts_before="$(ssh "$host" "systemctl show -p NRestarts --value $unit 2>/dev/null" || true)"
# shellcheck disable=SC2029  # client-side expansion is the point: the arguments are
# %q-quoted (or a literal command) composed here and executed on the host.
ssh "$host" "$restart_cmd"

# Believe the unit, not the exit code of the restart command: a release that passed its
# selfcheck can still crash-loop once it runs for real. NRestarts counts only automatic
# restarts (Restart=always), so a manual `systemctl restart` does not move it and two or
# more new ones inside the window mean the unit is flapping.
if ((health_wait > 0)) && [[ "$restart_cmd" != "true" ]]; then
    if [[ ! "$restarts_before" =~ ^[0-9]+$ ]]; then
        echo "deploy_release: health check skipped — cannot read NRestarts of $unit on $host" >&2
    else
        echo "deploy_release: waiting ${health_wait}s to see the unit stay up"
        sleep "$health_wait"
        # shellcheck disable=SC2029
        state="$(ssh "$host" "systemctl is-active $unit" || true)"
        # shellcheck disable=SC2029
        restarts_after="$(ssh "$host" "systemctl show -p NRestarts --value $unit" || true)"
        [[ "$restarts_after" =~ ^[0-9]+$ ]] || restarts_after="$restarts_before"
        if [[ "$state" != "active" ]] || ((restarts_after - restarts_before >= 2)); then
            echo "deploy_release: HEALTH CHECK FAILED — $unit is '$state', $((restarts_after - restarts_before)) automatic restarts in ${health_wait}s" >&2
            if [[ -n "$prev_release" && "$prev_release" != "$release" && "$prev_release" != "current" ]]; then
                echo "deploy_release: rolling back to releases/$prev_release" >&2
                # shellcheck disable=SC2029
                ssh "$host" "set -e; cd \"\$HOME/tapo-monitor\"
                    test -d \"releases/$prev_release\"
                    ln -s \"releases/$prev_release\" .current.rollback.\$\$
                    mv -T .current.rollback.\$\$ current"
                # shellcheck disable=SC2029
                ssh "$host" "$restart_cmd"
                die "rolled back to releases/$prev_release; the failed release is kept in releases/$release"
            fi
            die "no previous release to roll back to; $unit needs attention on $host"
        fi
        echo "deploy_release: $unit stayed active, $((restarts_after - restarts_before)) automatic restarts"
    fi
fi

remote_args="$(printf ' %q' "$fingerprint" "$python_bin" "$KEEP_RELEASES")"
# shellcheck disable=SC2029  # client-side expansion is the point: the arguments are
# %q-quoted (or a literal command) composed here and executed on the host.
ssh "$host" "bash -s --$remote_args" <<'REMOTE'
set -euo pipefail
expected="$1" python_bin="$2" keep="$3"
root="$HOME/tapo-monitor"
python_bin="${python_bin/#~\//$HOME/}"

actual="$(cd "$root/current" && "$python_bin" -m tapo_monitor.cli version | awk '/^package /{print $2}')"
if [[ "$actual" != "$expected" ]]; then
    echo "deploy_release: VERIFY FAILED — current reports $actual, staged tree was $expected" >&2
    exit 1
fi
echo "deploy_release: verified — current runs package $actual"

# Prune to the newest $keep releases. `current` is exempt even when old: deleting the
# code a unit is running turns the next restart into an outage.
current_target="$(readlink -f "$root/current")"
mapfile -t releases < <(find "$root/releases" -mindepth 1 -maxdepth 1 -type d | sort)
count=${#releases[@]}
if ((count > keep)); then
    for dir in "${releases[@]:0:count-keep}"; do
        [[ "$(readlink -f "$dir")" == "$current_target" ]] && continue
        rm -rf -- "$dir"
        echo "deploy_release: pruned old release ${dir##*/}"
    done
fi
REMOTE

echo "deploy_release: done — follow up on the host with:"
echo "  ~/tapo-monitor/current/tools/check_monitor_rollout.sh $fingerprint"
