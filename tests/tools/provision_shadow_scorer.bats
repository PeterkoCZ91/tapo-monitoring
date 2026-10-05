#!/usr/bin/env bats
#
# tools/provision_shadow_scorer.sh — validation, the rendered unit, and --dry-run.
#
# Nothing here installs a unit or touches systemd: every test either ends in a refusal,
# renders to stdout (--print-unit), or runs --dry-run against a scratch unit directory with
# the root check faked through TAPO_PROVISION_EUID (a dry run never writes).

load helper

setup() {
    setup_tools_test
    SCRIPT="$REPO_ROOT/tools/provision_shadow_scorer.sh"
    mkdir -p "$TEST_TMP/src/tapo_monitor" "$TEST_TMP/units" "$TEST_TMP/models"
    : >"$TEST_TMP/models/m.onnx"
    printf '#!/bin/sh\nexit 0\n' >"$TEST_TMP/python"
    chmod +x "$TEST_TMP/python"
    COMMON=(--name rfdetr --model "$TEST_TMP/models/m.onnx" --port 8767 --user tester
            --source-dir "$TEST_TMP/src" --python "$TEST_TMP/python")
    export TAPO_PROVISION_UNIT_DIR="$TEST_TMP/units"
}

teardown() {
    teardown_tools_test
}

@test "--help prints the header and works without root" {
    run "$SCRIPT" --help

    assert_status 0
    assert_output_contains "--token-file PATH"
    assert_output_not_contains "set -euo pipefail"
}

@test "an unknown argument is refused by name" {
    run "$SCRIPT" --bogus

    assert_status 1
    assert_output_contains "unknown argument --bogus"
}

@test "an option without its value is an error" {
    local flag
    for flag in --name --model --port --arch --input-size --token-file --user --source-dir --python; do
        run "$SCRIPT" "$flag"

        assert_status 1
        assert_output_contains "$flag needs a value"
    done
}

@test "name, model and port are required" {
    run "$SCRIPT" --model /x --port 1
    assert_status 1
    assert_output_contains "--name is required"

    run "$SCRIPT" --name a --port 1
    assert_status 1
    assert_output_contains "--model is required"

    run "$SCRIPT" --name a --model /x
    assert_status 1
    assert_output_contains "--port is required"
}

@test "a name outside [a-z0-9-] is refused" {
    local bad
    for bad in Upper under_score "with space" -lead trail- 'a;b'; do
        run "$SCRIPT" "${COMMON[@]}" --name "$bad" --print-unit

        assert_status 1
        assert_output_contains "--name must be"
    done
    run "$SCRIPT" "${COMMON[@]}" --name rfdetr-2 --print-unit
    assert_status 0
}

@test "a port outside 1-65535 or not a number is refused" {
    local bad
    for bad in 0 65536 abc -1 80.5 123456; do
        run "$SCRIPT" "${COMMON[@]}" --port "$bad" --print-unit

        assert_status 1
        assert_output_contains "--port must be"
    done
}

@test "an unknown arch and bad detr settings are refused" {
    run "$SCRIPT" "${COMMON[@]}" --arch onnx --print-unit
    assert_status 1
    assert_output_contains "--arch must be yolox or detr"

    run "$SCRIPT" "${COMMON[@]}" --arch detr --detr-activation relu --print-unit
    assert_status 1
    assert_output_contains "--detr-activation"

    run "$SCRIPT" "${COMMON[@]}" --arch yolox --detr-activation sigmoid --print-unit
    assert_status 1
    assert_output_contains "only apply with --arch detr"
}

@test "paths that would be mangled inside a unit are refused" {
    run "$SCRIPT" "${COMMON[@]}" --model "/srv/with space/m.onnx" --print-unit
    assert_status 1
    assert_output_contains "unsafe in a systemd unit"

    run "$SCRIPT" "${COMMON[@]}" --model 'relative.onnx' --print-unit
    assert_status 1
    assert_output_contains "absolute path"
}

@test "without root it refuses before installing anything" {
    if ((EUID == 0)); then skip "as root this would be a real run"; fi

    run "$SCRIPT" "${COMMON[@]}"
    assert_status 1
    assert_output_contains "run me as root"
    assert_output_not_contains "installed:"
}

@test "a missing model is refused" {
    TAPO_PROVISION_EUID=0 run "$SCRIPT" "${COMMON[@]}" --model "$TEST_TMP/models/none.onnx" --dry-run

    assert_status 1
    assert_output_contains "no model at"
}

@test "a token file that is not 0600 is refused, and no token reaches the output" {
    printf 'SECRETVALUE123\n' >"$TEST_TMP/tok"
    chmod 644 "$TEST_TMP/tok"

    TAPO_PROVISION_EUID=0 run "$SCRIPT" "${COMMON[@]}" --user "$(id -un)" \
        --token-file "$TEST_TMP/tok" --dry-run

    assert_status 1
    assert_output_contains "must be mode 0600"
    assert_output_not_contains "SECRETVALUE123"
}

@test "the unit carries only the token path, never the token" {
    printf 'SECRETVALUE123\n' >"$TEST_TMP/tok"
    chmod 600 "$TEST_TMP/tok"

    run "$SCRIPT" "${COMMON[@]}" --token-file "$TEST_TMP/tok" --print-unit

    assert_status 0
    assert_output_contains "Environment=TAPO_SCORER_TOKEN_FILE=$TEST_TMP/tok"
    assert_output_not_contains "SECRETVALUE123"
}

@test "the RF-DETR unit renders the template with every detector setting" {
    run "$SCRIPT" "${COMMON[@]}" --arch detr --input-size 560 --detr-activation sigmoid \
        --detr-person-id 0 --detr-normalize imagenet --print-unit

    assert_status 0
    assert_output_contains "Description=tapo-monitor shadow scoring service rfdetr"
    assert_output_contains "User=tester"
    assert_output_contains "WorkingDirectory=$TEST_TMP/src"
    assert_output_contains "Environment=TAPO_SCORER_ARCH=detr"
    assert_output_contains "Environment=TAPO_SCORER_INPUT_SIZE=560"
    assert_output_contains "Environment=TAPO_SCORER_DETR_ACTIVATION=sigmoid"
    assert_output_contains "Environment=TAPO_SCORER_DETR_PERSON_ID=0"
    assert_output_contains "Environment=TAPO_SCORER_DETR_NORMALIZE=imagenet"
    assert_output_contains "ExecStart=$TEST_TMP/python -m tapo_monitor.scorer_service --model $TEST_TMP/models/m.onnx --port 8767 --input-size 560"
    assert_output_not_contains "@SHADOW_"
}

@test "the rendered unit keeps every placeholder-free template line verbatim" {
    run "$SCRIPT" "${COMMON[@]}" --input-size 960 --print-unit
    assert_status 0
    printf '%s\n' "$output" >"$TEST_TMP/actual"

    local line
    while IFS= read -r line; do
        [[ "$line" == *@SHADOW_* ]] && continue
        grep -qxF -- "$line" "$TEST_TMP/actual" || {
            printf 'template line missing from the render: %s\n' "$line"
            return 1
        }
    done <"$REPO_ROOT/systemd/tapo-scorer-shadow.service.in"
    assert_output_contains "Environment=TAPO_SCORER_ARCH=yolox"
    assert_output_not_contains "DETR_"
    assert_output_contains "--input-size 960"
}

@test "--dry-run on an empty unit directory lists the change and writes nothing" {
    TAPO_PROVISION_EUID=0 run "$SCRIPT" "${COMMON[@]}" --dry-run

    assert_status 0
    assert_output_contains "would install: $TEST_TMP/units/tapo-scorer-shadow-rfdetr.service"
    assert_output_contains "changed=1"
    [[ -z "$(ls -A "$TEST_TMP/units")" ]]
}

@test "--dry-run against an installed identical unit says changed=0" {
    "$SCRIPT" "${COMMON[@]}" --print-unit >"$TEST_TMP/units/tapo-scorer-shadow-rfdetr.service"

    TAPO_PROVISION_EUID=0 run "$SCRIPT" "${COMMON[@]}" --dry-run

    assert_status 0
    assert_output_contains "changed=0"
    assert_output_not_contains "would install"
}

@test "--dry-run shows a diff when a setting changed" {
    "$SCRIPT" "${COMMON[@]}" --print-unit >"$TEST_TMP/units/tapo-scorer-shadow-rfdetr.service"

    TAPO_PROVISION_EUID=0 run "$SCRIPT" "${COMMON[@]}" --port 8768 --dry-run

    assert_status 0
    assert_output_contains "changed=1"
    assert_output_contains "--port 8768"
}
