#!/usr/bin/env bats
#
# tools/rollback_release.sh — argument handling before it touches a host.
#
# This is the script an operator reaches for while a unit is crash-looping, which is the
# worst moment to discover that a mistyped flag was accepted. Everything asserted here
# happens before the first ssh; the guard stub makes sure of it.

load helper

setup() {
    setup_tools_test
    SCRIPT="$REPO_ROOT/tools/rollback_release.sh"
    HOST="rollback-target.invalid"
    RELEASE="20260101T000000Z-abc123def456"
}

teardown() {
    teardown_tools_test
}

@test "no arguments prints the usage" {
    run "$SCRIPT"

    assert_status 1
    assert_output_contains "usage: rollback_release.sh <ssh-host> [release-name] [options]"
    assert_output_not_contains "TEST GUARD"
}

@test "an unknown option is refused by name" {
    run "$SCRIPT" "$HOST" "$RELEASE" --unknown-flag

    assert_status 1
    assert_output_contains "unknown option --unknown-flag"
    assert_output_not_contains "TEST GUARD"
}

@test "an option without its value is an error, not a silent default" {
    local flag
    for flag in --unit --python --restart-cmd; do
        run "$SCRIPT" "$HOST" "$RELEASE" "$flag"

        assert_status 1
        assert_output_contains "$flag needs a value"
    done
}

@test "--env-file belongs to the deploy, not to the rollback" {
    # Nothing is copied here, so there is no env file to snapshot; accepting the flag
    # would promise a behaviour this script does not have.
    run "$SCRIPT" "$HOST" "$RELEASE" --env-file /dev/null

    assert_status 1
    assert_output_contains "unknown option --env-file"
}

@test "a third positional argument is refused" {
    run "$SCRIPT" "$HOST" "$RELEASE" extra-argument

    assert_status 1
    assert_output_contains "unexpected argument extra-argument"
}

@test "a host alone lists the releases rather than switching anything" {
    stub_command ssh <<'STUB'
cat >/dev/null
echo "ssh called: $*" >>"$TEST_TMP/ssh.log"
STUB

    run "$SCRIPT" "$HOST" </dev/null

    assert_status 0
    # One ssh, and it is the listing: no restart command was composed, nothing switched.
    [[ "$(wc -l <"$TEST_TMP/ssh.log")" -eq 1 ]] || {
        printf 'expected exactly one ssh call\n--- log ---\n%s\n' "$(cat "$TEST_TMP/ssh.log")"
        return 1
    }
    assert_output_not_contains "restarting via"
}

@test "the restart command defaults to sudo and is announced before it runs" {
    stub_command ssh <<'STUB'
cat >/dev/null
echo "ssh called: $*" >>"$TEST_TMP/ssh.log"
STUB

    run "$SCRIPT" "$HOST" "$RELEASE" --unit tapo-monitor-two.service </dev/null

    assert_output_contains "restarting via: sudo systemctl restart tapo-monitor-two.service"
    # The follow-up line hands the operator the fingerprint out of the release name, so a
    # rollback ends where a deploy does: at the post-deploy check.
    assert_output_contains "check_monitor_rollout.sh abc123def456"
}
