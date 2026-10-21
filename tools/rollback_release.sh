#!/usr/bin/env bash
# Roll a host back (or forward) to a release that already exists on it. Run it FROM THE
# WORKSTATION. Nothing is copied and nothing is rebuilt: every release directory under
# ~/tapo-monitor/releases/ already passed its deploy-time selfcheck, so recovering from
# a bad deploy is re-pointing the `current` symlink and restarting — seconds, not a hunt
# for the right tarball while the unit crash-loops.
#
# Code only: the venv is shared by every release, so dependencies a deploy installed with
# --sync-deps stay installed. The restore command for them is in that release's
# deps-before.txt (releases/<name>/deps-before.txt on the host).
#
# usage: rollback_release.sh <ssh-host> [release-name] [options]
#   release-name         a directory name from the listing (e.g. 20260901T101500Z-3f9c2d81a0b4).
#                        Omit it to list the releases the host has, newest last.
#   --unit NAME          systemd unit to restart (default tapo-monitor.service)
#   --python PATH        venv interpreter on the host (default ~/tapo-env/bin/python)
#   --user               the unit is a systemd USER unit (`systemctl --user`, lingering):
#                        the EnvironmentFile lookup goes to the user manager and the
#                        default restart becomes `systemctl --user restart <unit>`.
#   --restart-cmd CMD    how to restart on the host (default: sudo systemctl restart <unit>,
#                        or systemctl --user restart <unit> with --user). Must be
#                        non-interactive.
#
# Manual dry run: same recipe as deploy_release.sh — an expendable ~/tapo-monitor on any
# ssh-reachable account, --restart-cmd true; deploy twice, roll back to the first.
set -euo pipefail

die() { echo "rollback_release: $*" >&2; exit 1; }

# shellcheck disable=SC2088  # the tilde is deliberately literal here: the remote side
# expands it against the host's $HOME, not the workstation's.
host="" release="" unit="tapo-monitor.service" python_bin="~/tapo-env/bin/python" restart_cmd="" sd_scope=""
while (($#)); do
    case "$1" in
        --unit)        unit="${2:?--unit needs a value}"; shift 2 ;;
        --python)      python_bin="${2:?--python needs a value}"; shift 2 ;;
        --restart-cmd) restart_cmd="${2:?--restart-cmd needs a value}"; shift 2 ;;
        --user)        sd_scope="--user"; shift ;;
        -*)            die "unknown option $1" ;;
        *)             if [[ -z "$host" ]]; then host="$1"
                       elif [[ -z "$release" ]]; then release="$1"
                       else die "unexpected argument $1"; fi; shift ;;
    esac
done
[[ -n "$host" ]] || die "usage: rollback_release.sh <ssh-host> [release-name] [options]"
if [[ -n "$sd_scope" ]]; then
    restart_cmd="${restart_cmd:-systemctl --user restart $unit}"
else
    restart_cmd="${restart_cmd:-sudo systemctl restart $unit}"
fi

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
# One deploy/rollback per host at a time (same lock file as deploy_release.sh).
lock_dir="${XDG_STATE_HOME:-$HOME/.local/state}/tapo-monitor"
mkdir -p "$lock_dir"
if command -v flock >/dev/null 2>&1; then
    exec 9>"$lock_dir/release-${host//[^A-Za-z0-9_.-]/_}.lock"
    flock -n 9 || die "another deploy or rollback to $host is already running (lock $lock_dir)"
else
    echo "rollback_release: WARNING: no flock on the workstation — concurrent runs are not prevented" >&2
fi
# ── host-side lease: held for the WHOLE sequence ──────────────────────────────────────
# The flock on the host only covers the switch (restart, health check and rollback are
# separate ssh calls), so a second workstation could interleave its own health check or
# rollback with ours. The lease is a file, releases/.deploy.lock, created atomically
# (hard link of a fully written temp file) and holding "<workstation-id> <pid> <epoch>"
# (epoch from the HOST clock). A lease younger than the limit refuses us; an older one is
# taken over with a warning. Every exit path releases it through the EXIT trap below, and
# only if it still carries our id.
lease_limit="${TAPO_DEPLOY_LEASE_SECS:-1200}"
[[ "$lease_limit" =~ ^[0-9]+$ ]] || die "TAPO_DEPLOY_LEASE_SECS needs a whole number of seconds"
lease_id="${HOSTNAME:-ws}"; lease_id="${lease_id//[^A-Za-z0-9_.-]/_}-$$-$RANDOM"
lease_held=0
acquire_lease() {   # $1 = 1 to create ~/tapo-monitor/releases when missing
    local rc=0
    # shellcheck disable=SC2029  # %q-quoted arguments composed here on purpose.
    ssh "$host" "bash -s -- $(printf ' %q' "$lease_id" "$$" "$lease_limit" "$1" "rollback_release")" <<'LEASE' || rc=$?
set -u
id="$1" pid="$2" limit="$3" mk="$4" me="$5"
dir="$HOME/tapo-monitor/releases"
f="$dir/.deploy.lock"
if [[ ! -d "$dir" ]]; then
    if [[ "$mk" == 1 ]]; then mkdir -p "$dir" || exit 1; else exit 0; fi
fi
write_lease() {
    local tmp="$f.$id.tmp"
    printf '%s %s %s\n' "$id" "$pid" "$(date +%s)" >"$tmp" || return 1
    ln "$tmp" "$f" 2>/dev/null; local r=$?
    rm -f "$tmp"
    return $r
}
write_lease && exit 0
read -r owner opid ots <"$f" 2>/dev/null || true
if [[ "${ots:-}" =~ ^[0-9]+$ ]]; then
    age=$(( $(date +%s) - ots )); ((age >= 0)) || age=0
else
    age=$((limit + 1))
fi
if ((age < limit)); then
    echo "$me: another deploy or rollback holds the lease $f on this host: owner ${owner:-?} pid ${opid:-?}, ${age}s old (a lease older than ${limit}s counts as stale)" >&2
    exit 3
fi
echo "$me: WARNING: taking over a stale lease from ${owner:-?} pid ${opid:-?}, ${age}s old (limit ${limit}s)" >&2
stale="$f.stale.$id"
mv "$f" "$stale" 2>/dev/null || true
read -r owner2 _ <"$stale" 2>/dev/null || true
if [[ "${owner2:-}" != "${owner:-}" ]]; then   # we moved someone's fresh lease: put it back
    ln "$stale" "$f" 2>/dev/null || true
    rm -f "$stale"
    echo "$me: the lease changed hands while taking it over — not proceeding" >&2
    exit 3
fi
rm -f "$stale"
write_lease || { echo "$me: lost the race for the lease $f" >&2; exit 3; }
LEASE
    ((rc == 0)) || die "could not take the host lease on $host (exit $rc) — nothing was changed"
    lease_held=1
}
release_lease() {
    ((lease_held)) || return 0
    lease_held=0
    local t=()
    command -v timeout >/dev/null 2>&1 && t=(timeout 30)
    # shellcheck disable=SC2029
    "${t[@]}" ssh "$host" "bash -s -- $(printf ' %q' "$lease_id")" <<'RELEASE' \
        || echo "rollback_release: WARNING: could not release the lease on $host — it ages out after ${lease_limit}s" >&2
f="$HOME/tapo-monitor/releases/.deploy.lock"
[[ -f "$f" ]] || exit 0
read -r owner _ <"$f" 2>/dev/null || true
if [[ "${owner:-}" == "$1" ]]; then rm -f "$f"; else echo "lease now belongs to ${owner:-?}, left alone" >&2; fi
RELEASE
}
cleanup() { rm -rf "${stage:-}"; release_lease; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
stage=""
acquire_lease 0
remote_args="$(printf ' %q' "$release" "$unit" "$sd_scope")"
# shellcheck disable=SC2029  # client-side expansion is the point: the arguments are
# %q-quoted (or a literal command) composed here and executed on the host.
ssh "$host" "bash -s --$remote_args" <<'REMOTE'
set -euo pipefail
release="$1" unit="$2" sd_scope="$3"
root="$HOME/tapo-monitor"
# Per-host lock around the switch: no interleaving with a deploy or another rollback.
if [[ -d "$root" ]]; then
    exec 8>"$root/.release.lock"
    if command -v flock >/dev/null 2>&1; then
        flock -n 8 || { echo "rollback_release: another deploy or rollback holds $root/.release.lock on the host" >&2; exit 1; }
    else
        echo "rollback_release: WARNING: no flock on the host — concurrent runs are not prevented" >&2
    fi
fi
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
# $sd_scope is "" or "--user"; unquoted so the empty case adds no argument.
# shellcheck disable=SC2086
env_file="$(systemctl $sd_scope show -p EnvironmentFiles --value "$unit" 2>/dev/null \
    | tr ' ' '\n' | sed 's/^-//' | grep -m1 '^/' || true)"
# A failure must not abort: `current` is already switched and the restart has to follow.
if [[ -n "$env_file" ]] && grep -q '^TAPO_EXPECTED_FINGERPRINT=' "$env_file"; then
    if [[ ! -w "$env_file" ]]; then
        echo "rollback_release: WARNING: $env_file is not writable — TAPO_EXPECTED_FINGERPRINT NOT updated; set it to ${release##*-} by hand" >&2
    elif sed -i "s/^TAPO_EXPECTED_FINGERPRINT=.*/TAPO_EXPECTED_FINGERPRINT=${release##*-}/" "$env_file"; then
        echo "rollback_release: TAPO_EXPECTED_FINGERPRINT -> ${release##*-}"
    else
        echo "rollback_release: WARNING: could not rewrite $env_file (sed failed, directory not writable?) — TAPO_EXPECTED_FINGERPRINT NOT updated; set it to ${release##*-} by hand" >&2
    fi
fi
REMOTE

echo "rollback_release: restarting via: $restart_cmd"
# shellcheck disable=SC2029  # client-side expansion is the point: the arguments are
# %q-quoted (or a literal command) composed here and executed on the host.
restart_ok=1
ssh "$host" "$restart_cmd" || {
    echo "rollback_release: RESTART FAILED — '$restart_cmd' exited non-zero on $host; current already points at releases/$release" >&2
    restart_ok=0
}
# Wait for the unit to come up on the release we just pointed at (skipped for a no-op
# restart command, as in deploy_release.sh).
if [[ "$restart_cmd" != "true" ]]; then
    systemctl_cmd="systemctl${sd_scope:+ $sd_scope}"
    state=""
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        # shellcheck disable=SC2029
        state="$(ssh "$host" "$systemctl_cmd is-active $unit" || true)"
        [[ "$state" == "active" ]] && break
        sleep 1
    done
    if [[ "$state" == "active" ]]; then
        echo "rollback_release: $unit is active"
    else
        echo "rollback_release: !!! $unit on $host is '$state' after the rollback — needs attention NOW !!!" >&2
        restart_ok=0
    fi
fi

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

((restart_ok)) || die "rollback re-pointed current to releases/$release but $unit is NOT confirmed running"
echo "rollback_release: done — follow up on the host with:"
echo "  ~/tapo-monitor/current/tools/check_monitor_rollout.sh${sd_scope:+ $sd_scope} ${release#*-}"
