#!/usr/bin/env bats
#
# tools/check_scorer_rollout.sh — what it accepts as a healthy scorer.
#
# The scorer is the one host every camera depends on, and this check is what a deploy
# believes. curl is stubbed with canned responses, so these tests are about the schema
# assertion itself: an older scorer that answers 200 to everything must still fail.

load helper

setup() {
    setup_tools_test
    SCRIPT="$REPO_ROOT/tools/check_scorer_rollout.sh"
    BASE_URL="http://127.0.0.1:8766"
    printf '{"ok":true}' >"$TEST_TMP/health.json"
    metrics_json >"$TEST_TMP/metrics.json"
    stub_command curl <<'STUB'
url="${!#}"
printf '%s\n' "$url" >>"$TEST_TMP/curl-urls"
case "$url" in
    */health)  cat "$TEST_TMP/health.json" ;;
    */metrics) cat "$TEST_TMP/metrics.json" ;;
    *)         exit 22 ;;
esac
STUB
}

teardown() {
    teardown_tools_test
}

# The aggregate metrics document, minus any field named as an argument.
metrics_json() {
    local omit=" $* " field first=1
    printf '{'
    for field in requests completed failed score_successes person_candidates \
                 animal_candidates malformed_responses failure_reasons inference_runs \
                 in_flight max_in_flight request_seconds_total request_seconds_max \
                 score_seconds_total score_seconds_max request_seconds_p50 \
                 request_seconds_p95 score_seconds_p50 score_seconds_p95 sources; do
        [[ "$omit" == *" $field "* ]] && continue
        ((first)) || printf ','
        first=0
        case "$field" in
            failure_reasons|sources) printf '"%s":{}' "$field" ;;
            *)                       printf '"%s":0' "$field" ;;
        esac
    done
    printf '}'
}

@test "a scorer answering the current schema passes" {
    run "$SCRIPT" "$BASE_URL"

    assert_status 0
    assert_output_contains "scorer aggregate metrics rollout: ok"
}

@test "the base URL is used without a doubled slash" {
    run "$SCRIPT" "$BASE_URL/"

    assert_status 0
    assert_file_contains "$TEST_TMP/curl-urls" "http://127.0.0.1:8766/health"
    if grep -q '//health' "$TEST_TMP/curl-urls"; then
        printf 'the trailing slash was not stripped\n--- urls ---\n%s\n' "$(cat "$TEST_TMP/curl-urls")"
        return 1
    fi
}

@test "no URL is a usage error" {
    run "$SCRIPT"

    assert_status 2
    assert_output_contains "usage:"
}

@test "more than one URL is a usage error" {
    run "$SCRIPT" "$BASE_URL" "$BASE_URL"

    assert_status 2
    assert_output_contains "usage:"
}

@test "a scorer that does not answer /health fails" {
    stub_command curl <<'STUB'
exit 7
STUB

    run "$SCRIPT" "$BASE_URL"

    assert_status 1
    assert_output_contains "scorer health check failed"
}

@test "a scorer that answers /health but not /metrics fails" {
    stub_command curl <<'STUB'
case "${!#}" in
    */health) cat "$TEST_TMP/health.json" ;;
    *)        exit 22 ;;
esac
STUB

    run "$SCRIPT" "$BASE_URL"

    assert_status 1
    assert_output_contains "scorer metrics check failed"
}

@test "health that is not ok fails even though the request succeeded" {
    printf '{"ok":false,"reason":"model not loaded"}' >"$TEST_TMP/health.json"

    run "$SCRIPT" "$BASE_URL"

    assert_status 1
    assert_output_contains "scorer health response is not ok"
}

@test "a metrics document from before the aggregate rollout fails, naming the field" {
    # This is the case the check exists for: the service is up and answering, but it is
    # the old build, so the digest would silently lose the percentiles.
    metrics_json request_seconds_p95 sources >"$TEST_TMP/metrics.json"

    run "$SCRIPT" "$BASE_URL"

    assert_status 1
    assert_output_contains "scorer metrics schema is older than the aggregate rollout"
    assert_output_contains "missing fields: request_seconds_p95, sources"
}

@test "a scalar where an object belongs fails" {
    metrics_json failure_reasons | sed 's/^{/{"failure_reasons":0,/' >"$TEST_TMP/metrics.json"

    run "$SCRIPT" "$BASE_URL"

    assert_status 1
    assert_output_contains "scorer failure_reasons is not an object"
}

@test "a body that is not JSON fails" {
    printf '<html>502 Bad Gateway</html>' >"$TEST_TMP/health.json"

    run "$SCRIPT" "$BASE_URL"

    assert_status 1
    assert_output_contains "scorer returned invalid JSON"
}
