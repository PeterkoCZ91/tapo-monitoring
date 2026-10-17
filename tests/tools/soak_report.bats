#!/usr/bin/env bats
#
# tools/soak_report.sh — the soak-test report. The parsing and arithmetic are covered by
# tests/test_soak_report.py; this is about the shell around it: that the probe it sends to
# a host reads the right things (and passes --user through), that an unreachable host is a
# finding rather than the end of the report, and that the fixtures dry run works.
#
# ssh is stubbed so that the probe script it is given on stdin runs locally against
# stubbed systemctl/journalctl: the real probe text is what gets tested.

load helper

setup() {
    setup_tools_test
    SCRIPT="$REPO_ROOT/tools/soak_report.sh"
    FIXTURES="$REPO_ROOT/tests/fixtures/soak"
    CALLS="$TEST_TMP/calls.log"

    stub_command ssh <<'STUB'
host=""
while (($#)); do
    case "$1" in
        -o) shift 2 ;;
        *)  host="$1"; shift; break ;;
    esac
done
if [[ "$host" == down-host ]]; then
    echo "ssh: connect to host down-host port 22: Connection refused" >&2
    exit 255
fi
echo "ssh $host" >>"$TEST_TMP/calls.log"
exec "$@"
STUB
    stub_command systemctl <<'STUB'
echo "systemctl $*" >>"$TEST_TMP/calls.log"
if [[ " $* " == *" show "* ]]; then
    printf 'LoadState=%s\nActiveState=active\nSubState=running\nResult=success\nNRestarts=0\n' \
        "${STUB_LOAD_STATE:-loaded}"
    printf 'MemoryCurrent=1048576\nCPUUsageNSec=1000000000\nActiveEnterTimestampMonotonic=1000000\n'
fi
STUB
    stub_command journalctl <<'STUB'
echo "journalctl $*" >>"$TEST_TMP/calls.log"
printf '%s\n' "2026-10-02T08:00:00+0000 h tapo-monitor[1]: INFO audit camera=cam-a path=live action=send etype=person start=1.0 event_age_s=20.0 telegram=true"
printf '%s\n' "2026-10-02T08:00:01+0000 h tapo-monitor[1]: ERROR something broke"
printf '%s\n' "2026-10-02T08:00:02+0000 h tapo-monitor[1]: INFO unrelated chatter"
STUB
}

teardown() { teardown_tools_test; }

@test "no hosts prints usage and exits 2" {
    run "$SCRIPT"
    assert_status 2
    assert_output_contains "usage:"
}

@test "an unsafe --since is refused before anything runs" {
    run "$SCRIPT" --since '1; rm -rf /' host1
    assert_status 2
    assert_output_contains "--since wants a number"
}

@test "dry run over the fixtures reports and exits 1 on findings" {
    run "$SCRIPT" --from-dir "$FIXTURES" host-a host-bad
    assert_status 1
    assert_output_contains "== host-a  [OK]"
    assert_output_contains "== host-bad  [ATTENTION]"
    assert_output_contains "median 85 s p90 163 s"
}

@test "dry run: a healthy host alone exits 0" {
    run "$SCRIPT" --from-dir "$FIXTURES" host-a
    assert_status 0
    assert_output_contains "all hosts OK"
}

@test "SOAK_HOSTS supplies the hosts and label=alias is accepted" {
    SOAK_HOSTS="one=host-a" run "$SCRIPT" --from-dir "$FIXTURES"
    assert_status 0
    assert_output_contains "== one  [OK]"
}

@test "--json emits parseable JSON" {
    run "$SCRIPT" --from-dir "$FIXTURES" --json host-a
    assert_status 0
    printf '%s' "$output" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["hosts"]["host-a"]["alerts"]["sent_ok"] == 5'
}

@test "the real probe runs over ssh: audit send and ERROR are counted from the journal" {
    run "$SCRIPT" --since 6 myhost
    assert_status 1
    assert_output_contains "== myhost"
    assert_output_contains "sent 1, failed 0"
    assert_output_contains "Traceback|ERROR|CRITICAL 1"
    assert_file_contains "$CALLS" "--since 6 hours ago"
    assert_file_contains "$CALLS" "systemctl show tapo-monitor.service"
}

@test "--user sends systemctl and journalctl to the user manager" {
    run "$SCRIPT" --user myhost
    assert_file_contains "$CALLS" "systemctl --user show"
    assert_file_contains "$CALLS" "journalctl --user -u tapo-monitor.service"
}

@test "a unit that is not loaded fails even though systemctl says Result=success" {
    STUB_LOAD_STATE=not-found run "$SCRIPT" myhost
    assert_status 1
    assert_output_contains "unit not loaded (LoadState=not-found)"
}

@test "an unreachable host is its own finding and the others are still reported" {
    run "$SCRIPT" down-host myhost
    assert_status 1
    assert_output_contains "== down-host  [UNREACHABLE]"
    assert_output_contains "== myhost"
}

@test "baseline is saved on first use and compared on the second" {
    run "$SCRIPT" --from-dir "$FIXTURES" --baseline "$TEST_TMP/base.json" host-a
    assert_status 0
    assert_output_contains "saved this run as the baseline"
    run "$SCRIPT" --from-dir "$FIXTURES" --baseline "$TEST_TMP/base.json" host-a
    assert_status 0
    assert_output_not_contains "vs baseline"
}

@test "the remote filter keeps Stopguard warnings and episode failures" {
    stub_command journalctl <<'STUB'
printf '%s\n' \
    'WARNING PTZ stop guard Stop failed name=cam error=TimeoutError' \
    'WARNING PTZ stop guard could not verify tracking OFF' \
    'WARNING PTZ stop guard episode cam: beyond_limit=True first_stop_ok=+0.10s off=verified+0.20s' \
    'WARNING PTZ stop guard episode cam: beyond_limit=False first_stop_ok=none off=unverified' \
    'WARNING rotation stop cam: shutdown tracking OFF unconfirmed' \
    'INFO PTZ stop guard episode cam: beyond_limit=False first_stop_ok=+0.10s off=verified+0.20s'
STUB
    run "$SCRIPT" --json myhost
    assert_status 1
    printf '%s' "$output" | python3 -c 'import json,sys; r=json.load(sys.stdin)["hosts"]["myhost"]; assert r["stopguard_problems"] == 5; assert r["errors"] == 0; assert any("Stopguard" in f for f in r["findings"])'
}
