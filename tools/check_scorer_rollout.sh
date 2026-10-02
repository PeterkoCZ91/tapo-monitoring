#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 SCORER_BASE_URL" >&2
    exit 2
fi

base_url="${1%/}"
timeout_seconds="${SCORER_CHECK_TIMEOUT:-5}"

health="$(curl --fail --silent --show-error --max-time "$timeout_seconds" \
    "$base_url/health")" || {
    echo "scorer health check failed" >&2
    exit 1
}
# /metrics needs the shared token when the service has one (TAPO_SCORER_TOKEN, or
# TAPO_SCORER_TOKEN_FILE); it goes in through a curl config on a pipe, not the command line.
auth_value="${TAPO_SCORER_TOKEN:-}"
if [[ -n "${TAPO_SCORER_TOKEN_FILE:-}" && -r "$TAPO_SCORER_TOKEN_FILE" ]]; then
    auth_value="$(<"$TAPO_SCORER_TOKEN_FILE")"
fi
if [[ -n "$auth_value" ]]; then
    metrics="$(curl --fail --silent --show-error --max-time "$timeout_seconds" \
        --config <(printf 'header = "Authorization: Bearer %s"\n' "$auth_value") \
        "$base_url/metrics")" || {
        echo "scorer metrics check failed" >&2
        exit 1
    }
else
    metrics="$(curl --fail --silent --show-error --max-time "$timeout_seconds" \
        "$base_url/metrics")" || {
        echo "scorer metrics check failed (a service with a token needs TAPO_SCORER_TOKEN)" >&2
        exit 1
    }
fi

python3 - "$health" "$metrics" <<'PY'
import json
import sys

try:
    health = json.loads(sys.argv[1])
    metrics = json.loads(sys.argv[2])
except (IndexError, json.JSONDecodeError):
    print("scorer returned invalid JSON", file=sys.stderr)
    raise SystemExit(1)

if health.get("ok") is not True:
    print("scorer health response is not ok", file=sys.stderr)
    raise SystemExit(1)

required = {
    "requests",
    "completed",
    "failed",
    "score_successes",
    "person_candidates",
    "animal_candidates",
    "malformed_responses",
    "failure_reasons",
    "inference_runs",
    "in_flight",
    "max_in_flight",
    "request_seconds_total",
    "request_seconds_max",
    "score_seconds_total",
    "score_seconds_max",
    "request_seconds_p50",
    "request_seconds_p95",
    "score_seconds_p50",
    "score_seconds_p95",
    "sources",
}
missing = sorted(required.difference(metrics))
if missing:
    print("scorer metrics schema is older than the aggregate rollout", file=sys.stderr)
    print("missing fields: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)

for name in ("failure_reasons", "sources"):
    if not isinstance(metrics[name], dict):
        print(f"scorer {name} is not an object", file=sys.stderr)
        raise SystemExit(1)

print("scorer aggregate metrics rollout: ok")
PY
