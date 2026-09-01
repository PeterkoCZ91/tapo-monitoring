#!/usr/bin/env bash
# Build (or repair) the shared scorer host from this checkout. Run it ON that host, as
# root, from a clone of this repository:
#
#   sudo tools/provision_scorer.sh --user tapo
#
# The scorer is the one machine every camera depends on and the only one that could not
# be rebuilt from the repository: its unit was hand-written, so "how was it started" had
# no answer outside that one disk. Why a renderer instead of a unit file to copy: systemd
# expands ${VAR} in an ExecStart *argument* but never in the executable itself, so the
# venv interpreter and the working directory can only ever be literal text in the unit.
# Copy-and-hand-edit is exactly how the running unit drifted away from the repo's copy.
#
# Safe to re-run on the live scorer: every file is rendered to a temporary path first and
# installed only when it differs, and the service is restarted only when something that
# affects it actually changed. --dry-run shows the diffs and touches nothing.
#
# What it installs:
#   /etc/systemd/system/<unit>                     rendered from systemd/tapo-scorer.service.in
#   /etc/tapo-monitor/scorer.env                   model/port/metrics settings, created once
#                                                  and never overwritten — it holds the
#                                                  host's own paths
#   /usr/local/bin/pi_notify.sh                    the fleet's Telegram sender
#   /etc/systemd/system/pi-failure-notify@.service OnFailure handler for the unit above
#
# Telegram credentials are deliberately not an option: argv is world-readable in /proc.
# Put them in /etc/tapo-monitor/notify.env (mode 600) as TELEGRAM_TOKEN=/TELEGRAM_CHAT_ID=,
# or point TAPO_ENV_FILE= there at a file that already has them.
set -euo pipefail

die() { echo "provision_scorer: $*" >&2; exit 1; }
note() { echo "provision_scorer: $*"; }

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

user="${SUDO_USER:-}" home="" python_bin="" model="" metrics_file=""
port="8766" input_size="640" unit="tapo-scorer.service" bootstrap=0 dry_run=0
while (($#)); do
    case "$1" in
        --user)         user="${2:?--user needs a value}"; shift 2 ;;
        --home)         home="${2:?--home needs a value}"; shift 2 ;;
        --python)       python_bin="${2:?--python needs a value}"; shift 2 ;;
        --model)        model="${2:?--model needs a value}"; shift 2 ;;
        --port)         port="${2:?--port needs a value}"; shift 2 ;;
        --input-size)   input_size="${2:?--input-size needs a value}"; shift 2 ;;
        --metrics-file) metrics_file="${2:?--metrics-file needs a value}"; shift 2 ;;
        --unit)         unit="${2:?--unit needs a value}"; shift 2 ;;
        --bootstrap)    bootstrap=1; shift ;;
        --dry-run)      dry_run=1; shift ;;
        # The header block, however long it grows: print from line 2 until the first line
        # that is not a comment. A fixed range printed `set -euo pipefail` as help text
        # and would have kept sliding every time a line was added above it.
        -h|--help)      sed -n '2,${/^#/!q;p;}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)              die "unknown argument $1" ;;
    esac
done

((EUID == 0)) || die "run me as root — the unit, /etc/tapo-monitor and /usr/local/bin need it"
[[ -n "$user" ]] || die "no service user: pass --user (SUDO_USER is unset when root logs in directly)"
command -v runuser >/dev/null || die "runuser is missing; it is how this script drops to $user"
user_home="$(getent passwd "$user" | cut -d: -f6)"
[[ -n "$user_home" ]] || die "unknown user $user"

home="${home:-$user_home/tapo-scorer}"
python_bin="${python_bin:-$home/env/bin/python}"
model="${model:-$home/yolox_m.onnx}"
metrics_file="${metrics_file:-$home/metrics/scorer.jsonl}"

run_as() { runuser -u "$user" -- "$@"; }

changed=0 restart_needed=0

install_file() {  # <rendered-source> <destination> <mode> [service]
    local src="$1" dst="$2" mode="$3" scope="${4:-}"
    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        note "unchanged: $dst"
        return 0
    fi
    if ((dry_run)); then
        note "would install: $dst (mode $mode)"
        if [[ -f "$dst" ]]; then
            diff -u "$dst" "$src" | sed 's/^/    /' || true
        fi
    else
        install -o root -g root -m "$mode" "$src" "$dst"
        note "installed: $dst"
    fi
    changed=1
    [[ "$scope" == "service" ]] && restart_needed=1
    return 0
}

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

# ── layout ────────────────────────────────────────────────────────────────────────────
if ((dry_run)); then
    note "dry run: no directory, file, or unit is touched"
else
    install -d -o root -g root -m 0755 /etc/tapo-monitor
    # The metrics journal is written by the service user, so its directory must belong to
    # them — a root-owned directory here is a crash the first time metrics persist.
    install -d -o "$user" -g "$user" -m 0755 "$home" "$(dirname "$metrics_file")"
fi

# ── the venv, optionally built here ───────────────────────────────────────────────────
if ((bootstrap)) && ((! dry_run)); then
    venv_dir="${python_bin%/bin/python}"
    if [[ ! -x "$python_bin" ]]; then
        note "creating venv $venv_dir"
        run_as python3 -m venv "$venv_dir"
    fi
    note "installing ${repo}[scorer] into $venv_dir"
    run_as "$python_bin" -m pip install --quiet --upgrade pip
    run_as "$python_bin" -m pip install --quiet "${repo}[scorer]"
fi

[[ -x "$python_bin" ]] \
    || die "no interpreter at $python_bin — pass --python, or re-run with --bootstrap to build the venv"
run_as "$python_bin" -c 'import numpy, onnxruntime' >/dev/null 2>&1 \
    || die "$python_bin cannot import numpy/onnxruntime — re-run with --bootstrap"
# From $home, because that is the working directory the unit will use: a host that keeps
# the package as a copy there passes here, and so does one that pip-installed it.
(cd "$home" && run_as "$python_bin" -c 'import tapo_monitor.scorer_service' >/dev/null 2>&1) \
    || die "$python_bin cannot import tapo_monitor.scorer_service from $home — re-run with --bootstrap"

# ── settings the unit reads ───────────────────────────────────────────────────────────
env_path="/etc/tapo-monitor/scorer.env"
env_value() { sed -n "s/^$1=//p" "$env_path" | tail -1; }

if [[ -f "$env_path" ]]; then
    note "kept existing $env_path (host-owned; hand-edit it to change model, port or retention)"
    for required in TAPO_SCORER_MODEL TAPO_SCORER_PORT TAPO_SCORER_INPUT_SIZE; do
        [[ -n "$(env_value "$required")" ]] \
            || die "$env_path defines no $required — the unit would exit before the model loads"
    done
    model="$(env_value TAPO_SCORER_MODEL)"
    port="$(env_value TAPO_SCORER_PORT)"
elif ((dry_run)); then
    note "would create: $env_path (model=$model port=$port input-size=$input_size)"
else
    cat >"$stage/scorer.env" <<ENV
# Written by tools/provision_scorer.sh. Host-owned from here on: the script never
# overwrites this file, so local retention or path changes survive re-provisioning.
TAPO_SCORER_MODEL=$model
TAPO_SCORER_PORT=$port
TAPO_SCORER_INPUT_SIZE=$input_size

# Aggregate-only metrics; the directory must stay writable by $user. These are read from
# this file by the process itself and never appear in ExecStart: an undefined \${VAR}
# there expands to nothing and argparse would exit before the model loads — under
# Restart=always, an invisible crash loop caused by an observability setting.
TAPO_SCORER_METRICS_FILE=$metrics_file
TAPO_SCORER_METRICS_PERSIST_SECONDS=60
TAPO_SCORER_METRICS_RETENTION_DAYS=7
TAPO_SCORER_METRICS_RETENTION_FILES=8
TAPO_SCORER_METRICS_MAX_JOURNAL_BYTES=33554432
ENV
    install -o root -g root -m 0644 "$stage/scorer.env" "$env_path"
    note "created: $env_path"
    changed=1 restart_needed=1
fi

[[ -f "$model" ]] || die "no model at $model — put the YOLOX .onnx weights there (or pass --model)"
run_as test -r "$model" || die "$model is not readable by $user"

# ── the unit, rendered from the repository's template ─────────────────────────────────
template="$repo/systemd/tapo-scorer.service.in"
[[ -f "$template" ]] || die "missing $template — run this from a checkout of the repo"
sed -e "s|@SCORER_USER@|$user|g" \
    -e "s|@SCORER_HOME@|$home|g" \
    -e "s|@SCORER_PYTHON@|$python_bin|g" \
    "$template" >"$stage/$unit"
install_file "$stage/$unit" "/etc/systemd/system/$unit" 0644 service

# ── failure notification: the scorer's crash loop is the one that cannot report itself ─
install_file "$repo/pi_notify.sh" /usr/local/bin/pi_notify.sh 0755
install_file "$repo/systemd/pi-failure-notify@.service" \
    /etc/systemd/system/pi-failure-notify@.service 0644

notify_env="/etc/tapo-monitor/notify.env"
if [[ -f "$notify_env" ]] && grep -qE '^(TELEGRAM_TOKEN|TAPO_ENV_FILE)=' "$notify_env"; then
    note "failure notification: credentials present in $notify_env"
else
    note "WARNING: $notify_env has no TELEGRAM_TOKEN= (or TAPO_ENV_FILE= pointing at one)."
    note "         OnFailure= is wired, but the alert cannot be sent. Create it with mode 600."
fi

# ── activate ──────────────────────────────────────────────────────────────────────────
if ((dry_run)); then
    note "dry run finished; changed=$changed restart_needed=$restart_needed"
    exit 0
fi

if ((changed)); then
    systemctl daemon-reload
fi
systemctl enable "$unit" >/dev/null
if ((restart_needed)) || ! systemctl is-active --quiet "$unit"; then
    note "restarting $unit"
    systemctl restart "$unit"
else
    note "$unit already runs the installed configuration — not restarting"
fi

# ── verify the way a client sees it ───────────────────────────────────────────────────
base_url="http://127.0.0.1:$port"
deadline=$((SECONDS + 60))
until curl --fail --silent --max-time 3 "$base_url/health" >/dev/null 2>&1; do
    ((SECONDS < deadline)) || die "no answer from $base_url/health within 60s — journalctl -u $unit"
    sleep 2
done
"$repo/tools/check_scorer_rollout.sh" "$base_url"
note "done — $unit is active on port $port (model $model)"
