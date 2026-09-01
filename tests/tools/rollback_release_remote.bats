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
