"""Pure helpers for camera reliability and bounded self-healing.

The helpers in this module deliberately do not open camera sessions or perform writes.
They normalize operator policy, aggregate latency observations, and inspect the local
recorder tree. Camera mutations remain in the daemon's existing firmware-safe control
path, where this policy is consulted before an allow-listed repair is attempted.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Mapping

REPAIR_NAMES = frozenset({"person_detection", "vehicle_detection", "smarttrack"})
DEFAULT_REPAIRS = ("person_detection", "vehicle_detection", "smarttrack")
SEGMENT_SUFFIXES = (".mkv", ".mp4", ".ts")


def normalize_repairs(values: Iterable[str] | None) -> tuple[str, ...]:
    """Validate and deterministically normalize the configured repair allow-list."""
    if values is None:
        values = DEFAULT_REPAIRS
    if isinstance(values, (str, bytes)):
        raise ValueError("reliability.allowed_repairs must be a list")
    repairs = tuple(values)
    bad = [value for value in repairs if value not in REPAIR_NAMES]
    if bad:
        allowed = ", ".join(sorted(REPAIR_NAMES))
        raise ValueError(
            f"reliability.allowed_repairs has invalid {bad}; allowed: [{allowed}]"
        )
    return tuple(dict.fromkeys(repairs))


def observe_latency(bucket: dict, operation: str, seconds: float) -> None:
    """Accumulate bounded aggregate timing for one operation.

    Only count/total/max are retained; no camera media, URLs or event payloads enter the
    metric. Invalid timings are ignored because telemetry must never affect alert delivery.
    """
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return
    if not math.isfinite(seconds) or seconds < 0:
        return
    item = bucket.setdefault(str(operation), {"count": 0, "total_s": 0.0, "max_s": 0.0})
    item["count"] += 1
    item["total_s"] += seconds
    item["max_s"] = max(item["max_s"], seconds)


def latency_snapshot(bucket: Mapping) -> dict:
    """Return JSON-safe latency aggregates with rounded floating-point values."""
    result = {}
    for operation in sorted(bucket):
        item = bucket[operation]
        count = int(item.get("count", 0))
        total = float(item.get("total_s", 0.0))
        maximum = float(item.get("max_s", 0.0))
        result[str(operation)] = {
            "count": count,
            "total_s": round(total, 3),
            "max_s": round(maximum, 3),
            "avg_s": round(total / count, 3) if count else 0.0,
        }
    return result


def recorder_health(root: str | None, host: str, *, now: float,
                    max_age: float = 300.0, segment_seconds: float = 900.0,
                    lister=None, parse_start=None) -> dict:
    """Inspect freshness and continuity of one local recorder tree.

    ``lister`` and ``parse_start`` are injectable for deterministic tests. The returned
    structure contains no absolute path, only health facts safe for twin state.
    """
    root = (root or "").strip()
    if not root:
        return {"status": "unknown", "reason": "recording_root_unset"}
    camera_dir = os.path.join(root, host)
    if not os.path.isdir(camera_dir):
        return {"status": "unknown", "reason": "camera_recording_dir_missing"}
    if lister is None:
        lister = _list_recordings
    if parse_start is None:
        from .recclip import parse_segment_start

        parse_start = parse_segment_start
    try:
        paths = list(lister(camera_dir))
    except OSError:
        return {"status": "degraded", "reason": "recording_tree_unreadable"}
    entries = []
    for path in paths:
        try:
            start = float(parse_start(path))
            mtime = float(os.path.getmtime(path))
        except (OSError, TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(mtime):
            entries.append((start, mtime))
    if not entries:
        return {"status": "unknown", "reason": "no_recording_segments"}
    entries.sort()
    newest_mtime = max(mtime for _start, mtime in entries)
    age = max(0.0, float(now) - newest_mtime)
    gaps = [right[0] - left[0] for left, right in zip(entries, entries[1:], strict=False)]
    max_gap = max(gaps, default=0.0)
    stale = age > max_age
    gap = max_gap > segment_seconds * 1.5
    status = "degraded" if stale or gap else "ok"
    reason = "stale_output" if stale else "recording_gap" if gap else "continuous"
    return {
        "status": status,
        "reason": reason,
        "segments": len(entries),
        "latest_age_s": round(age, 3),
        "max_gap_s": round(max_gap, 3),
    }


def _list_recordings(camera_dir: str):
    for dirpath, _dirnames, filenames in os.walk(camera_dir):
        for name in filenames:
            if name.lower().endswith(SEGMENT_SUFFIXES):
                yield os.path.join(dirpath, name)
