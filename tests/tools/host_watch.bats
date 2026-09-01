#!/usr/bin/env bats
#
# tools/host_watch.sh — the escalation logic of the fleet's dead-man's switch.
#
# Nobody notices an absent message, so this script is the only thing that can say "that
# host is dead". Its whole value is in when it speaks and when it stays quiet: one lost
# echo pair must not alert, a host that stays down must not alert every two minutes, and
# an alert Telegram never confirmed must not consume its cooldown. ping and curl are
# stubbed, so every test below is about those decisions.

load helper

setup() {
    setup_tools_test
    SCRIPT="$REPO_ROOT/tools/host_watch.sh"
    STATE_DIR="$TEST_TMP/state"
    TELEGRAM_LOG="$TEST_TMP/telegram.log"
    touch "$TEST_TMP/hosts-down" "$TEST_TMP/health-down"

    printf 'TELEGRAM_TOKEN=test-token\nTELEGRAM_CHAT_ID=1234567\n' >"$TEST_TMP/secrets.env"
    export HOST_WATCH_ENV_FILE="$TEST_TMP/host-watch.env"
    write_env "host1=192.0.2.1"

    # Down hosts and failing health URLs are listed in files the tests write, so a single
    # stub covers every scenario.
    stub_command ping <<'STUB'
addr="${!#}"
if grep -qxF "$addr" "$TEST_TMP/hosts-down"; then
    exit 1
fi
if grep -qxF "$addr flag-refused" "$TEST_TMP/hosts-down" && [[ "$*" == *" -i "* ]]; then
    # Some ping builds allow sub-second intervals only to root. A refused flag is a
    # complaint on stderr, not a down host.
    echo "ping: cannot flood; minimal interval allowed for user is 200ms" >&2
    exit 2
fi
exit 0
STUB
    stub_command curl <<'STUB'
case "$*" in
    *api.telegram.org*)
        for arg in "$@"; do
            case "$arg" in
                text=*) printf '%s\n' "${arg#text=}" >>"$TEST_TMP/telegram.log" ;;
            esac
        done
        if [[ -f "$TEST_TMP/telegram-refuses" ]]; then
            echo '{"ok":false,"description":"Forbidden: bot was blocked by the user"}'
        else
            echo '{"ok":true,"result":{"message_id":1}}'
        fi
        ;;
    *)
        if grep -qxF "${!#}" "$TEST_TMP/health-down"; then
            exit 22
        fi
        ;;
esac
STUB
}

teardown() {
    teardown_tools_test
}

# The env file is how a real host is configured, so the tests go through it too.
write_env() {  # <targets> [fails] [cooldown]
    cat >"$TEST_TMP/host-watch.env" <<ENV
WATCH_TARGETS="$1"
WATCH_FAILS=${2:-1}
WATCH_COOLDOWN=${3:-1800}
WATCH_STATE_DIR=$TEST_TMP/state
TAPO_ENV_FILE=$TEST_TMP/secrets.env
ENV
}

telegram_messages() {
    cat "$TELEGRAM_LOG" 2>/dev/null
}

assert_telegram_contains() {
    if [[ "$(telegram_messages)" != *"$1"* ]]; then
        printf 'expected a Telegram message containing: %s\n--- sent ---\n%s\n' \
            "$1" "$(telegram_messages)"
        return 1
    fi
}

assert_no_telegram() {
    if [[ -s "$TELEGRAM_LOG" ]]; then
        printf 'expected no Telegram message, got:\n%s\n' "$(telegram_messages)"
        return 1
    fi
}

# ── configuration ─────────────────────────────────────────────────────────────────────

@test "no targets configured is an error, not a quiet success" {
    # A watchman watching nothing is the failure mode this script cannot report on
    # itself, so it has to be loud at startup.
    printf 'TAPO_ENV_FILE=%s/secrets.env\n' "$TEST_TMP" >"$TEST_TMP/host-watch.env"

    run "$SCRIPT"

    [[ "$status" -ne 0 ]] || {
        printf 'expected a non-zero exit\n--- output ---\n%s\n' "$output"
        return 1
    }
    assert_output_contains "WATCH_TARGETS missing"
}

@test "a target that does not parse is a watcher failure" {
    write_env "host1"

    run "$SCRIPT"

    assert_status 1
    assert_output_contains "cannot parse target 'host1'"
    assert_no_telegram
}

@test "a target with no address does not parse either" {
    write_env "host1="

    run "$SCRIPT"

    assert_status 1
    assert_output_contains "cannot parse target"
}

# ── escalation ────────────────────────────────────────────────────────────────────────

@test "a single miss is counted, not announced" {
    write_env "host1=192.0.2.1" 3
    echo "192.0.2.1" >"$TEST_TMP/hosts-down"

    run "$SCRIPT"

    assert_status 0
    assert_output_contains "host1.ping miss 1/3"
    assert_no_telegram
    assert_file_contains "$STATE_DIR/host1.ping.miss" "1"
}

@test "the alert comes on the configured miss, not before" {
    write_env "host1=192.0.2.1" 3
    echo "192.0.2.1" >"$TEST_TMP/hosts-down"

    run "$SCRIPT"
    assert_no_telegram
    run "$SCRIPT"
    assert_no_telegram
    run "$SCRIPT"

    assert_status 0
    assert_telegram_contains "host-watch: host1 unreachable"
    assert_file_contains "$STATE_DIR/host1.ping.miss" "3"
    [[ -f "$STATE_DIR/host1.ping.alerted" ]] || {
        echo "expected the episode to be marked alerted"
        return 1
    }
}

@test "a host that stays down does not alert again until the cooldown expires" {
    echo "192.0.2.1" >"$TEST_TMP/hosts-down"

    run "$SCRIPT"
    assert_telegram_contains "unreachable"
    run "$SCRIPT"

    assert_status 0
    assert_output_contains "host1.ping still down, cooldown active"
    [[ "$(telegram_messages | wc -l)" -eq 1 ]] || {
        printf 'expected exactly one alert\n%s\n' "$(telegram_messages)"
        return 1
    }
}

@test "the cooldown is measured, not permanent" {
    write_env "host1=192.0.2.1" 1 60
    echo "192.0.2.1" >"$TEST_TMP/hosts-down"

    run "$SCRIPT"
    # Age the stamp past the cooldown instead of sleeping through it.
    echo "$(( $(date +%s) - 120 ))" >"$STATE_DIR/host1.ping.last_alert"
    run "$SCRIPT"

    assert_status 0
    [[ "$(telegram_messages | wc -l)" -eq 2 ]] || {
        printf 'expected a second alert after the cooldown\n%s\n' "$(telegram_messages)"
        return 1
    }
}

@test "an unconfirmed alert does not consume its cooldown" {
    # Telegram answering anything but ok:true means the operator has not been told, so
    # the state must stay as it was and the next pass must try again.
    echo "192.0.2.1" >"$TEST_TMP/hosts-down"
    touch "$TEST_TMP/telegram-refuses"

    run "$SCRIPT"

    assert_status 1
    assert_output_contains "Telegram send failed"
    assert_file_absent "$STATE_DIR/host1.ping.last_alert"
    assert_file_absent "$STATE_DIR/host1.ping.alerted"
}

# ── recovery ──────────────────────────────────────────────────────────────────────────

@test "a host that comes back is reported with the length of the outage" {
    echo "192.0.2.1" >"$TEST_TMP/hosts-down"
    run "$SCRIPT"
    echo "$(( $(date +%s) - 900 ))" >"$STATE_DIR/host1.ping.down_since"
    : >"$TEST_TMP/hosts-down"

    run "$SCRIPT"

    assert_status 0
    [[ "$(telegram_messages | tail -1)" == *"host1 reachable again (down 15 min)"* ]] || {
        printf 'unexpected recovery message: %s\n' "$(telegram_messages | tail -1)"
        return 1
    }
    assert_file_absent "$STATE_DIR/host1.ping.alerted"
    assert_file_absent "$STATE_DIR/host1.ping.down_since"
    assert_file_absent "$STATE_DIR/host1.ping.miss"
}

@test "a host that never alerted recovers silently" {
    write_env "host1=192.0.2.1" 3
    echo "192.0.2.1" >"$TEST_TMP/hosts-down"
    run "$SCRIPT"
    : >"$TEST_TMP/hosts-down"

    run "$SCRIPT"

    assert_status 0
    assert_no_telegram
    assert_file_absent "$STATE_DIR/host1.ping.miss"
}

@test "an unconfirmed recovery keeps the episode open" {
    echo "192.0.2.1" >"$TEST_TMP/hosts-down"
    run "$SCRIPT"
    : >"$TEST_TMP/hosts-down"
    touch "$TEST_TMP/telegram-refuses"

    run "$SCRIPT"

    assert_status 1
    # Still marked alerted: closing it here would swallow the recovery message entirely.
    [[ -f "$STATE_DIR/host1.ping.alerted" ]] || {
        echo "the episode was closed without a confirmed recovery message"
        return 1
    }
}

# ── ping and health are separate findings ─────────────────────────────────────────────

@test "a failing health check on a live host alerts on its own" {
    write_env "host1=192.0.2.1,health=http://192.0.2.1:8766/health"
    echo "http://192.0.2.1:8766/health" >"$TEST_TMP/health-down"

    run "$SCRIPT"

    assert_status 0
    assert_telegram_contains "host1 health check failing"
    assert_file_absent "$STATE_DIR/host1.ping.alerted"
    assert_file_contains "$STATE_DIR/host1.health.miss" "1"
}

@test "a dead host costs one alert, not two" {
    write_env "host1=192.0.2.1,health=http://192.0.2.1:8766/health"
    echo "192.0.2.1" >"$TEST_TMP/hosts-down"
    echo "http://192.0.2.1:8766/health" >"$TEST_TMP/health-down"

    run "$SCRIPT"

    assert_status 0
    assert_telegram_contains "host1 unreachable"
    [[ "$(telegram_messages | wc -l)" -eq 1 ]] || {
        printf 'expected only the ping alert\n%s\n' "$(telegram_messages)"
        return 1
    }
    # The health check is skipped entirely while the host is down, so its episode is
    # neither grown nor reset: a service that stayed dead needs fresh misses afterwards.
    assert_file_absent "$STATE_DIR/host1.health.miss"
}

@test "a healthy host and service say nothing at all" {
    write_env "host1=192.0.2.1,health=http://192.0.2.1:8766/health"

    run "$SCRIPT"

    assert_status 0
    assert_no_telegram
}

# ── details that only bite in production ──────────────────────────────────────────────

@test "a ping build that refuses the sub-second interval is not a down host" {
    echo "192.0.2.1 flag-refused" >"$TEST_TMP/hosts-down"

    run "$SCRIPT"

    assert_status 0
    assert_no_telegram
    assert_file_absent "$STATE_DIR/host1.ping.miss"
}

@test "a target name with dots and colons still yields one state file per check" {
    write_env "host-one.example=192.0.2.1"
    echo "192.0.2.1" >"$TEST_TMP/hosts-down"

    run "$SCRIPT"

    assert_status 0
    assert_file_contains "$STATE_DIR/host-one_example.ping.miss" "1"
    # The name reaches the operator unmangled; only the file name is sanitised.
    assert_telegram_contains "host-one.example unreachable"
}

@test "several targets are each judged on their own" {
    write_env "host1=192.0.2.1 host2=192.0.2.2"
    echo "192.0.2.2" >"$TEST_TMP/hosts-down"

    run "$SCRIPT"

    assert_status 0
    assert_telegram_contains "host2 unreachable"
    if [[ "$(telegram_messages)" == *"host1 unreachable"* ]]; then
        printf 'the healthy host was alerted on too:\n%s\n' "$(telegram_messages)"
        return 1
    fi
    assert_file_absent "$STATE_DIR/host1.ping.miss"
}
