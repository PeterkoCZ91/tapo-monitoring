#!/usr/bin/env bats
#
# tools/deploy_release.sh — argument handling and everything it does before the first ssh.
#
# A deploy script gets one chance to refuse: once it reaches the network it is changing a
# host. So the interesting cases are all on this side of that line — a typo in a flag, a
# flag whose value went missing, an argument the script would otherwise have quietly
# reinterpreted. ssh is a guard stub here, and any test whose exit status is 99 means the
# script got further than it should have.

load helper

setup() {
    setup_tools_test
    SCRIPT="$REPO_ROOT/tools/deploy_release.sh"
    HOST="deploy-target.invalid"   # never resolvable, in case a stub is ever missed
}

teardown() {
    teardown_tools_test
}

# Stands in for the workstation's python3 while it fingerprints the staged tree.
stub_staged_fingerprint() {
    stub_command python3 <<STUB
echo "package $1"
STUB
}

# ── refusals ──────────────────────────────────────────────────────────────────────────

@test "no arguments prints the usage and touches nothing" {
    run "$SCRIPT"

    assert_status 1
    assert_output_contains "usage: deploy_release.sh <ssh-host> [git-ref] [options]"
}

@test "an unknown option is refused by name" {
    run "$SCRIPT" "$HOST" --unknown-flag

    assert_status 1
    assert_output_contains "unknown option --unknown-flag"
}

@test "a misspelled option is refused rather than taken for a host" {
    run "$SCRIPT" --untis tapo-monitor.service

    assert_status 1
    assert_output_contains "unknown option --untis"
}

@test "an option without its value is an error, not a silent default" {
    local flag
    for flag in --unit --python --restart-cmd --env-file; do
        run "$SCRIPT" "$HOST" "$flag"

        assert_status 1
        assert_output_contains "$flag needs a value"
    done
}

@test "a third positional argument is refused" {
    # host, ref, and then what? Anything the script accepted here would be deploying
    # something other than what the command line reads.
    run "$SCRIPT" "$HOST" v0.0.1 extra-argument

    assert_status 1
    assert_output_contains "unexpected argument extra-argument"
}

@test "an explicit HEAD does not reopen the ref slot" {
    # Regression: the ref was treated as unset while it still equalled its default, so
    # `deploy_release.sh <host> HEAD <ref>` silently shipped <ref> — a deploy of something
    # the operator did not name, from a command line that says HEAD.
    run "$SCRIPT" "$HOST" HEAD v0.0.1

    assert_status 1
    assert_output_contains "unexpected argument v0.0.1"
}

@test "an uncommitted ref cannot be deployed" {
    stub_staged_fingerprint abc123def456

    run "$SCRIPT" "$HOST" no-such-ref-exists

    assert_status 1
    assert_output_contains "git archive no-such-ref-exists failed"
    assert_output_not_contains "TEST GUARD"
}

@test "a staged tree that will not fingerprint is not shipped" {
    # The fingerprint is what the post-deploy check compares against; without one there
    # is nothing to verify the host against, so the deploy stops here.
    stub_staged_fingerprint "not-a-fingerprint"

    run "$SCRIPT" "$HOST" HEAD

    assert_status 1
    assert_output_contains "could not fingerprint the staged tree"
    assert_output_not_contains "TEST GUARD"
}

# ── the last thing it does before the network ─────────────────────────────────────────

@test "the release name is the UTC timestamp and the staged fingerprint" {
    stub_staged_fingerprint abc123def456

    run "$SCRIPT" "$HOST" HEAD

    # It reached the guarded ssh, which is exactly as far as a test may let it go.
    assert_status 99
    assert_output_matches 'releases/[0-9]{8}T[0-9]{6}Z-abc123def456 to deploy-target\.invalid'
}

@test "the default unit and restart command are reported before use" {
    stub_staged_fingerprint abc123def456
    stub_command ssh <<'STUB'
cat >/dev/null   # the transfer pipes a tar into ssh; an unread pipe is SIGPIPE upstream
echo "ssh called: $*" >>"$TEST_TMP/ssh.log"
exit 0
STUB

    # </dev/null: the stub reads its stdin, and the restart call inherits the test's.
    run "$SCRIPT" "$HOST" HEAD --restart-cmd "true" </dev/null

    # Every remote step is a no-op here, so what this asserts is the restart command the
    # script composed and announced, not the state of any host.
    assert_output_contains "restarting via: true"
    assert_file_contains "$TEST_TMP/ssh.log" "deploy-target.invalid"
}

@test "--restart-cmd overrides the sudo default" {
    stub_staged_fingerprint abc123def456
    stub_command ssh <<'STUB'
cat >/dev/null   # the transfer pipes a tar into ssh; an unread pipe is SIGPIPE upstream
echo "ssh called: $*" >>"$TEST_TMP/ssh.log"
exit 0
STUB

    run "$SCRIPT" "$HOST" HEAD --restart-cmd "systemctl --user restart tapo-monitor" </dev/null

    assert_output_contains "restarting via: systemctl --user restart tapo-monitor"
    assert_output_not_contains "restarting via: sudo"
}
