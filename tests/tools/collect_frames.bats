#!/usr/bin/env bats
#
# tools/collect_frames.sh — the nightly pull of every host's frame logs into one dataset.
#
# Its whole value is that the dataset outlives the hosts' retention: frames are never
# deleted locally, and index lines a host has already pruned stay in the local index.
# rsync is stubbed to copy from a fake remote tree, so nothing leaves the test directory.

load helper

setup() {
    setup_tools_test
    SCRIPT="$REPO_ROOT/tools/collect_frames.sh"
    REMOTE="$TEST_TMP/remote"
    DATASET="$TEST_TMP/dataset"
    export REMOTE
    for host in site-a site-b; do
        mkdir -p "$REMOTE/$host/tapo-monitor/sent-log" "$REMOTE/$host/tapo-monitor/review-log"
    done
    add_frame site-a sent-log 20260901-000001.jpg
    add_frame site-a sent-log 20260901-000002.jpg
    add_frame site-b sent-log 20260901-000003.jpg

    # host:path becomes $REMOTE/host/path; a host listed in $TEST_TMP/down fails like an
    # unreachable one. Everything else is the real rsync, so --exclude and friends hold.
    touch "$TEST_TMP/down"
    stub_command rsync <<'STUB'
args=()
for arg in "$@"; do
    if [[ "$arg" == *:* && "$arg" != -* ]]; then
        host="${arg%%:*}"
        if grep -qxF "$host" "$TEST_TMP/down"; then
            echo "ssh: connect to host $host: Connection refused" >&2
            exit 255
        fi
        arg="$REMOTE/$host/${arg#*:}"
    fi
    args+=("$arg")
done
exec /usr/bin/rsync "${args[@]}"
STUB
}

teardown() {
    teardown_tools_test
}

add_frame() {
    local host="$1" log="$2" name="$3"
    printf 'jpeg %s' "$name" >"$REMOTE/$host/tapo-monitor/$log/$name"
    printf '{"file": "%s", "ts": 1}\n' "$name" >>"$REMOTE/$host/tapo-monitor/$log/index.jsonl"
}

@test "first run into an empty dataset copies frames and the whole index" {
    # With an empty local index an awk NR == FNR merge swallowed every fresh line.
    run "$SCRIPT" "$DATASET" site-a site-b
    assert_status 0
    [ -f "$DATASET/site-a/sent-log/20260901-000002.jpg" ]
    [ "$(wc -l < "$DATASET/site-a/sent-log/index.jsonl")" -eq 2 ]
    [ "$(wc -l < "$DATASET/site-b/sent-log/index.jsonl")" -eq 1 ]
    assert_output_contains "site-a sent-log: +2 index line(s)"
}

@test "frames and index lines the host has pruned are kept locally" {
    run "$SCRIPT" "$DATASET" site-a
    assert_status 0
    # The host prunes its oldest frame and index line, then records a new one.
    rm "$REMOTE/site-a/tapo-monitor/sent-log/20260901-000001.jpg"
    sed -i '1d' "$REMOTE/site-a/tapo-monitor/sent-log/index.jsonl"
    add_frame site-a sent-log 20260902-000001.jpg

    run "$SCRIPT" "$DATASET" site-a
    assert_status 0
    [ -f "$DATASET/site-a/sent-log/20260901-000001.jpg" ]
    [ -f "$DATASET/site-a/sent-log/20260902-000001.jpg" ]
    [ "$(wc -l < "$DATASET/site-a/sent-log/index.jsonl")" -eq 3 ]
    assert_output_contains "site-a sent-log: +1 index line(s)"
}

@test "a rerun with nothing new adds nothing" {
    run "$SCRIPT" "$DATASET" site-a
    run "$SCRIPT" "$DATASET" site-a
    assert_status 0
    [ "$(wc -l < "$DATASET/site-a/sent-log/index.jsonl")" -eq 2 ]
    assert_output_contains "site-a sent-log: +0 index line(s)"
}

@test "a log that has no index yet is not a failure" {
    # site-a's review log exists but has never archived a frame.
    run "$SCRIPT" "$DATASET" site-a
    assert_status 0
    assert_output_contains "site-a review-log: no index yet"
}

@test "an unreachable host is reported and does not cost the others their run" {
    echo site-a >"$TEST_TMP/down"
    run "$SCRIPT" "$DATASET" site-a site-b
    assert_status 1
    assert_output_contains "site-a sent-log: not collected"
    [ "$(wc -l < "$DATASET/site-b/sent-log/index.jsonl")" -eq 1 ]
}

@test "without a dataset and a host it prints usage" {
    run "$SCRIPT" "$DATASET"
    assert_status 2
    assert_output_contains "Usage:"
}
