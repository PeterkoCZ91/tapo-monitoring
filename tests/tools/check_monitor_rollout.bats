#!/usr/bin/env bats
#
# tools/check_monitor_rollout.sh — the post-deploy check that runs on the host.
#
# Its whole job is to be believed, so the case that matters most is the one where it cried
# wolf: under the release layout it found cameras.yaml through one path and handed the
# selfcheck another, and reported a config failure on every healthy deploy. The systemd
# and interpreter calls are stubbed; what is under test is the script's own reasoning.

load helper

setup() {
    setup_tools_test

    # A host that is up and quiet, which is the state a good deploy leaves behind.
    stub_command systemctl <<'STUB'
case "$*" in
    *ActiveState*)            echo "${STUB_ACTIVE_STATE:-active}" ;;
    *SubState*)               echo "${STUB_SUB_STATE:-running}" ;;
    *NRestarts*)              echo 0 ;;
    *EnvironmentFiles*)       echo "${STUB_ENV_FILES:-}" ;;
    *ActiveEnterTimestamp*)   echo "Thu 2026-01-01 00:00:00 UTC" ;;
esac
STUB
    # Unset-only default: a test that sets STUB_JOURNAL to the empty string means it.
    stub_command journalctl <<'STUB'
printf '%s\n' "${STUB_JOURNAL-tapo-monitor: loaded 2 camera(s)}"
STUB
    # Stands in for the venv interpreter. It records the config path it was handed, so a
    # test can assert on the path itself and not merely on "the selfcheck passed".
    stub_command venv-python <<'STUB'
case "${3:-}" in
    version)
        echo "package ${STUB_FINGERPRINT:-abc123def456}"
        ;;
    selfcheck)
        printf '%s\n' "${4:-}" >"$TEST_TMP/selfcheck-config"
        if [[ ! -f "${4:-}" ]]; then
            echo "config file not found: ${4:-}"
            exit 1
        fi
        echo "selfcheck: 2 camera(s) ok"
        ;;
esac
STUB
    export TAPO_MONITOR_PYTHON="$STUB_BIN/venv-python"
    export TAPO_MONITOR_UNIT="tapo-monitor.service"
}

teardown() {
    teardown_tools_test
}

# ── layouts ───────────────────────────────────────────────────────────────────────────

# The layout deploy_release.sh builds: the config lives above the releases tree and the
# unit runs the code through the `current` symlink.
make_release_layout() {
    ROOT="$TEST_TMP/tapo-monitor"
    RELEASE="20260101T000000Z-abc123def456"
    mkdir -p "$ROOT/releases/$RELEASE/tools"
    cp "$REPO_ROOT/tools/check_monitor_rollout.sh" "$ROOT/releases/$RELEASE/tools/"
    ln -s "releases/$RELEASE" "$ROOT/current"
    touch "$ROOT/cameras.yaml"
}

# The older arrangement, still on any host that has not been migrated: one directory,
# config beside the package.
make_flat_layout() {
    ROOT="$TEST_TMP/tapo-monitor"
    mkdir -p "$ROOT/tools"
    cp "$REPO_ROOT/tools/check_monitor_rollout.sh" "$ROOT/tools/"
    touch "$ROOT/cameras.yaml"
}

selfcheck_saw() {
    cat "$TEST_TMP/selfcheck-config" 2>/dev/null
}

# ── the regression ────────────────────────────────────────────────────────────────────

@test "release layout: the config is found through the current symlink" {
    make_release_layout

    run "$ROOT/current/tools/check_monitor_rollout.sh"

    assert_status 0
    assert_output_contains "selfcheck: ok"
    assert_output_not_contains "FAILED"
    # The heart of it: the path handed to the selfcheck must be the one that was proven
    # to exist. Resolving `..` against the symlink's own parent instead of its target put
    # the config a level too high here, and every good deploy reported a config failure.
    [[ "$(selfcheck_saw)" == "$ROOT/cameras.yaml" ]] || {
        printf 'selfcheck was handed %s, expected %s\n' "$(selfcheck_saw)" "$ROOT/cameras.yaml"
        return 1
    }
}

@test "release layout: the config is found when run inside the release directory" {
    make_release_layout

    run "$ROOT/releases/$RELEASE/tools/check_monitor_rollout.sh"

    assert_status 0
    [[ "$(selfcheck_saw)" == "$ROOT/cameras.yaml" ]] || {
        printf 'selfcheck was handed %s\n' "$(selfcheck_saw)"
        return 1
    }
}

@test "flat layout: the config beside the package is used as-is" {
    make_flat_layout

    run "$ROOT/tools/check_monitor_rollout.sh"

    assert_status 0
    [[ "$(selfcheck_saw)" == "$ROOT/cameras.yaml" ]] || {
        printf 'selfcheck was handed %s\n' "$(selfcheck_saw)"
        return 1
    }
}

@test "an explicit TAPO_MONITOR_CONFIG wins over the walk-up" {
    make_release_layout
    touch "$TEST_TMP/elsewhere.yaml"
    export TAPO_MONITOR_CONFIG="$TEST_TMP/elsewhere.yaml"

    run "$ROOT/current/tools/check_monitor_rollout.sh"

    assert_status 0
    [[ "$(selfcheck_saw)" == "$TEST_TMP/elsewhere.yaml" ]] || {
        printf 'selfcheck was handed %s\n' "$(selfcheck_saw)"
        return 1
    }
}

@test "no config anywhere: the check fails instead of passing silently" {
    make_release_layout
    rm "$ROOT/cameras.yaml"

    run "$ROOT/current/tools/check_monitor_rollout.sh"

    assert_status 1
    assert_output_contains "selfcheck: FAILED"
    assert_output_contains "monitor rollout check FAILED"
}

# ── the failures this check exists to catch ───────────────────────────────────────────

@test "a crash-looping unit fails the check" {
    make_release_layout
    # `is-active` alone reports "activating" for a unit dying every RestartSec seconds,
    # which is why the check reads ActiveState and SubState.
    export STUB_ACTIVE_STATE="activating" STUB_SUB_STATE="auto-restart"

    run "$ROOT/current/tools/check_monitor_rollout.sh"

    assert_status 1
    assert_output_contains "unit: FAILED"
    assert_output_contains "SubState=auto-restart"
}

@test "a fingerprint other than the expected one fails the check" {
    make_release_layout
    export STUB_FINGERPRINT="0000deadbeef"

    run "$ROOT/current/tools/check_monitor_rollout.sh" "abc123def456"

    assert_status 1
    assert_output_contains "fingerprint: FAILED"
    assert_output_contains "host has 0000deadbeef, expected abc123def456"
}

@test "the expected fingerprint passes" {
    make_release_layout

    run "$ROOT/current/tools/check_monitor_rollout.sh" "abc123def456"

    assert_status 0
    assert_output_contains "fingerprint: ok"
}

@test "without an expected fingerprint the reported one is only shown" {
    make_release_layout

    run "$ROOT/current/tools/check_monitor_rollout.sh"

    assert_status 0
    assert_output_contains "nothing to compare against"
}

@test "a journal without a startup line fails the check" {
    make_release_layout
    export STUB_JOURNAL="tapo-monitor: starting"

    run "$ROOT/current/tools/check_monitor_rollout.sh"

    assert_status 1
    assert_output_contains "startup: FAILED"
}

@test "an exception in the journal fails the check" {
    make_release_layout
    export STUB_JOURNAL="loaded 2 camera(s)
Traceback (most recent call last):
AttributeError: nope"

    run "$ROOT/current/tools/check_monitor_rollout.sh"

    assert_status 1
    assert_output_contains "journal: FAILED"
    assert_output_contains "exception line(s) since start"
}

@test "an empty journal is reported as unknown, not as a failure" {
    make_release_layout
    export STUB_JOURNAL=""

    run "$ROOT/current/tools/check_monitor_rollout.sh"

    assert_status 0
    assert_output_contains "journal: unknown"
}

@test "a missing health state is a note, not a failure" {
    make_release_layout

    run "$ROOT/current/tools/check_monitor_rollout.sh"

    assert_status 0
    # Liveness is deliberately reported rather than asserted: a quiet camera produces a
    # quiet daemon, and that is not a bad deploy.
    assert_output_contains "health-state: unknown"
}

@test "the env file named by the unit is sourced before the selfcheck" {
    make_release_layout
    printf 'TAPO_TEST_CREDENTIAL=from-env-file\n' >"$TEST_TMP/monitor.env"
    export STUB_ENV_FILES="$TEST_TMP/monitor.env"
    stub_command venv-python <<'STUB'
case "${3:-}" in
    version)   echo "package abc123def456" ;;
    selfcheck) printf '%s\n' "${TAPO_TEST_CREDENTIAL:-<unset>}" >"$TEST_TMP/credential-seen" ;;
esac
STUB

    run "$ROOT/current/tools/check_monitor_rollout.sh"

    assert_status 0
    assert_file_contains "$TEST_TMP/credential-seen" "from-env-file"
}
