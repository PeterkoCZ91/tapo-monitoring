#!/usr/bin/env bats
#
# pi_notify.sh — the fleet's Telegram sender, and its per-service cooldown.
#
# Everything else in the project reports through this script, including the OnFailure
# handlers, so its two contracts are worth pinning: an unconfirmed send must not arm the
# cooldown (the alert has not been delivered, so the next attempt must be allowed), and a
# service in cooldown must not reach the network at all.

load helper

setup() {
    setup_tools_test
    SCRIPT="$REPO_ROOT/pi_notify.sh"
    CURL_LOG="$TEST_TMP/curl.log"
    export TAPO_ENV_FILE="$TEST_TMP/secrets.env"
    printf 'TELEGRAM_TOKEN=test-token\nTELEGRAM_CHAT_ID=1234567\n' >"$TAPO_ENV_FILE"

    # The lock path is hardcoded to /tmp inside the script, so the test names its service
    # after its own PID and removes the file again; nothing else here touches /tmp.
    SERVICE="batstest-$$"
    LOCK="/tmp/notify_cooldown_batstest_$$"

    stub_command curl <<'STUB'
printf '%s\n' "$*" >>"$TEST_TMP/curl.log"
if [[ -f "$TEST_TMP/telegram-refuses" ]]; then
    echo '{"ok":false,"description":"Bad Request: chat not found"}'
else
    echo '{"ok":true,"result":{"message_id":1}}'
fi
STUB
}

teardown() {
    rm -f "${LOCK:-}"
    teardown_tools_test
}

assert_curl_not_called() {
    if [[ -s "$CURL_LOG" ]]; then
        printf 'expected no request to be made, got:\n%s\n' "$(cat "$CURL_LOG")"
        return 1
    fi
}

@test "a message is required" {
    run "$SCRIPT"

    [[ "$status" -ne 0 ]] || {
        echo "expected a non-zero exit for a missing message"
        return 1
    }
    assert_curl_not_called
}

@test "no token means no attempt" {
    : >"$TAPO_ENV_FILE"

    run "$SCRIPT" "camera unreachable"

    [[ "$status" -ne 0 ]] || {
        printf 'expected a non-zero exit\n--- output ---\n%s\n' "$output"
        return 1
    }
    assert_output_contains "TELEGRAM_TOKEN missing"
    assert_curl_not_called
}

@test "no chat id means no attempt" {
    printf 'TELEGRAM_TOKEN=test-token\n' >"$TAPO_ENV_FILE"

    run "$SCRIPT" "camera unreachable"

    [[ "$status" -ne 0 ]] || {
        printf 'expected a non-zero exit\n--- output ---\n%s\n' "$output"
        return 1
    }
    assert_output_contains "TELEGRAM_CHAT_ID missing"
    assert_curl_not_called
}

@test "credentials come from the env file, not the environment" {
    run "$SCRIPT" "camera unreachable"

    assert_status 0
    assert_file_contains "$CURL_LOG" "chat_id=1234567"
    assert_file_contains "$CURL_LOG" "text=camera unreachable"
}

@test "a confirmed send with a service name arms the cooldown" {
    run "$SCRIPT" "unit failed" "$SERVICE"

    assert_status 0
    [[ -f "$LOCK" ]] || {
        echo "expected the cooldown stamp $LOCK to be written"
        return 1
    }
}

@test "a service in cooldown does not reach the network" {
    run "$SCRIPT" "unit failed" "$SERVICE"
    assert_status 0
    : >"$CURL_LOG"

    run "$SCRIPT" "unit failed again" "$SERVICE"

    assert_status 0
    assert_output_contains "notify cooldown active for $SERVICE"
    assert_curl_not_called
}

@test "the cooldown expires" {
    run "$SCRIPT" "unit failed" "$SERVICE"
    echo "$(( $(date +%s) - 2000 ))" >"$LOCK"
    : >"$CURL_LOG"

    run "$SCRIPT" "unit failed again" "$SERVICE"

    assert_status 0
    assert_file_contains "$CURL_LOG" "text=unit failed again"
}

@test "an unconfirmed send does not arm the cooldown" {
    # The point of the stamp is "the operator has been told". Writing it on a refused
    # send would silence the next thirty minutes of a problem nobody has heard about.
    touch "$TEST_TMP/telegram-refuses"

    run "$SCRIPT" "unit failed" "$SERVICE"

    assert_status 1
    assert_output_contains "Telegram send failed"
    assert_file_absent "$LOCK"
}

@test "a send without a service name is never suppressed" {
    run "$SCRIPT" "one-off message"
    assert_status 0
    : >"$CURL_LOG"

    run "$SCRIPT" "one-off message"

    assert_status 0
    assert_file_contains "$CURL_LOG" "text=one-off message"
}
