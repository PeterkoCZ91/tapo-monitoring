#!/usr/bin/env bash
# Build (or update) one shadow scorer service on this machine, typically an LXC container:
#
#   sudo tools/provision_shadow_scorer.sh --name rfdetr --model /srv/models/rfdetr.onnx \
#        --port 8767 --arch detr --user tapo --python /srv/tapo/.venv/bin/python
#
# A shadow scorer is a second tapo_monitor.scorer_service that hosts query for comparison
# only (see "Provision a shadow scorer" in docs/operations.md); it never changes the answer
# a host acts on. The unit is rendered from systemd/tapo-scorer-shadow.service.in, the same
# way tools/provision_scorer.sh renders the primary one, and is called
# tapo-scorer-shadow-<name>.service.
#
# Options:
#   --name NAME        [a-z0-9-], 1-40 characters; names the unit (required)
#   --model PATH       ONNX weights, must exist (required)
#   --port PORT        TCP port 1-65535 (required)
#   --arch yolox|detr  detector family, default yolox
#   --input-size N     model input side; default 640 for detr, 416 for yolox
#   --detr-activation softmax|sigmoid   DETR logits head (RF-DETR: sigmoid)
#   --detr-person-id 0|1                DETR person class index
#   --detr-normalize imagenet|none      DETR input normalisation
#   --token-file PATH  shared-token file (mode 0600, owned by the service user); only the
#                      path goes into the unit, the token is never read, printed or passed
#                      on a command line
#   --user USER        service user (default: SUDO_USER)
#   --source-dir DIR   checkout or copy holding tapo_monitor (default: this checkout)
#   --python PATH      interpreter with numpy and onnxruntime (default: DIR/.venv/bin/python)
#   --dry-run          show what would change, touch nothing; prints changed=0 when the
#                      installed unit already matches
#   --print-unit       print the rendered unit and exit (no root needed)
#
# Idempotent: the unit is rendered to a temporary file and installed only when it differs;
# the service is restarted only after a change (or when it is not running). Success means
# GET /health answered HTTP 200 within the time limit; otherwise the script exits non-zero
# and prints the last journal lines of the unit.
set -euo pipefail

die() { echo "provision_shadow_scorer: $*" >&2; exit 1; }
note() { echo "provision_shadow_scorer: $*"; }

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

name="" model="" port="" arch="yolox" input_size="" token_file=""
user="${SUDO_USER:-}" source_dir="$repo" python_bin=""
detr_activation="" detr_person_id="" detr_normalize=""
dry_run=0 print_unit=0
while (($#)); do
    case "$1" in
        --name)            name="${2:?--name needs a value}"; shift 2 ;;
        --model)           model="${2:?--model needs a value}"; shift 2 ;;
        --port)            port="${2:?--port needs a value}"; shift 2 ;;
        --arch)            arch="${2:?--arch needs a value}"; shift 2 ;;
        --input-size)      input_size="${2:?--input-size needs a value}"; shift 2 ;;
        --detr-activation) detr_activation="${2:?--detr-activation needs a value}"; shift 2 ;;
        --detr-person-id)  detr_person_id="${2:?--detr-person-id needs a value}"; shift 2 ;;
        --detr-normalize)  detr_normalize="${2:?--detr-normalize needs a value}"; shift 2 ;;
        --token-file)      token_file="${2:?--token-file needs a value}"; shift 2 ;;
        --user)            user="${2:?--user needs a value}"; shift 2 ;;
        --source-dir)      source_dir="${2:?--source-dir needs a value}"; shift 2 ;;
        --python)          python_bin="${2:?--python needs a value}"; shift 2 ;;
        --dry-run)         dry_run=1; shift ;;
        --print-unit)      print_unit=1; shift ;;
        -h|--help)         sed -n '2,${/^#/!q;p;}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)                 die "unknown argument $1" ;;
    esac
done

# ── validation: everything that can be refused is refused before anything is touched ──
[[ -n "$name" ]] || die "--name is required"
[[ "$name" =~ ^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$ ]] \
    || die "--name must be 1-40 characters of [a-z0-9-], not starting or ending with '-'"
[[ -n "$model" ]] || die "--model is required"
[[ -n "$port" ]] || die "--port is required"
[[ "$port" =~ ^[0-9]{1,5}$ ]] || die "--port must be a number from 1 to 65535"
((10#$port >= 1 && 10#$port <= 65535)) || die "--port must be a number from 1 to 65535"
port=$((10#$port))
case "$arch" in yolox|detr) ;; *) die "--arch must be yolox or detr" ;; esac
if [[ -z "$input_size" ]]; then
    if [[ "$arch" == detr ]]; then input_size=640; else input_size=416; fi
fi
[[ "$input_size" =~ ^[0-9]{1,5}$ ]] || die "--input-size must be a whole number of at least 32"
((10#$input_size >= 32)) || die "--input-size must be a whole number of at least 32"
input_size=$((10#$input_size))
if [[ "$arch" != detr ]]; then
    [[ -z "$detr_activation$detr_person_id$detr_normalize" ]] \
        || die "--detr-* options only apply with --arch detr"
fi
case "$detr_activation" in ""|softmax|sigmoid) ;; *) die "--detr-activation must be softmax or sigmoid" ;; esac
case "$detr_person_id" in ""|0|1) ;; *) die "--detr-person-id must be 0 or 1" ;; esac
case "$detr_normalize" in ""|imagenet|none) ;; *) die "--detr-normalize must be imagenet or none" ;; esac

# Paths end up as literal text in a unit file: no whitespace, quotes, '%' or '$', which
# systemd would split or expand.
safe_path() {  # <flag> <value>
    [[ "$2" == /* ]] || die "$1 must be an absolute path"
    [[ "$2" =~ ^[A-Za-z0-9._/+@:,=-]+$ ]] \
        || die "$1 contains characters that are unsafe in a systemd unit (use [A-Za-z0-9._/+@:,=-])"
}
safe_path --model "$model"
safe_path --source-dir "$source_dir"
python_bin="${python_bin:-$source_dir/.venv/bin/python}"
safe_path --python "$python_bin"
[[ -z "$token_file" ]] || safe_path --token-file "$token_file"
[[ -n "$user" ]] || die "no service user: pass --user (SUDO_USER is unset when root logs in directly)"
[[ "$user" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "--user $user is not a valid user name"

unit="tapo-scorer-shadow-$name.service"
template="$repo/systemd/tapo-scorer-shadow.service.in"
[[ -f "$template" ]] || die "missing $template — run this from a checkout of the repo"

# ── render ────────────────────────────────────────────────────────────────────────────
render() {
    local env_lines text
    env_lines="Environment=TAPO_SCORER_ARCH=$arch"
    env_lines+=$'\n'"Environment=TAPO_SCORER_INPUT_SIZE=$input_size"
    [[ -z "$detr_activation" ]] || env_lines+=$'\n'"Environment=TAPO_SCORER_DETR_ACTIVATION=$detr_activation"
    [[ -z "$detr_person_id" ]] || env_lines+=$'\n'"Environment=TAPO_SCORER_DETR_PERSON_ID=$detr_person_id"
    [[ -z "$detr_normalize" ]] || env_lines+=$'\n'"Environment=TAPO_SCORER_DETR_NORMALIZE=$detr_normalize"
    # Only the path: the service reads the file itself and refuses to start unless it is 0600.
    [[ -z "$token_file" ]] || env_lines+=$'\n'"Environment=TAPO_SCORER_TOKEN_FILE=$token_file"
    text="$(cat "$template"; echo x)"
    text="${text%x}"
    text="${text//@SHADOW_NAME@/$name}"
    text="${text//@SHADOW_USER@/$user}"
    text="${text//@SHADOW_SOURCE_DIR@/$source_dir}"
    text="${text//@SHADOW_PYTHON@/$python_bin}"
    text="${text//@SHADOW_MODEL@/$model}"
    text="${text//@SHADOW_PORT@/$port}"
    text="${text//@SHADOW_INPUT_SIZE@/$input_size}"
    text="${text//@SHADOW_ENVIRONMENT@/$env_lines}"
    printf '%s' "$text"
}

if ((print_unit)); then
    render
    exit 0
fi

# TAPO_PROVISION_UNIT_DIR / TAPO_PROVISION_EUID exist so the tests can exercise --dry-run
# against a scratch directory; a dry run never writes, so neither can cause damage.
unit_dir="${TAPO_PROVISION_UNIT_DIR:-/etc/systemd/system}"
effective_uid="$EUID"
if ((dry_run)); then
    effective_uid="${TAPO_PROVISION_EUID:-$EUID}"
fi
((effective_uid == 0)) || die "run me as root — the unit directory and systemctl need it"

[[ -f "$model" ]] || die "no model at $model"
[[ -x "$python_bin" ]] || die "no interpreter at $python_bin — pass --python (needs numpy and onnxruntime)"
[[ -d "$source_dir/tapo_monitor" ]] || die "$source_dir/tapo_monitor not found — pass --source-dir"
if [[ -n "$token_file" ]]; then
    [[ -f "$token_file" ]] || die "no token file at $token_file"
    token_mode="$(stat -c '%a' "$token_file")"
    [[ "$token_mode" == 600 || "$token_mode" == 400 ]] \
        || die "$token_file must be mode 0600 (is $token_mode) — the service refuses to start otherwise"
    [[ "$(stat -c '%U' "$token_file")" == "$user" ]] \
        || die "$token_file must be owned by $user"
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
render >"$stage/$unit"
dest="$unit_dir/$unit"

changed=0
if [[ -f "$dest" ]] && cmp -s "$stage/$unit" "$dest"; then
    note "unchanged: $dest"
else
    changed=1
    if ((dry_run)); then
        note "would install: $dest"
        if [[ -f "$dest" ]]; then
            diff -u "$dest" "$stage/$unit" | sed 's/^/    /' || true
        else
            sed 's/^/    + /' "$stage/$unit"
        fi
    fi
fi

if ((dry_run)); then
    note "dry run: nothing was touched"
    note "changed=$changed"
    exit 0
fi

# Real run only: these need the service user and a working Python environment.
command -v runuser >/dev/null || die "runuser is missing; it is how this script drops to $user"
runuser -u "$user" -- test -r "$model" || die "$model is not readable by $user"
runuser -u "$user" -- "$python_bin" -c 'import numpy, onnxruntime' >/dev/null 2>&1 \
    || die "$python_bin cannot import numpy/onnxruntime"
(cd "$source_dir" && runuser -u "$user" -- "$python_bin" -c 'import tapo_monitor.scorer_service' \
    >/dev/null 2>&1) || die "$python_bin cannot import tapo_monitor.scorer_service from $source_dir"

if ((changed)); then
    install -o root -g root -m 0644 "$stage/$unit" "$dest"
    note "installed: $dest"
    systemctl daemon-reload
fi
systemctl enable "$unit" >/dev/null
if ((changed)) || ! systemctl is-active --quiet "$unit"; then
    note "restarting $unit"
    systemctl restart "$unit"
else
    note "$unit already runs the installed configuration — not restarting"
fi

# ── verify the way a client sees it: the HTTP status, not the pipeline's exit code ────
limit="${TAPO_PROVISION_HEALTH_TIMEOUT:-90}"
url="http://127.0.0.1:$port/health"
deadline=$((SECONDS + limit))
code=000
while :; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" || true)"
    [[ "$code" == 200 ]] && break
    if ((SECONDS >= deadline)); then
        echo "provision_shadow_scorer: $url answered HTTP $code, not 200, within ${limit}s" >&2
        echo "provision_shadow_scorer: last journal lines of $unit:" >&2
        # The service never logs the token (only the setting name); the filter is a belt
        # and braces guard for anything that looks like a bearer value.
        journalctl -u "$unit" -n 30 --no-pager 2>&1 \
            | sed -E 's/([Bb]earer|[Tt]oken)([ =:]+)[^ ]+/\1\2<redacted>/g' >&2 || true
        exit 1
    fi
    sleep 2
done
note "done — $unit is active on port $port (HTTP $code from /health, arch=$arch, input-size=$input_size)"
