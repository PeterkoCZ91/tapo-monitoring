# shellcheck shell=bash
#
# Shared scaffolding for the tools/*.sh tests.
#
# These scripts deploy and restart the fleet, so a test that reached a real host would be
# the worst possible way to discover a mistake in one of them. setup_tools_test therefore
# gives every test a throwaway $HOME and puts a stub directory at the front of $PATH in
# which ssh, sudo, systemctl and friends are loud failures; a test that wants an answer
# from one of them replaces exactly that stub and nothing else. Anything still reaching
# the network or the local system is a bug in the test, and exit 99 says so by name.

TOOLS_TEST_HELPER_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2034  # every .bats file that loads this helper uses REPO_ROOT.
REPO_ROOT="$(cd -P "$TOOLS_TEST_HELPER_DIR/../.." && pwd)"

# Commands that must never run for real here. curl and ping are on the list because the
# notifier and the host watch call them; tests that exercise those stub them deliberately.
TOOLS_TEST_GUARDED=(ssh scp rsync sudo systemctl systemd-run journalctl curl ping restic docker)

setup_tools_test() {
    # -P: the scripts under test resolve their own paths physically, so a symlinked
    # /tmp would otherwise make every path comparison in here a false failure.
    TEST_TMP="$(cd -P "$(mktemp -d)" && pwd)"
    STUB_BIN="$TEST_TMP/bin"
    mkdir -p "$STUB_BIN"
    export TEST_TMP STUB_BIN
    # check_monitor_rollout.sh reads ~/.local/state and pi_notify.sh writes cooldown
    # files: no test may read, let alone write, the operator's own home.
    HOME="$TEST_TMP/home"
    mkdir -p "$HOME"
    export HOME

    local guarded
    for guarded in "${TOOLS_TEST_GUARDED[@]}"; do
        stub_command "$guarded" <<'STUB'
echo "TEST GUARD: $(basename "$0") must not be called here: $0 $*" >&2
exit 99
STUB
    done
    PATH="$STUB_BIN:$PATH"
    export PATH
}

teardown_tools_test() {
    if [[ -n "${TEST_TMP:-}" && -d "$TEST_TMP" ]]; then
        rm -rf "$TEST_TMP"
    fi
    return 0
}

# stub_command <name>  — body is read from stdin.
stub_command() {
    local name="$1"
    {
        echo '#!/usr/bin/env bash'
        cat
    } >"$STUB_BIN/$name"
    chmod +x "$STUB_BIN/$name"
}

# ── assertions ────────────────────────────────────────────────────────────────────────
# Each prints the whole captured output on failure: a bats failure without it says only
# which line broke, which is not enough to tell a wrong exit code from a missing stub.

# shellcheck disable=SC2154  # $status and $output are set by bats' own `run`.
assert_status() {
    if [[ "$status" -ne "$1" ]]; then
        printf 'expected exit status %s, got %s\n--- output ---\n%s\n' "$1" "$status" "$output"
        return 1
    fi
}

# shellcheck disable=SC2154  # as above: $output comes from `run`.
assert_output_contains() {
    if [[ "$output" != *"$1"* ]]; then
        printf 'expected output to contain: %s\n--- output ---\n%s\n' "$1" "$output"
        return 1
    fi
}

# shellcheck disable=SC2154
assert_output_not_contains() {
    if [[ "$output" == *"$1"* ]]; then
        printf 'expected output NOT to contain: %s\n--- output ---\n%s\n' "$1" "$output"
        return 1
    fi
}

# shellcheck disable=SC2154
assert_output_matches() {
    if [[ ! "$output" =~ $1 ]]; then
        printf 'expected output to match: %s\n--- output ---\n%s\n' "$1" "$output"
        return 1
    fi
}

assert_file_contains() {  # <path> <needle>
    if [[ ! -f "$1" ]]; then
        printf 'expected file %s to exist\n' "$1"
        return 1
    fi
    local content
    content="$(cat "$1")"
    if [[ "$content" != *"$2"* ]]; then
        printf 'expected %s to contain: %s\n--- content ---\n%s\n' "$1" "$2" "$content"
        return 1
    fi
}

assert_file_absent() {
    if [[ -e "$1" ]]; then
        printf 'expected %s not to exist, but it does\n' "$1"
        return 1
    fi
}
