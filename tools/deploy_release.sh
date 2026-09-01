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
host="" ref="HEAD" unit="tapo-monitor.service" python_bin="~/tapo-env/bin/python"
restart_cmd="" env_file=""
while (($#)); do
    case "$1" in
        --unit)        unit="${2:?--unit needs a value}"; shift 2 ;;
        --python)      python_bin="${2:?--python needs a value}"; shift 2 ;;
        --restart-cmd) restart_cmd="${2:?--restart-cmd needs a value}"; shift 2 ;;
        --env-file)    env_file="${2:?--env-file needs a value}"; shift 2 ;;
        -*)            die "unknown option $1" ;;
        *)             if [[ -z "$host" ]]; then host="$1"
                       elif [[ "$ref" == "HEAD" ]]; then ref="$1"
                       else die "unexpected argument $1"; fi; shift ;;
    esac
done
[[ -n "$host" ]] || die "usage: deploy_release.sh <ssh-host> [git-ref] [options]"
restart_cmd="${restart_cmd:-sudo systemctl restart $unit}"

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
tmp_link="$root/.current.next.$$"
ln -s "releases/$release" "$tmp_link"
mv -T "$tmp_link" "$root/current"
echo "deploy_release: current -> releases/$release"
REMOTE

# ── restart, then believe only what the host reports back ─────────────────────────────
echo "deploy_release: restarting via: $restart_cmd"
# shellcheck disable=SC2029  # client-side expansion is the point: the arguments are
# %q-quoted (or a literal command) composed here and executed on the host.
ssh "$host" "$restart_cmd"

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
