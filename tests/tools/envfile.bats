#!/usr/bin/env bats
#
# tools/lib/envfile.sh — the EnvironmentFile reader shared by the tools.

load helper

setup() {
    setup_tools_test
    # shellcheck source=/dev/null
    . "$REPO_ROOT/tools/lib/envfile.sh"
    F="$TEST_TMP/x.env"
}

teardown() {
    teardown_tools_test
}

@test "values are literal: no expansion, no command execution" {
    printf 'A=a$b\nB=p(ss\nC=`touch %s/pwned`\nD=$(touch %s/pwned)\n' "$TEST_TMP" "$TEST_TMP" >"$F"
    [[ "$(envfile_get "$F" A)" == 'a$b' ]]
    [[ "$(envfile_get "$F" B)" == 'p(ss' ]]
    envfile_load "$F"
    [[ "$C" == "\`touch $TEST_TMP/pwned\`" ]]
    [[ ! -e "$TEST_TMP/pwned" ]]
}

@test "comments, blanks and invalid keys are skipped; one pair of quotes is stripped" {
    printf '# c\n; c\n\n  K1 = "v 1"  \n1BAD=x\nK-2=y\nK3='"'"'q'"'"'\nK4="\x27x\x27"\nK5=last\nK5=wins\n' >"$F"
    [[ "$(envfile_get "$F" K1)" == 'v 1' ]]
    [[ -z "$(envfile_get "$F" 1BAD)" ]]
    [[ "$(envfile_get "$F" K3)" == 'q' ]]
    [[ "$(envfile_get "$F" K4)" == "'x'" ]]
    [[ "$(envfile_get "$F" K5)" == 'wins' ]]
}

@test "a missing file is not an error" {
    run envfile_load "$TEST_TMP/nope.env"
    assert_status 0
}
