#!/usr/bin/env bats
#
# tools/rollback_release.sh — the half that runs on the host.
#
# This is the recovery path, so it has to work while something else is already broken:
# list what the host has, re-point the symlink atomically, and say plainly when the
# release named does not exist rather than leaving a host with no `current` at all. Same
# arrangement as the deploy's remote tests — ssh runs the remote block here, against a
# throwaway $HOME that plays the host.

load helper

setup() {
    setup_tools_test
    SCRIPT="$REPO_ROOT/tools/rollback_release.sh"
    HOST="rollback-target.invalid"
    ROOT="$HOME/tapo-monitor"
    OLD="20260101T000000Z-abc123def456"
    NEW="20260102T000000Z-feed0000face"
    mkdir -p "$ROOT/releases/$OLD" "$ROOT/releases/$NEW"
    ln -s "releases/$NEW" "$ROOT/current"

    stub_command ssh <<'STUB'
shift
exec bash -c "$*"
STUB
    # Reports whichever package the release directory it is started from says it is.
    stub_command venv-python <<'STUB'
release="$(basename "$(readlink -f "$PWD")")"
echo "package ${STUB_HOST_FINGERPRINT:-${release##*-}}"
STUB
}

teardown() {
    teardown_tools_test
}

rollback() {
    run "$SCRIPT" "$HOST" "$@" --python "$STUB_BIN/venv-python" --restart-cmd "true" </dev/null
}

current_release() {
    basename "$(readlink "$ROOT/current")"
}

@test "the listing shows every release and marks the one in use" {
    rollback

    assert_status 0
    assert_output_contains "$OLD"
    assert_output_contains "$NEW"
    assert_output_matches "$NEW +feed0000face +<- current"
    assert_output_not_contains "restarting via"
}

@test "a host that was never deployed this way says so" {
    rm -rf "$ROOT/releases" "$ROOT/current"

    rollback

    assert_status 1
    assert_output_contains "nothing was ever deployed with deploy_release.sh"
}

@test "an empty releases directory is not an empty listing" {
    rm -rf "$ROOT/releases"/*
    rm "$ROOT/current"

    rollback

    assert_status 1
    assert_output_contains "is empty"
}

@test "rolling back re-points current and verifies what it now runs" {
    rollback "$OLD"

    assert_status 0
    [[ "$(current_release)" == "$OLD" ]] || {
        printf 'current points at %s\n' "$(current_release)"
        return 1
    }
    assert_output_contains "verified — current runs package abc123def456"
    # A rollback ends where a deploy does: at the post-deploy check, with the fingerprint
    # already filled in.
    assert_output_contains "check_monitor_rollout.sh abc123def456"
}

@test "current stays a symlink through the switch" {
    rollback "$OLD"

    assert_status 0
    [[ -L "$ROOT/current" ]] || {
        echo "current is no longer a symlink"
        return 1
    }
}

@test "a release the host does not have leaves current alone" {
    rollback "20250101T000000Z-000000000000"

    assert_status 1
    assert_output_contains "run without a release name to list them"
    [[ "$(current_release)" == "$NEW" ]] || {
        printf 'current was changed to %s\n' "$(current_release)"
        return 1
    }
}

@test "a host still on the rsync layout is refused" {
    rm "$ROOT/current"
    mkdir -p "$ROOT/current"

    rollback "$OLD"

    assert_status 1
    assert_output_contains "is not a symlink"
}

@test "the drift expectation follows the rollback" {
    # Without this the next digest would report the release the operator deliberately
    # returned to as drift.
    printf 'TAPO_EXPECTED_FINGERPRINT=feed0000face\n' >"$TEST_TMP/monitor.env"
    stub_command systemctl <<'STUB'
case "$*" in
    *EnvironmentFiles*) echo "-$TEST_TMP/monitor.env" ;;
esac
STUB

    rollback "$OLD"

    assert_status 0
    assert_output_contains "TAPO_EXPECTED_FINGERPRINT -> abc123def456"
    assert_file_contains "$TEST_TMP/monitor.env" "TAPO_EXPECTED_FINGERPRINT=abc123def456"
}

@test "a release running a package other than its name fails the verification" {
    export STUB_HOST_FINGERPRINT="0000deadbeef"

    rollback "$OLD"

    assert_status 1
    assert_output_contains "VERIFY FAILED"
    assert_output_contains "the release name says abc123def456"
}

@test "a release name carrying no fingerprint is reported, not failed" {
    mkdir -p "$ROOT/releases/handmade"

    rollback "handmade"

    assert_status 0
    assert_output_contains "carries no fingerprint to verify against"
    [[ "$(current_release)" == "handmade" ]] || {
        printf 'current points at %s\n' "$(current_release)"
        return 1
    }
}

@test "--user: the env file comes from the user unit and the restart from the user manager" {
    printf 'TAPO_EXPECTED_FINGERPRINT=feed0000face\n' >"$TEST_TMP/monitor.env"
    stub_command systemctl <<'STUB'
echo "$*" >>"$TEST_TMP/systemctl.log"
[[ " $* " == *" --user "* ]] || exit 0   # the system manager has no such unit
case "$*" in
    *EnvironmentFiles*) echo "-$TEST_TMP/monitor.env" ;;
    *is-active*)        echo active ;;
esac
STUB

    run "$SCRIPT" "$HOST" "$OLD" --python "$STUB_BIN/venv-python" --user </dev/null

    assert_status 0
    assert_output_contains "restarting via: systemctl --user restart tapo-monitor.service"
    assert_output_contains "TAPO_EXPECTED_FINGERPRINT -> abc123def456"
    assert_output_contains "check_monitor_rollout.sh --user abc123def456"
    grep -qx -- '--user restart tapo-monitor.service' "$TEST_TMP/systemctl.log" || {
        echo "the restart did not go to the user manager"
        return 1
    }
    if grep -v -- '--user' "$TEST_TMP/systemctl.log"; then
        echo "a call above went to the system manager"
        return 1
    fi
}

@test "a failing restart is reported loudly and fails the rollback" {
    stub_command systemctl <<'STUB'
case "$*" in
    *is-active*) echo failed; exit 3 ;;
esac
STUB
    stub_command sleep <<'STUB'
exit 0
STUB

    run "$SCRIPT" "$HOST" "$OLD" --python "$STUB_BIN/venv-python" --restart-cmd "false" </dev/null

    assert_status 1
    assert_output_contains "RESTART FAILED"
    assert_output_contains "needs attention NOW"
    assert_output_contains "NOT confirmed running"
    # The re-point itself happened and stays.
    [[ "$(current_release)" == "$OLD" ]]
}

@test "the rollback waits for the unit to be active" {
    stub_command systemctl <<'STUB'
case "$*" in
    *is-active*) echo active ;;
esac
STUB

    run "$SCRIPT" "$HOST" "$OLD" --python "$STUB_BIN/venv-python" --restart-cmd "echo restarting" </dev/null

    assert_status 0
    assert_output_contains "tapo-monitor.service is active"
}

@test "an env file that cannot be rewritten warns and the rollback carries on" {
    printf 'TAPO_EXPECTED_FINGERPRINT=feed0000face\n' >"$TEST_TMP/monitor.env"
    chmod 444 "$TEST_TMP/monitor.env"
    stub_command systemctl <<'STUB'
case "$*" in
    *EnvironmentFiles*) echo "-$TEST_TMP/monitor.env" ;;
esac
STUB

    rollback "$OLD"

    assert_status 0
    assert_output_contains "is not writable — TAPO_EXPECTED_FINGERPRINT NOT updated"
    assert_output_contains "verified — current runs package abc123def456"
}

@test "a rollback is refused while a deploy or another rollback holds the lock" {
    mkdir -p "$HOME/.local/state/tapo-monitor"
    exec 9>"$HOME/.local/state/tapo-monitor/release-$HOST.lock"
    flock -n 9

    rollback "$OLD"
    exec 9>&-

    assert_status 1
    assert_output_contains "already running"
    [[ "$(current_release)" == "$NEW" ]]
}

@test "the host-side lock refuses the switch while another one is in progress" {
    exec 8>"$ROOT/.release.lock"
    flock -n 8

    rollback "$OLD"
    exec 8>&-

    assert_status 1
    assert_output_contains "holds $ROOT/.release.lock"
    [[ "$(current_release)" == "$NEW" ]]
}

# ── host-side lease held for the whole sequence ───────────────────────────────────────

@test "a rollback is refused while a fresh lease is held and changes nothing" {
    printf 'other-ws 4242 %s\n' "$(date +%s)" >"$ROOT/releases/.deploy.lock"

    rollback "$OLD"

    assert_status 1
    assert_output_contains "holds the lease"
    assert_output_contains "owner other-ws pid 4242"
    [[ "$(current_release)" == "$NEW" ]]
    assert_file_contains "$ROOT/releases/.deploy.lock" "other-ws 4242"
}

@test "a stale lease is taken over by the rollback and released afterwards" {
    printf 'old-ws 4242 %s\n' "$(( $(date +%s) - 3600 ))" >"$ROOT/releases/.deploy.lock"

    rollback "$OLD"

    assert_status 0
    assert_output_contains "WARNING: taking over a stale lease"
    [[ "$(current_release)" == "$OLD" ]]
    assert_file_absent "$ROOT/releases/.deploy.lock"
}

@test "the lease is held during the restart and released after a successful rollback" {
    stub_command systemctl <<'STUB'
[[ "$*" == *is-active* ]] && echo active
exit 0
STUB
    run "$SCRIPT" "$HOST" "$OLD" --python "$STUB_BIN/venv-python" \
        --restart-cmd "cp $ROOT/releases/.deploy.lock $TEST_TMP/lease-during-restart" </dev/null

    assert_status 0
    [[ "$(cat "$TEST_TMP/lease-during-restart")" =~ ^[A-Za-z0-9_.-]+\ [0-9]+\ [0-9]+$ ]]
    assert_file_absent "$ROOT/releases/.deploy.lock"
}

@test "the lease is released when the rollback fails" {
    STUB_HOST_FINGERPRINT=000000000000 rollback "$OLD"

    assert_status 1
    assert_output_contains "VERIFY FAILED"
    assert_file_absent "$ROOT/releases/.deploy.lock"
}

@test "a rollback leaves a lease that changed hands alone" {
    stub_command systemctl <<'STUB'
[[ "$*" == *is-active* ]] && echo active
exit 0
STUB
    run "$SCRIPT" "$HOST" "$OLD" --python "$STUB_BIN/venv-python" \
        --restart-cmd "printf 'foreign-ws 7 %s\n' \$(date +%s) >$ROOT/releases/.deploy.lock" </dev/null

    assert_status 0
    assert_file_contains "$ROOT/releases/.deploy.lock" "foreign-ws 7"
}

@test "a rollback to a host without releases does not create the layout" {
    rm -rf "$ROOT/releases" "$ROOT/current"

    rollback "$OLD"

    assert_status 1
    assert_file_absent "$ROOT/releases"
}
