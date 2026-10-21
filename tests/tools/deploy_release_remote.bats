#!/usr/bin/env bats
#
# tools/deploy_release.sh — the half that runs on the host.
#
# The interesting decisions of a deploy all happen on the far side of ssh: snapshot the
# config, prove the release before it can take over, switch the symlink atomically, prune
# what is no longer needed without deleting what a unit is running. None of that is
# reachable from the workstation half of the script, and it is precisely the part where a
# mistake takes a host down.
#
# So ssh is stubbed with one that drops the host argument and runs the command here, with
# $HOME pointing at a throwaway directory. The remote blocks are plain bash reading their
# arguments and stdin, so they run unchanged; what they build is a real ~/tapo-monitor
# under the test's own temporary home, which the assertions then inspect.

load helper

setup() {
    setup_tools_test
    SCRIPT="$REPO_ROOT/tools/deploy_release.sh"
    HOST="deploy-target.invalid"
    FINGERPRINT="abc123def456"
    ROOT="$HOME/tapo-monitor"
    mkdir -p "$ROOT"
    printf 'cameras: []\n' >"$ROOT/cameras.yaml"
    printf 'TAPO_TEST_CREDENTIAL=from-env-file\n' >"$TEST_TMP/monitor.env"

    # The real interpreter, for the stub below to hand the lock parsing to.
    REAL_PYTHON3="$(command -v python3)"
    export REAL_PYTHON3
    stub_command ssh <<'STUB'
shift              # the host: everything below runs on this machine instead
exec bash -c "$*"  # stdin — the tar stream or the remote heredoc — passes through
STUB
    # The workstation's interpreter, fingerprinting the staged tree. `-I -` is the
    # uv.lock reader, which runs for real.
    stub_command python3 <<'STUB'
[[ "${1:-}" == -I ]] && exec "$REAL_PYTHON3" "$@"
echo "package abc123def456"
STUB
    # The host's venv interpreter. Fails the selfcheck when the test asks it to; reports
    # installed versions from $TEST_TMP/installed ("name version" lines) and logs pip.
    stub_command venv-python <<'STUB'
if [[ "${1:-}" == -I && "${2:-}" == -c ]]; then
    shift 3
    for name; do
        version="$(awk -v n="$name" '$1 == n {print $2}' "$TEST_TMP/installed" 2>/dev/null)"
        echo "$name ${version:--}"
    done
    exit 0
fi
if [[ "${1:-}" == -m && "${2:-}" == pip ]]; then
    [[ "${3:-}" == --version ]] && exit 0
    [[ "${3:-}" == freeze ]] && { echo "pytapo==3.4.18"; exit 0; }
    echo "pip $*" >>"$TEST_TMP/order"
    [[ -f "$TEST_TMP/pip-fails" ]] && exit 1
    exit 0
fi
case "${3:-}" in
    version)
        echo "package ${STUB_HOST_FINGERPRINT:-abc123def456}"
        ;;
    selfcheck)
        printf '%s\n%s\n' "${4:-}" "${TAPO_TEST_CREDENTIAL:-<unset>}" \
            >"$TEST_TMP/selfcheck-context"
        printf '%s\n' "$PWD" >>"$TEST_TMP/selfcheck-cwd"
        echo selfcheck >>"$TEST_TMP/order"
        if [[ -f "$TEST_TMP/selfcheck-fails" ]]; then
            echo "selfcheck: config rejected"
            exit 1
        fi
        echo "selfcheck: 0 camera(s) ok"
        ;;
esac
STUB
}

teardown() {
    teardown_tools_test
}

deploy() {
    run "$SCRIPT" "$HOST" HEAD \
        --python "$STUB_BIN/venv-python" \
        --restart-cmd "true" \
        "$@" </dev/null
}

current_release() {
    basename "$(readlink "$ROOT/current")"
}

@test "a deploy lands in its own directory and only then becomes current" {
    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 0
    assert_output_contains "verified — current runs package $FINGERPRINT"
    [[ -L "$ROOT/current" ]] || {
        echo "current is not a symlink"
        return 1
    }
    [[ "$(current_release)" == *"-$FINGERPRINT" ]] || {
        printf 'current points at %s\n' "$(current_release)"
        return 1
    }
    # The package itself came over, not just the directory.
    [[ -f "$ROOT/current/tapo_monitor/cli.py" ]] || {
        echo "the release does not contain the package"
        return 1
    }
}

@test "the release keeps the config and env file it was validated against" {
    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 0
    local snapshot="$ROOT/current/config-snapshot"
    assert_file_contains "$snapshot/cameras.yaml" "cameras: []"
    # cameras.yaml is shared by every release, so without this copy "what config did the
    # release I am rolling back to pass with" has no answer.
    assert_file_contains "$snapshot/monitor.env" "TAPO_TEST_CREDENTIAL"
    # The env file holds credentials.
    [[ "$(stat -c '%a' "$snapshot/monitor.env")" == "600" ]] || {
        printf 'snapshot mode is %s, expected 600\n' "$(stat -c '%a' "$snapshot/monitor.env")"
        return 1
    }
}

@test "the selfcheck runs inside the release and under the unit's environment" {
    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 0
    # With -m, the cwd package wins over any installed copy: the selfcheck must therefore
    # run from inside the release, or it proves something about a different tree.
    assert_file_contains "$TEST_TMP/selfcheck-cwd" "$ROOT/releases/"
    assert_file_contains "$TEST_TMP/selfcheck-context" "from-env-file"
    assert_file_contains "$TEST_TMP/selfcheck-context" "$ROOT/cameras.yaml"
}

@test "the selfcheck reads the env file like systemd: no expansion, nothing executed" {
    printf '%s\n' 'TAPO_TEST_CREDENTIAL=pa$ss(w)`touch '"$TEST_TMP"'/pwned`' >"$TEST_TMP/monitor.env"

    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 0
    assert_file_contains "$TEST_TMP/selfcheck-context" 'pa$ss(w)`touch'
    [[ ! -e "$TEST_TMP/pwned" ]]
}

@test "a release that fails its selfcheck never becomes current" {
    touch "$TEST_TMP/selfcheck-fails"

    deploy --env-file "$TEST_TMP/monitor.env"

    [[ "$status" -ne 0 ]] || {
        printf 'expected a non-zero exit\n--- output ---\n%s\n' "$output"
        return 1
    }
    assert_output_contains "selfcheck FAILED"
    assert_output_contains "'current' was not switched"
    assert_file_absent "$ROOT/current"
    # The directory stays for inspection rather than being cleaned up behind the operator.
    [[ -n "$(find "$ROOT/releases" -mindepth 1 -maxdepth 1 -type d)" ]] || {
        echo "the failed release directory was removed"
        return 1
    }
}

@test "a second deploy switches current and keeps the first release" {
    deploy --env-file "$TEST_TMP/monitor.env"
    assert_status 0
    local first
    first="$(current_release)"

    # A different tree, so the release name differs by more than its timestamp.
    stub_command python3 <<'STUB'
echo "package feed0000face"
STUB
    export STUB_HOST_FINGERPRINT="feed0000face"
    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 0
    [[ "$(current_release)" == *"-feed0000face" ]] || {
        printf 'current points at %s\n' "$(current_release)"
        return 1
    }
    # Rollback is re-pointing the symlink, which only works while the release it points
    # back to is still on the host.
    [[ -d "$ROOT/releases/$first" ]] || {
        echo "the previous release was not kept — there would be nothing to roll back to"
        return 1
    }
}

@test "a host still on the rsync layout is refused, not overwritten" {
    mkdir -p "$ROOT/current/tapo_monitor"
    printf 'old\n' >"$ROOT/current/tapo_monitor/cli.py"

    deploy --env-file "$TEST_TMP/monitor.env"

    [[ "$status" -ne 0 ]] || {
        printf 'expected a non-zero exit\n--- output ---\n%s\n' "$output"
        return 1
    }
    assert_output_contains "is not a symlink"
    assert_output_contains "migrate first"
    assert_file_contains "$ROOT/current/tapo_monitor/cli.py" "old"
}

@test "a host without the shared config is refused" {
    rm "$ROOT/cameras.yaml"

    deploy --env-file "$TEST_TMP/monitor.env"

    [[ "$status" -ne 0 ]] || {
        printf 'expected a non-zero exit\n--- output ---\n%s\n' "$output"
        return 1
    }
    assert_output_contains "the release layout expects the host-owned config there"
    assert_file_absent "$ROOT/current"
}

@test "an unreadable env file stops the deploy before the switch" {
    deploy --env-file "$TEST_TMP/does-not-exist.env"

    [[ "$status" -ne 0 ]] || {
        printf 'expected a non-zero exit\n--- output ---\n%s\n' "$output"
        return 1
    }
    assert_output_contains "is not readable on the host"
    assert_file_absent "$ROOT/current"
}

@test "the env file is discovered from the unit when none is named" {
    stub_command systemctl <<'STUB'
case "$*" in
    *EnvironmentFiles*) echo "-$TEST_TMP/monitor.env" ;;
esac
STUB

    deploy

    assert_status 0
    # The leading '-' of an optional EnvironmentFile= is part of systemd's syntax, not of
    # the path; a snapshot named "-monitor.env" would mean it was never stripped.
    assert_file_contains "$ROOT/current/config-snapshot/monitor.env" "TAPO_TEST_CREDENTIAL"
}

@test "a unit that names no env file still deploys, with a note" {
    stub_command systemctl <<'STUB'
echo ""
STUB

    deploy

    assert_status 0
    assert_output_contains "names no EnvironmentFile"
    [[ ! -e "$ROOT/current/config-snapshot/monitor.env" ]] || {
        echo "an env file was snapshotted that the unit does not have"
        return 1
    }
}

@test "the drift expectation follows the deploy, but only where it was opted into" {
    printf 'TAPO_EXPECTED_FINGERPRINT=000000000000\n' >>"$TEST_TMP/monitor.env"

    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 0
    assert_output_contains "TAPO_EXPECTED_FINGERPRINT -> $FINGERPRINT"
    assert_file_contains "$TEST_TMP/monitor.env" "TAPO_EXPECTED_FINGERPRINT=$FINGERPRINT"
}

@test "a host that never opted into the drift check stays unenrolled" {
    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 0
    assert_output_not_contains "TAPO_EXPECTED_FINGERPRINT ->"
    if grep -q TAPO_EXPECTED_FINGERPRINT "$TEST_TMP/monitor.env"; then
        echo "the deploy added a setting the host had not asked for"
        return 1
    fi
}

@test "a host reporting a different package than was shipped fails the deploy" {
    export STUB_HOST_FINGERPRINT="0000deadbeef"

    deploy --env-file "$TEST_TMP/monitor.env"

    [[ "$status" -ne 0 ]] || {
        printf 'expected a non-zero exit\n--- output ---\n%s\n' "$output"
        return 1
    }
    assert_output_contains "VERIFY FAILED"
    assert_output_contains "current reports 0000deadbeef"
}

@test "old releases are pruned and the newest are kept" {
    local old
    for old in 20250101T000000Z-000000000001 20250102T000000Z-000000000002 \
               20250103T000000Z-000000000003 20250104T000000Z-000000000004 \
               20250105T000000Z-000000000005; do
        mkdir -p "$ROOT/releases/$old"
    done

    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 0
    assert_output_contains "pruned old release 20250101T000000Z-000000000001"
    assert_file_absent "$ROOT/releases/20250101T000000Z-000000000001"
    # Five kept, newest first, and the deploy's own release is one of them.
    [[ "$(find "$ROOT/releases" -mindepth 1 -maxdepth 1 -type d | wc -l)" -eq 5 ]] || {
        printf 'kept %s releases\n' "$(find "$ROOT/releases" -mindepth 1 -maxdepth 1 -type d | wc -l)"
        return 1
    }
    [[ -d "$ROOT/current" ]] || {
        echo "current points at a directory that was pruned"
        return 1
    }
}

# ── the health check after the restart, on a host that runs a user unit ───────────────

# The unit exists only in the user manager. The system manager answers the way a real one
# does for a unit it does not have: inactive, NRestarts=0. Every call is logged.
stub_user_unit_host() {
    stub_command systemctl <<'STUB'
echo "$*" >>"$TEST_TMP/systemctl.log"
if [[ " $* " != *" --user "* ]]; then
    case "$*" in
        *is-active*) echo inactive; exit 3 ;;
        *NRestarts*) echo 0 ;;
        *restart*)   echo "no such system unit" >&2; exit 5 ;;
    esac
    exit 0
fi
case "$*" in
    *is-active*)        echo active ;;
    *NRestarts*)        echo 0 ;;
    *EnvironmentFiles*) echo "-$TEST_TMP/monitor.env" ;;
    *restart*)          echo restarted >>"$TEST_TMP/user-restarts" ;;
esac
STUB
    stub_command sleep <<'STUB'
exit 0
STUB
}

# A first release that is live, so a failed health check has something to roll back to.
seed_previous_release() {
    deploy --env-file "$TEST_TMP/monitor.env"
    assert_status 0
    PREVIOUS="$(current_release)"
    stub_command python3 <<'STUB'
echo "package feed0000face"
STUB
    export STUB_HOST_FINGERPRINT="feed0000face"
}

@test "--user: the health check asks the user manager and keeps a running release" {
    seed_previous_release
    stub_user_unit_host

    run "$SCRIPT" "$HOST" HEAD --python "$STUB_BIN/venv-python" --user --health-wait 1 </dev/null

    assert_status 0
    assert_output_contains "restarting via: systemctl --user restart tapo-monitor.service"
    assert_output_contains "stayed active"
    assert_output_not_contains "HEALTH CHECK FAILED"
    [[ "$(current_release)" == *"-feed0000face" ]] || {
        printf 'current points at %s — the healthy release was rolled back\n' "$(current_release)"
        return 1
    }
    assert_file_contains "$TEST_TMP/user-restarts" "restarted"
    # The env file came from the user unit too.
    assert_file_contains "$ROOT/current/config-snapshot/monitor.env" "TAPO_TEST_CREDENTIAL"
    if grep -v -- '--user' "$TEST_TMP/systemctl.log"; then
        echo "a call above went to the system manager"
        return 1
    fi
}

@test "without --user the health check still asks the system manager" {
    seed_previous_release
    stub_user_unit_host

    # The pre-flag way of deploying to a user-unit host: only the restart knew about it.
    run "$SCRIPT" "$HOST" HEAD --python "$STUB_BIN/venv-python" --env-file "$TEST_TMP/monitor.env" \
        --restart-cmd "systemctl --user restart tapo-monitor.service" --health-wait 1 </dev/null

    [[ "$status" -ne 0 ]] || {
        printf 'expected a non-zero exit\n--- output ---\n%s\n' "$output"
        return 1
    }
    assert_output_contains "HEALTH CHECK FAILED"
    assert_output_contains "rolling back to releases/$PREVIOUS"
    grep -qx 'is-active tapo-monitor.service' "$TEST_TMP/systemctl.log" || {
        echo "the default health check did not query the system manager"
        return 1
    }
}

# ── dependency drift between the host venv and the workstation's uv.lock ──────────────

# A lock that has moved past what the host venv was installed with: pytapo is newer, the
# rest matches. The staged pyproject names these four as [project].dependencies.
seed_lock_and_venv() {
    cat >"$TEST_TMP/uv.lock" <<'LOCK'
version = 1

[[package]]
name = "aiofiles"
version = "25.1.0"

[[package]]
name = "astral"
version = "3.2"

[[package]]
name = "pytapo"
version = "3.4.28"

[[package]]
name = "pyyaml"
version = "6.0.3"
LOCK
    printf '%s\n' "aiofiles 25.1.0" "astral 3.2" "pytapo 3.4.18" "pyyaml 6.0.3" \
        >"$TEST_TMP/installed"
}

@test "dependency drift is reported but does not stop the deploy" {
    seed_lock_and_venv

    deploy --env-file "$TEST_TMP/monitor.env" --lock "$TEST_TMP/uv.lock"

    assert_status 0
    assert_output_contains "host venv differs from the workstation's uv.lock"
    assert_output_matches 'pytapo +3\.4\.18 +3\.4\.28'
    assert_output_not_contains "pyyaml           6.0.3"
    assert_output_contains "rerun with --sync-deps"
    assert_output_contains "verified — current runs package $FINGERPRINT"
    if grep -q '^pip' "$TEST_TMP/order"; then
        echo "pip ran without --sync-deps"
        return 1
    fi
}

@test "a venv that matches the lock says so" {
    seed_lock_and_venv
    sed -i 's/^pytapo .*/pytapo 3.4.28/' "$TEST_TMP/installed"

    deploy --env-file "$TEST_TMP/monitor.env" --lock "$TEST_TMP/uv.lock"

    assert_status 0
    assert_output_contains "dependencies match the lock (4 checked)"
    assert_output_not_contains "WARNING"
}

@test "without a lock the dependency check is skipped with a note" {
    deploy --env-file "$TEST_TMP/monitor.env" --lock "$TEST_TMP/no-such.lock"

    assert_status 0
    assert_output_contains "dependency versions on the host are not checked"
}

@test "--sync-deps without a lock refuses before anything is shipped" {
    deploy --env-file "$TEST_TMP/monitor.env" --lock "$TEST_TMP/no-such.lock" --sync-deps

    assert_status 1
    assert_output_contains "--sync-deps needs expected versions"
    assert_file_absent "$ROOT/releases"
}

@test "--sync-deps installs the drifted pins before the selfcheck and prints the way back" {
    seed_lock_and_venv
    sed -i '/^astral /d' "$TEST_TMP/installed"

    deploy --env-file "$TEST_TMP/monitor.env" --lock "$TEST_TMP/uv.lock" --sync-deps

    assert_status 0
    # Only what drifted, exactly pinned — and before the selfcheck that has to prove it.
    [[ "$(head -n1 "$TEST_TMP/order")" == "pip -m pip install"*" pytapo==3.4.28 astral==3.2" ]] || {
        printf 'order of host calls:\n%s\n' "$(cat "$TEST_TMP/order")"
        return 1
    }
    [[ "$(sed -n 2p "$TEST_TMP/order")" == selfcheck ]] || {
        printf 'order of host calls:\n%s\n' "$(cat "$TEST_TMP/order")"
        return 1
    }
    # No rollback undoes the install, so the previous state is spelled out as commands.
    assert_output_contains "no rollback undoes this"
    assert_output_contains "-m pip install 'pytapo==3.4.18'"
    assert_output_contains "-m pip uninstall -y astral"
    assert_file_contains "$ROOT/current/deps-before.txt" "'pytapo==3.4.18'"
}

@test "--sync-deps whose pip install fails never switches current" {
    seed_lock_and_venv
    touch "$TEST_TMP/pip-fails"

    deploy --env-file "$TEST_TMP/monitor.env" --lock "$TEST_TMP/uv.lock" --sync-deps

    [[ "$status" -ne 0 ]] || {
        printf 'expected a non-zero exit\n--- output ---\n%s\n' "$output"
        return 1
    }
    assert_output_contains "pip install FAILED"
    assert_file_absent "$ROOT/current"
    if grep -q '^selfcheck' "$TEST_TMP/order"; then
        echo "the selfcheck ran after a failed install"
        return 1
    fi
}

@test "a health-check rollback after --sync-deps names the venv it left behind" {
    seed_previous_release
    stub_user_unit_host
    seed_lock_and_venv
    stub_command python3 <<'STUB'
[[ "${1:-}" == -I ]] && exec "$REAL_PYTHON3" "$@"
echo "package feed0000face"
STUB

    # The system manager does not have the unit, so the health check fails and rolls back.
    run "$SCRIPT" "$HOST" HEAD --python "$STUB_BIN/venv-python" --env-file "$TEST_TMP/monitor.env" \
        --restart-cmd "systemctl --user restart tapo-monitor.service" --health-wait 1 \
        --lock "$TEST_TMP/uv.lock" --sync-deps </dev/null

    [[ "$status" -ne 0 ]] || {
        printf 'expected a non-zero exit\n--- output ---\n%s\n' "$output"
        return 1
    }
    assert_output_contains "rolling back to releases/$PREVIOUS"
    assert_output_contains "the venv still has the versions --sync-deps installed"
    assert_output_contains "'pytapo==3.4.18'"
}

# ── restart failures, env file trouble, locks, permissions ────────────────────────────

stub_active_host() {
    stub_command systemctl <<'STUB'
case "$*" in
    *is-active*) echo active ;;
    *NRestarts*) echo 0 ;;
esac
STUB
    stub_command sleep <<'STUB'
exit 0
STUB
}

@test "a failing restart command is a health failure: rolled back, fingerprint restored" {
    printf 'TAPO_EXPECTED_FINGERPRINT=000000000000\n' >>"$TEST_TMP/monitor.env"
    seed_previous_release
    stub_active_host

    run "$SCRIPT" "$HOST" HEAD --python "$STUB_BIN/venv-python" --env-file "$TEST_TMP/monitor.env" \
        --restart-cmd "false" --health-wait 0 </dev/null

    [[ "$status" -ne 0 ]] || {
        printf 'expected a non-zero exit\n--- output ---\n%s\n' "$output"
        return 1
    }
    assert_output_contains "RESTART FAILED"
    assert_output_contains "rolling back to releases/$PREVIOUS"
    [[ "$(current_release)" == "$PREVIOUS" ]] || {
        printf 'current points at %s\n' "$(current_release)"
        return 1
    }
    # The deploy rewrote it to the new fingerprint; the rollback puts back what it was.
    assert_output_contains "TAPO_EXPECTED_FINGERPRINT restored"
    assert_file_contains "$TEST_TMP/monitor.env" "TAPO_EXPECTED_FINGERPRINT=$FINGERPRINT"
}

@test "a rollback that does not bring the unit back says so loudly" {
    seed_previous_release
    stub_command systemctl <<'STUB'
case "$*" in
    *is-active*) echo failed; exit 3 ;;
    *NRestarts*) echo 0 ;;
esac
STUB
    stub_command sleep <<'STUB'
exit 0
STUB

    run "$SCRIPT" "$HOST" HEAD --python "$STUB_BIN/venv-python" --env-file "$TEST_TMP/monitor.env" \
        --restart-cmd "false" --health-wait 0 </dev/null

    [[ "$status" -ne 0 ]] || return 1
    assert_output_contains "ROLLBACK RESTART FAILED"
    assert_output_contains "ROLLBACK DID NOT RECOVER"
    assert_output_contains "NOT confirmed running"
}

@test "an env file that cannot be rewritten warns and the deploy carries on" {
    printf 'TAPO_EXPECTED_FINGERPRINT=000000000000\n' >>"$TEST_TMP/monitor.env"
    chmod 444 "$TEST_TMP/monitor.env"

    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 0
    assert_output_contains "is not writable — TAPO_EXPECTED_FINGERPRINT NOT updated"
    assert_output_contains "verified — current runs package $FINGERPRINT"
}

@test "a writable env file in a directory sed cannot write to warns instead of aborting" {
    mkdir "$TEST_TMP/envdir"
    printf 'TAPO_EXPECTED_FINGERPRINT=000000000000\n' >"$TEST_TMP/envdir/monitor.env"
    chmod 664 "$TEST_TMP/envdir/monitor.env"
    chmod 555 "$TEST_TMP/envdir"

    deploy --env-file "$TEST_TMP/envdir/monitor.env"
    chmod 755 "$TEST_TMP/envdir"

    assert_status 0
    assert_output_contains "could not rewrite"
    assert_output_contains "verified — current runs package $FINGERPRINT"
}

@test "a second deploy to the same host is refused while one holds the lock" {
    mkdir -p "$HOME/.local/state/tapo-monitor"
    exec 9>"$HOME/.local/state/tapo-monitor/release-$HOST.lock"
    flock -n 9

    deploy --env-file "$TEST_TMP/monitor.env"
    exec 9>&-

    assert_status 1
    assert_output_contains "already running"
    assert_file_absent "$ROOT/current"
}

@test "the host-side lock refuses a switch while another one is in progress" {
    exec 8>"$ROOT/.release.lock"
    flock -n 8

    deploy --env-file "$TEST_TMP/monitor.env"
    exec 8>&-

    assert_status 1
    assert_output_contains "holds $ROOT/.release.lock"
    assert_file_absent "$ROOT/current"
}

@test "the config snapshot directory is private too" {
    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 0
    [[ "$(stat -c '%a' "$ROOT/current/config-snapshot")" == "700" ]] || {
        printf 'snapshot dir mode is %s\n' "$(stat -c '%a' "$ROOT/current/config-snapshot")"
        return 1
    }
}

@test "--sync-deps warns about the shared venv and keeps a pip freeze next to the release" {
    seed_lock_and_venv

    deploy --env-file "$TEST_TMP/monitor.env" --lock "$TEST_TMP/uv.lock" --sync-deps

    assert_status 0
    assert_output_contains "changes the SHARED venv now, before the selfcheck"
    assert_file_contains "$ROOT/current/pip-freeze-before.txt" "pytapo==3.4.18"
}

# ── host-side lease held for the whole sequence ───────────────────────────────────────

@test "a fresh lease from another workstation refuses the deploy and is left untouched" {
    mkdir -p "$ROOT/releases"
    printf 'other-ws 4242 %s\n' "$(date +%s)" >"$ROOT/releases/.deploy.lock"

    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 1
    assert_output_contains "holds the lease"
    assert_output_contains "owner other-ws pid 4242"
    assert_output_matches "[0-9]+s old"
    assert_file_absent "$ROOT/current"
    assert_file_contains "$ROOT/releases/.deploy.lock" "other-ws 4242"
}

@test "a stale lease is taken over with a warning and released afterwards" {
    mkdir -p "$ROOT/releases"
    printf 'old-ws 4242 %s\n' "$(( $(date +%s) - 3600 ))" >"$ROOT/releases/.deploy.lock"

    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 0
    assert_output_contains "WARNING: taking over a stale lease from old-ws pid 4242"
    assert_file_absent "$ROOT/releases/.deploy.lock"
    [[ "$(current_release)" == *"-$FINGERPRINT" ]]
}

@test "the lease is held while the restart runs and gone after a successful deploy" {
    deploy --env-file "$TEST_TMP/monitor.env" \
        --restart-cmd "cp $ROOT/releases/.deploy.lock $TEST_TMP/lease-during-restart"

    assert_status 0
    # <workstation-id> <pid> <epoch>
    [[ "$(cat "$TEST_TMP/lease-during-restart")" =~ ^[A-Za-z0-9_.-]+\ [0-9]+\ [0-9]+$ ]] || {
        cat "$TEST_TMP/lease-during-restart"
        return 1
    }
    assert_file_absent "$ROOT/releases/.deploy.lock"
}

@test "the lease is released after a failed health check and rollback" {
    seed_previous_release
    stub_active_host

    run "$SCRIPT" "$HOST" HEAD --python "$STUB_BIN/venv-python" --env-file "$TEST_TMP/monitor.env" \
        --restart-cmd "false" --health-wait 0 </dev/null

    assert_status 1
    assert_output_contains "rolling back to releases/$PREVIOUS"
    assert_file_absent "$ROOT/releases/.deploy.lock"
}

@test "the lease is released when the deploy dies before the switch" {
    touch "$TEST_TMP/selfcheck-fails"

    deploy --env-file "$TEST_TMP/monitor.env"

    assert_status 1
    assert_file_absent "$ROOT/releases/.deploy.lock"
}

@test "a lease that now belongs to someone else is not removed" {
    deploy --env-file "$TEST_TMP/monitor.env" \
        --restart-cmd "printf 'foreign-ws 7 %s\n' \$(date +%s) >$ROOT/releases/.deploy.lock"

    assert_status 0
    assert_output_contains "left alone"
    assert_file_contains "$ROOT/releases/.deploy.lock" "foreign-ws 7"
}
