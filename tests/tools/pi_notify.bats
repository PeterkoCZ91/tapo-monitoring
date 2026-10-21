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

    # The cooldown stamp lives under TAPO_NOTIFY_LOCK_DIR (/run in production).
    export TAPO_NOTIFY_LOCK_DIR="$TEST_TMP/run"
    mkdir -p "$TAPO_NOTIFY_LOCK_DIR"
    SERVICE="batstest-$$"
    LOCK="$TAPO_NOTIFY_LOCK_DIR/notify_cooldown_batstest_$$"

    stub_command curl <<'STUB'
cfg=""; prev=""
for a in "$@"; do [[ "$prev" == --config ]] && cfg="$(cat "$a")"; prev="$a"; done
printf '%s\n' "$* $cfg" >>"$TEST_TMP/curl.log"
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

@test "operational credentials override the camera destination" {
    printf 'TAPO_OPS_TELEGRAM_TOKEN=operator-bot\nTAPO_OPS_TELEGRAM_CHAT_ID=operator-chat\n' >>"$TAPO_ENV_FILE"
    run "$SCRIPT" "service failed"
    assert_status 0
    assert_file_contains "$CURL_LOG" "botoperator-bot/sendMessage"
    assert_file_contains "$CURL_LOG" "chat_id=operator-chat"
}

@test "partial operational credentials never mix destinations" {
    printf 'TAPO_OPS_TELEGRAM_TOKEN=operator-bot\n' >>"$TAPO_ENV_FILE"
    run "$SCRIPT" "service failed"
    [[ "$status" -ne 0 ]]
    assert_curl_not_called
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
    [[ "$(wc -l <"$CURL_LOG")" -eq 1 ]]
}

@test "a send without a service name is never suppressed" {
    run "$SCRIPT" "one-off message"
    assert_status 0
    : >"$CURL_LOG"

    run "$SCRIPT" "one-off message"

    assert_status 0
    assert_file_contains "$CURL_LOG" "text=one-off message"
}

@test "temporary network startup failure is retried before arming cooldown" {
    stub_command sleep <<'STUB'
exit 0
STUB
    stub_command curl <<'STUB'
cfg=""; prev=""
for a in "$@"; do [[ "$prev" == --config ]] && cfg="$(cat "$a")"; prev="$a"; done
printf '%s\n' "$* $cfg" >>"$TEST_TMP/curl.log"
if [[ ! -f "$TEST_TMP/network-ready" ]]; then
    touch "$TEST_TMP/network-ready"
    exit 6
fi
echo '{"ok":true,"result":{"message_id":1}}'
STUB

    run "$SCRIPT" "unit online" "$SERVICE"

    assert_status 0
    [[ "$(wc -l <"$CURL_LOG")" -eq 2 ]]
    [[ -f "$LOCK" ]]
}

@test "persistent network failure stops after three attempts without cooldown" {
    stub_command sleep <<'STUB'
exit 0
STUB
    stub_command curl <<'STUB'
cfg=""; prev=""
for a in "$@"; do [[ "$prev" == --config ]] && cfg="$(cat "$a")"; prev="$a"; done
printf '%s\n' "$* $cfg" >>"$TEST_TMP/curl.log"
exit 7
STUB

    run "$SCRIPT" "unit online" "$SERVICE"

    assert_status 1
    assert_output_contains "Telegram send failed"
    [[ "$(wc -l <"$CURL_LOG")" -eq 3 ]]
    assert_file_absent "$LOCK"
}

@test "a malformed cooldown stamp is ignored, never evaluated" {
    printf 'x[$(touch %s/pwned)]\n' "$TEST_TMP" >"$LOCK"

    run "$SCRIPT" "unit failed" "$SERVICE"

    assert_status 0
    assert_file_absent "$TEST_TMP/pwned"
    assert_file_contains "$CURL_LOG" "text=unit failed"
}

@test "the env file is data: no expansion, no execution" {
    printf 'TELEGRAM_TOKEN="dummy$b(p`touch %s/pwned`"\nTELEGRAM_CHAT_ID=1234567\n' "$TEST_TMP" >"$TAPO_ENV_FILE"

    run "$SCRIPT" "camera unreachable"

    assert_status 0
    assert_file_absent "$TEST_TMP/pwned"
    assert_file_contains "$CURL_LOG" 'botdummy$b(p`touch '
}

@test "the token is not in curl's argv" {
    stub_command curl <<'STUB'
printf '%s\n' "$*" >"$TEST_TMP/argv.log"
echo '{"ok":true}'
STUB

    run "$SCRIPT" "camera unreachable"

    assert_status 0
    if grep -q 'test-token' "$TEST_TMP/argv.log"; then
        echo "token leaked into argv"; return 1
    fi
}
