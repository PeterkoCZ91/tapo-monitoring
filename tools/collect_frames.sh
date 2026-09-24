#!/usr/bin/env bash
# Pull every host's sent-log and review-log into one local dataset, never deleting.
#
# Usage: tools/collect_frames.sh DATASET_DIR SSH_HOST [SSH_HOST...]
#
# The hosts prune their frame logs after TAPO_*_LOG_RETENTION_DAYS; the dataset keeps
# everything it has ever seen, so labelling (`tapo-monitor label DATASET_DIR`) and later
# training are not limited by a host's retention window. Frames are copied without
# --delete. Each host's index.jsonl is also pruned on the host, so it is not copied over
# the local one: new lines are merged into it instead, keeping entries whose frames the
# host has already forgotten.
#
# Layout: DATASET_DIR/<host>/{sent-log,review-log}/{*.jpg,index.jsonl}
# Remote paths default to ~/tapo-monitor/{sent-log,review-log}; override with
# TAPO_REMOTE_ROOT. A host that cannot be reached is reported and skipped, so one
# offline site never costs the others their night.
set -euo pipefail

if [ "$#" -lt 2 ]; then
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi

dataset="$1"
shift
remote_root="${TAPO_REMOTE_ROOT:-tapo-monitor}"
failed=0

merge_index() {
    # Append lines of $2 missing from $1 (exact-line match), keeping $1's order.
    local local_index="$1" fresh="$2" new_lines
    touch "$local_index"
    new_lines="$(mktemp)"
    # FILENAME, not NR == FNR: with an empty local index the second file would pass for
    # the first and every fresh line would be swallowed as already seen.
    awk -v known="$local_index" \
        'FILENAME == known { seen[$0] = 1; next } !($0 in seen) && NF { print; seen[$0] = 1 }' \
        "$local_index" "$fresh" > "$new_lines"
    cat "$new_lines" >> "$local_index"
    rm -f "$new_lines"
}

for host in "$@"; do
    for log in sent-log review-log; do
        dest="$dataset/$host/$log"
        mkdir -p "$dest"
        tmp_index="$(mktemp)"
        if ! rsync -a --timeout=120 --exclude index.jsonl "$host:$remote_root/$log/" "$dest/"; then
            echo "collect_frames: $host $log: not collected (host unreachable or no log)" >&2
            failed=1
        elif ! rsync -a --timeout=60 "$host:$remote_root/$log/index.jsonl" "$tmp_index" \
                2>/dev/null; then
            # A log that has not archived its first frame has no index yet: not a failure.
            echo "collect_frames: $host $log: no index yet"
        else
            touch "$dest/index.jsonl"
            before="$(wc -l < "$dest/index.jsonl")"
            merge_index "$dest/index.jsonl" "$tmp_index"
            after="$(wc -l < "$dest/index.jsonl")"
            echo "collect_frames: $host $log: +$((after - before)) index line(s)"
        fi
        rm -f "$tmp_index"
    done
done
exit "$failed"
