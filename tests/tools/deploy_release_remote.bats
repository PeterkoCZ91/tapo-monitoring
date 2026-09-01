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

    stub_command ssh <<'STUB'
shift              # the host: everything below runs on this machine instead
exec bash -c "$*"  # stdin — the tar stream or the remote heredoc — passes through
STUB
    # The workstation's interpreter, fingerprinting the staged tree.
    stub_command python3 <<'STUB'
echo "package abc123def456"
STUB
    # The host's venv interpreter. Fails the selfcheck when the test asks it to.
    stub_command venv-python <<'STUB'
case "${3:-}" in
    version)
        echo "package ${STUB_HOST_FINGERPRINT:-abc123def456}"
        ;;
    selfcheck)
        printf '%s\n%s\n' "${4:-}" "${TAPO_TEST_CREDENTIAL:-<unset>}" \
            >"$TEST_TMP/selfcheck-context"
        printf '%s\n' "$PWD" >>"$TEST_TMP/selfcheck-cwd"
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
