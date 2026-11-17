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
echo "$*" >>"$TEST_TMP/rsync-args"
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
/usr/bin/rsync "${args[@]}"
rc=$?
# TEST_VANISH: the copy worked but rsync reports vanished source files (exit 24).
[[ "$rc" -eq 0 && -f "$TEST_TMP/vanish" ]] && exit 24
exit "$rc"
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
    assert_output_contains "site-a sent-log: 2 new frame(s), 48 bytes, +2 index line(s)"
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
    assert_output_contains "site-a sent-log: 1 new frame(s), 24 bytes, +1 index line(s)"
}

@test "a rerun with nothing new adds nothing" {
    run "$SCRIPT" "$DATASET" site-a
    run "$SCRIPT" "$DATASET" site-a
    assert_status 0
    [ "$(wc -l < "$DATASET/site-a/sent-log/index.jsonl")" -eq 2 ]
    assert_output_contains "site-a sent-log: 0 new frame(s), 0 bytes, +0 index line(s)"
}

@test "a log that has no index yet is not a failure" {
    # site-a's review log exists but has never archived a frame.
    run "$SCRIPT" "$DATASET" site-a
    assert_status 0
    assert_output_contains "site-a review-log: 0 new frame(s), 0 bytes, no index yet"
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

@test "a NAME=DIR source is read from this machine without SSH" {
    local_dir="$TEST_TMP/local-daemon"
    mkdir -p "$local_dir/sent-log" "$local_dir/review-log"
    printf 'jpeg' >"$local_dir/sent-log/20260901-000009.jpg"
    printf '{"file": "20260901-000009.jpg", "ts": 1}\n' >"$local_dir/sent-log/index.jsonl"
    run "$SCRIPT" "$DATASET" "site-l=$local_dir" site-a
    assert_status 0
    [ -f "$DATASET/site-l/sent-log/20260901-000009.jpg" ]
    [ "$(wc -l < "$DATASET/site-l/sent-log/index.jsonl")" -eq 1 ]
    [ -f "$DATASET/site-a/sent-log/20260901-000002.jpg" ]
}

@test "rsync exit 24 (files vanished mid-transfer) is a success" {
    touch "$TEST_TMP/vanish"
    run "$SCRIPT" "$DATASET" site-a
    assert_status 0
    assert_output_not_contains "not collected"
    [ "$(wc -l < "$DATASET/site-a/sent-log/index.jsonl")" -eq 2 ]
}

@test "a truncated trailing index line is dropped before the merge" {
    printf '{"file": "half' >>"$REMOTE/site-a/tapo-monitor/sent-log/index.jsonl"
    run "$SCRIPT" "$DATASET" site-a
    assert_status 0
    [ "$(wc -l < "$DATASET/site-a/sent-log/index.jsonl")" -eq 2 ]
    ! grep -q half "$DATASET/site-a/sent-log/index.jsonl"
}

@test "--dry-run reports what a run would add and copies nothing" {
    run "$SCRIPT" --dry-run "$DATASET" site-a
    assert_status 0
    assert_output_contains "site-a sent-log: 2 new frame(s), 48 bytes, +2 index line(s)"
    assert_output_contains "site-a: 2 new frame(s), 48 bytes (dry run, nothing copied)"
    [ ! -e "$DATASET" ]

    # After a real run, a dry run counts only what is new since.
    run "$SCRIPT" "$DATASET" site-a
    add_frame site-a sent-log 20260902-000001.jpg
    run "$SCRIPT" "$DATASET" --dry-run site-a
    assert_status 0
    assert_output_contains "site-a sent-log: 1 new frame(s), 24 bytes, +1 index line(s)"
    [ ! -e "$DATASET/site-a/sent-log/20260902-000001.jpg" ]
    [ "$(wc -l < "$DATASET/site-a/sent-log/index.jsonl")" -eq 2 ]
}

@test "only frames and the index are copied, never other files in the log dir" {
    printf 'x' >"$REMOTE/site-a/tapo-monitor/sent-log/.index.lock"
    printf 'x' >"$REMOTE/site-a/tapo-monitor/sent-log/notes.txt"
    mkdir -p "$REMOTE/site-a/tapo-monitor/sent-log/sub"
    printf 'x' >"$REMOTE/site-a/tapo-monitor/sent-log/sub/deep.jpg"
    run "$SCRIPT" "$DATASET" site-a
    assert_status 0
    [ "$(find "$DATASET/site-a/sent-log" -mindepth 1 -printf "%P\n" | sort | tr '\n' ' ')" = \
        "20260901-000001.jpg 20260901-000002.jpg index.jsonl " ]
    # Frames show people: owner-only locally even when the source file is not.
    [ "$(stat -c %a "$DATASET/site-a/sent-log/20260901-000001.jpg")" = 600 ]
}

@test "a local frame is never overwritten by a changed remote one" {
    run "$SCRIPT" "$DATASET" site-a
    printf 'changed' >"$REMOTE/site-a/tapo-monitor/sent-log/20260901-000001.jpg"
    run "$SCRIPT" "$DATASET" site-a
    assert_status 0
    [ "$(cat "$DATASET/site-a/sent-log/20260901-000001.jpg")" = "jpeg 20260901-000001.jpg" ]
}

@test "--env-file reads only the two log dirs from the host" {
    mkdir -p "$REMOTE/site-a/custom/sent" "$REMOTE/site-a/tapo"
    printf 'jpeg' >"$REMOTE/site-a/custom/sent/20260903-000001.jpg"
    cat >"$REMOTE/site-a/tapo/camera.env" <<'ENV'
TAPO_TELEGRAM_TOKEN=secret-value
export TAPO_SENT_LOG_DIR="/custom/sent"
TAPO_SENT_LOG_RETENTION_DAYS=14
ENV
    # ssh HOST CMD runs CMD in the fake host's home and records what came back.
    stub_command ssh <<'STUB'
host="$1"; shift
cd "$REMOTE/$host" && HOME="$REMOTE/$host" bash -c "$*" | tee -a "$TEST_TMP/ssh-output"
STUB
    # shellcheck disable=SC2088  # the literal ~ is for the (fake) remote shell.
    run "$SCRIPT" "$DATASET" --env-file '~/tapo/camera.env' site-a
    assert_status 0
    [ -f "$DATASET/site-a/sent-log/20260903-000001.jpg" ]
    [ ! -e "$DATASET/site-a/sent-log/20260901-000001.jpg" ]
    assert_output_contains "site-a review-log: not configured on the host, skipped"
    grep -qF /custom/sent "$TEST_TMP/ssh-output"
    if grep -q secret "$TEST_TMP/ssh-output"; then false; fi
}

@test "--sent-dir and --review-dir apply to the sources after them" {
    mkdir -p "$REMOTE/site-b/other/sent"
    printf 'jpeg' >"$REMOTE/site-b/other/sent/20260904-000001.jpg"
    run "$SCRIPT" "$DATASET" site-a --sent-dir /other/sent site-b
    assert_status 0
    [ -f "$DATASET/site-a/sent-log/20260901-000001.jpg" ]
    [ -f "$DATASET/site-b/sent-log/20260904-000001.jpg" ]
    [ ! -e "$DATASET/site-b/sent-log/20260901-000003.jpg" ]
}

@test "--rsh is handed to rsync as the remote shell" {
    run "$SCRIPT" "$DATASET" --rsh "ssh -p 2200" site-a
    assert_status 0
    [ -f "$DATASET/site-a/sent-log/20260901-000001.jpg" ]
    grep -qF -- "-e ssh -p 2200" "$TEST_TMP/rsync-args"
}

@test "RSYNC_RSH from the environment is still the remote shell" {
    # Existing timers set RSYNC_RSH for a host behind a jump; the copy must keep using it.
    RSYNC_RSH="ssh -p 2201" run "$SCRIPT" "$DATASET" site-a
    assert_status 0
    grep -qF -- "-e ssh -p 2201" "$TEST_TMP/rsync-args"
    if grep -qE -- "-e ssh( |$)[^-]" "$TEST_TMP/rsync-args"; then false; fi
}

@test "an unknown option prints usage" {
    run "$SCRIPT" "$DATASET" --bogus site-a
    assert_status 2
    assert_output_contains "Usage:"
}
