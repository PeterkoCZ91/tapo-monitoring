#!/usr/bin/env bats
#
# tools/provision_scorer.sh — the refusals that happen before it can install anything.
#
# This script writes units into /etc and restarts the one service every camera depends on,
# so the only parts a test may exercise are the ones that end in an error message. Every
# test here therefore asserts a non-zero exit, and the whole file skips when it is run as
# root: as root the argument parsing would fall through into the real work.

load helper

setup() {
    if ((EUID == 0)); then
        skip "runs as root would provision this machine for real"
    fi
    setup_tools_test
    SCRIPT="$REPO_ROOT/tools/provision_scorer.sh"
}

teardown() {
    teardown_tools_test
}

@test "without root it stops before touching /etc" {
    run "$SCRIPT" --user scorer

    assert_status 1
    assert_output_contains "run me as root"
    assert_output_not_contains "installed:"
}

@test "--dry-run is still not a licence to run unprivileged" {
    # Root is checked before the dry run is honoured, because the checks the script makes
    # (reading the unit, the model, the venv) already need it.
    run "$SCRIPT" --user scorer --dry-run

    assert_status 1
    assert_output_contains "run me as root"
}

@test "an unknown argument is refused by name" {
    run "$SCRIPT" --unknown-flag

    assert_status 1
    assert_output_contains "unknown argument --unknown-flag"
}

@test "a stray positional argument is refused" {
    # There is no positional form; a bare word here is a flag that lost its dashes.
    run "$SCRIPT" scorer

    assert_status 1
    assert_output_contains "unknown argument scorer"
}

@test "an option without its value is an error, not a silent default" {
    local flag
    for flag in --user --home --python --model --port --input-size --metrics-file --unit; do
        run "$SCRIPT" "$flag"

        assert_status 1
        assert_output_contains "$flag needs a value"
    done
}

@test "--help prints the header block and nothing below it" {
    run "$SCRIPT" --help

    assert_status 0
    assert_output_contains "sudo tools/provision_scorer.sh --user"
    assert_output_contains "--dry-run shows the diffs and touches nothing"
    # The help is a range of comment lines; the fixed line number it used to end on had
    # already slipped past the last of them and printed a line of the script itself.
    assert_output_not_contains "set -euo pipefail"
}

@test "--help works before the root check, so an operator can read it" {
    run "$SCRIPT" --help

    assert_status 0
    assert_output_not_contains "run me as root"
}
