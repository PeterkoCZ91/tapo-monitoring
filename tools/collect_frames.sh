#!/usr/bin/env bash
# Pull every host's sent-log and review-log into one local dataset, never deleting.
#
# Usage: tools/collect_frames.sh [--dry-run] DATASET_DIR [OPTIONS] SOURCE [[OPTIONS] SOURCE...]
#
# A SOURCE is an SSH host alias, or NAME=DIR for a daemon running on this machine (DIR
# holds sent-log/ and review-log/ directly; NAME is the dataset subdirectory).
# Options apply to every SOURCE after them, until changed:
#   --env-file FILE   take TAPO_SENT_LOG_DIR / TAPO_REVIEW_LOG_DIR from FILE on the
#                     source; only those two values leave it ("" = back to the default)
#   --sent-dir DIR    explicit sent-log dir on the source, beats --env-file ("" resets)
#   --review-dir DIR  explicit review-log dir on the source (the same)
#   --rsh CMD         remote shell (rsync -e; default $RSYNC_RSH or ssh), host goes first
# --dry-run copies and merges nothing and reports what a real run would add.
#
# By default a source's logs are ~/tapo-monitor/{sent-log,review-log}, or
# $TAPO_REMOTE_ROOT/{sent-log,review-log}. Only *.jpg frames and index.jsonl are read.
# Frames are copied with --ignore-existing and never --delete. Each host's index.jsonl is
# pruned on the host, so it is not copied over the local one: new lines are merged into
# it instead, keeping entries whose frames the host has already forgotten. The dataset
# outlives the hosts' retention; `tapo-monitor label DATASET_DIR` reads it.
#
# Layout: DATASET_DIR/<host>/{sent-log,review-log}/{*.jpg,index.jsonl}
# A host that cannot be reached is reported and skipped (exit 1 at the end), so one
# offline site never costs the others their run.
set -euo pipefail

drop_partial_tail() {
    # An index copied while the monitor was appending can end in a half-written line
    # (no trailing newline). Merged as-is it would stay in the local index forever next
    # to its completed form, so cut it back to the last complete line.
    local f="$1"
    [[ -s "$f" ]] || return 0
    [[ "$(tail -c1 "$f" | od -An -c | tr -d ' ')" == '\n' ]] && return 0
    local keep
    keep="$(mktemp)"
    # Lines before the last one are complete; the last has no newline, so drop it.
    sed '$d' "$f" > "$keep"
    cat "$keep" > "$f"
    rm -f "$keep"
}

# Lines of $2 missing from $1 (exact-line match). FILENAME, not NR == FNR: with an empty
# local index the second file would pass for the first and every fresh line would be
# swallowed as already seen.
# shellcheck disable=SC2016  # awk fields, not shell expansions.
NEW_LINES_AWK='FILENAME == known { seen[$0] = 1; next } !($0 in seen) && NF { print; seen[$0] = 1 }'

merge_index() {
    # Append lines of $2 missing from $1, keeping $1's order.
    local local_index="$1" fresh="$2" new_lines
    touch "$local_index"
    new_lines="$(mktemp)"
    awk -v known="$local_index" "$NEW_LINES_AWK" "$local_index" "$fresh" > "$new_lines"
    cat "$new_lines" >> "$local_index"
    rm -f "$new_lines"
}

count_new_lines() {
    # How many lines merge_index would append, without touching $1.
    local known="$1" fresh="$2"
    [[ -f "$known" ]] || known=/dev/null
    awk -v known="$known" "$NEW_LINES_AWK" "$known" "$fresh" | wc -l
}

# rsync exit 24 = some source files vanished mid-transfer (the monitor prunes its logs
# while we copy): the rest arrived, so that is success. --safe-links drops symlinks that
# point outside the tree, so a hostile remote cannot make us copy or follow arbitrary paths.
rsync_ok() {
    local rc=0
    rsync "$@" || rc=$?
    [[ "$rc" -eq 0 || "$rc" -eq 24 ]]
}

usage() {
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

quote_remote_path() {
    # Quote a path for the remote shell, keeping a leading ~/ expandable.
    local path="$1"
    # shellcheck disable=SC2088  # a literal ~/ is meant: the remote shell expands it.
    if [[ "$path" == "~/"* ]]; then
        # shellcheck disable=SC2088
        printf '~/%q' "${path:2}"
    else
        printf '%q' "$path"
    fi
}

read_env_dir() {
    # Print the value of $3 in env file $2 on source $1 (empty when unset). The sed runs
    # on the source and prints only that one value: tokens in the file never leave it.
    local src_prefix="$1" file="$2" var="$3" expr value
    expr='s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}'"$var"'=//p'
    if [[ -z "$src_prefix" ]]; then
        value="$(sed -n "$expr" "$file")" || return 1
    else
        local -a rsh_words
        read -ra rsh_words <<<"$rsh"
        value="$("${rsh_words[@]}" "${src_prefix%:}" \
            "sed -n $(printf '%q' "$expr") $(quote_remote_path "$file")" </dev/null)" || return 1
    fi
    value="$(printf '%s\n' "$value" | tail -n1)"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    printf '%s' "$value"
}

collect_log() {
    # Copy one log of one source; adds its new frames and bytes to the host totals.
    local host="$1" src_prefix="$2" src_dir="$3" log="$4"
    local dest="$dataset/$host/$log" tmp_index listing frames bytes added
    local -a dry=()
    if [[ "$dry_run" -eq 1 ]]; then
        dry=(--dry-run)
    else
        mkdir -p "$dest"
    fi
    listing="$(mktemp)"
    # Only the frames: no lock files, digests or whatever else lives in the log dir. They
    # show people, so they stay owner-only here as on the host.
    if ! rsync_ok -rt --chmod=F600 --safe-links --timeout=120 --ignore-existing -e "$rsh" "${dry[@]}" \
            --include='*.jpg' --exclude='*' --out-format='%l %n' \
            "$src_prefix$src_dir/" "$dest/" >"$listing"; then
        echo "collect_frames: $host $log: not collected (host unreachable or no log)" >&2
        rm -f "$listing"
        failed=1
        return 0
    fi
    read -r frames bytes < <(awk '$NF ~ /\.jpg$/ { n++; b += $1 } END { print n + 0, b + 0 }' \
        "$listing")
    rm -f "$listing"
    tmp_index="$(mktemp)"
    if ! rsync_ok -t --timeout=60 -e "$rsh" "$src_prefix$src_dir/index.jsonl" "$tmp_index" \
            2>/dev/null; then
        # A log that has not archived its first frame has no index yet: not a failure.
        echo "collect_frames: $host $log: $frames new frame(s), $bytes bytes, no index yet"
    else
        drop_partial_tail "$tmp_index"
        if [[ "$dry_run" -eq 1 ]]; then
            added="$(count_new_lines "$dest/index.jsonl" "$tmp_index")"
        else
            local before after
            touch "$dest/index.jsonl"
            before="$(wc -l < "$dest/index.jsonl")"
            merge_index "$dest/index.jsonl" "$tmp_index"
            after="$(wc -l < "$dest/index.jsonl")"
            added=$((after - before))
        fi
        echo "collect_frames: $host $log: $frames new frame(s), $bytes bytes," \
            "+$((added)) index line(s)"
    fi
    rm -f "$tmp_index"
    host_frames=$((host_frames + frames))
    host_bytes=$((host_bytes + bytes))
}

dry_run=0
args=()
for arg in "$@"; do
    if [[ "$arg" == --dry-run ]]; then dry_run=1; else args+=("$arg"); fi
done
[[ "${#args[@]}" -ge 2 ]] || usage
dataset="${args[0]}"
remote_root="${TAPO_REMOTE_ROOT:-tapo-monitor}"
# The remote shell defaults to RSYNC_RSH, as plain rsync would use it, so --env-file
# reaches the host the same way the copy does.
rsh="${RSYNC_RSH:-ssh}"
env_file=""
sent_dir=""
review_dir=""
failed=0
sources=0

i=1
while [[ "$i" -lt "${#args[@]}" ]]; do
    arg="${args[$i]}"
    case "$arg" in
        --env-file|--sent-dir|--review-dir|--rsh)
            [[ $((i + 1)) -lt "${#args[@]}" ]] || usage
            value="${args[$((i + 1))]}"
            case "$arg" in
                --env-file) env_file="$value" ;;
                --sent-dir) sent_dir="$value" ;;
                --review-dir) review_dir="$value" ;;
                --rsh) [[ -n "$value" ]] || usage; rsh="$value" ;;
            esac
            i=$((i + 2))
            continue
            ;;
        -*) echo "collect_frames: unknown option $arg" >&2; usage ;;
    esac
    i=$((i + 1))
    sources=$((sources + 1))
    if [[ "$arg" == *=* ]]; then
        host="${arg%%=*}"
        src_root="${arg#*=}"
        src_prefix=""
    else
        host="$arg"
        src_root="$remote_root"
        src_prefix="$host:"
    fi
    if [[ -z "$host" || "$host" == */* || "$host" == .* ]]; then
        echo "collect_frames: $arg: not a usable dataset name" >&2
        failed=1
        continue
    fi
    host_sent="$src_root/sent-log"
    host_review="$src_root/review-log"
    if [[ -n "$env_file" ]]; then
        if ! host_sent="$(read_env_dir "$src_prefix" "$env_file" TAPO_SENT_LOG_DIR)" \
                || ! host_review="$(read_env_dir "$src_prefix" "$env_file" TAPO_REVIEW_LOG_DIR)"
        then
            echo "collect_frames: $host: not collected (cannot read $env_file)" >&2
            failed=1
            continue
        fi
    fi
    [[ -n "$sent_dir" ]] && host_sent="$sent_dir"
    [[ -n "$review_dir" ]] && host_review="$review_dir"
    host_frames=0
    host_bytes=0
    for log in sent-log review-log; do
        if [[ "$log" == sent-log ]]; then src_dir="$host_sent"; else src_dir="$host_review"; fi
        if [[ -z "$src_dir" ]]; then
            echo "collect_frames: $host $log: not configured on the host, skipped"
            continue
        fi
        collect_log "$host" "$src_prefix" "${src_dir%/}" "$log"
    done
    mode=""
    [[ "$dry_run" -eq 1 ]] && mode=" (dry run, nothing copied)"
    echo "collect_frames: $host: $host_frames new frame(s), $host_bytes bytes$mode"
done
[[ "$sources" -gt 0 ]] || usage
exit "$failed"
