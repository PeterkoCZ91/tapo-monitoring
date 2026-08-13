"""Independent nightly audit of local recorder segments (Phase 3 shadow worker).

Scans a date's 15-minute recorder segments without any camera involvement, scores
motion-candidate frames through the shared scorer, records shadow observations in the
event ledger, and archives miss-candidate frames to the review log. Observation-only:
it never contacts a camera and never changes configuration. See
docs/superpowers/specs/2026-08-13-shadow-worker-design.md.
"""

import glob
import json
import logging
import os
import re
import subprocess
import time

from . import recclip, scorer, sentlog

log = logging.getLogger(__name__)

DEFAULT_SCENE = 0.04
DEFAULT_SEGMENT_CAP = 8
DEFAULT_BUDGET = 1500
DEFAULT_RATE = 1.5
DEFAULT_CLUSTER_GAP = 180.0
DEFAULT_MATCH_WINDOW = 120.0
SCORER_ABORT_AFTER = 3
SUMMARY_NAME = ".shadow-scan.json"
SUMMARY_MAX_AGE = 36 * 3600.0

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def resolve_date(arg, now=None):
    """Normalize the --date argument to a local YYYY-MM-DD string."""
    if arg in (None, "", "yesterday"):
        now = time.time() if now is None else now
        return time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    if not _DATE_RE.match(arg):
        raise ValueError(f"not a date: {arg!r}")
    return arg


def segments_for_date(base_dir, host, date_str, lister=None):
    """(mkv_path, seg_start) for every segment of the date, sorted by start."""
    lister = lister or (lambda d: sorted(glob.glob(os.path.join(d, "zaznam_*.mkv"))))
    day_dir = os.path.join(base_dir, host, date_str)
    found = []
    for hour in range(24):
        for path in lister(os.path.join(day_dir, f"{hour:02d}")):
            try:
                found.append((path, recclip.parse_segment_start(path)))
            except ValueError:
                continue
    return sorted(found, key=lambda item: item[1])


def parse_showinfo_times(stderr_text):
    """Selected-frame offsets (seconds) from ffmpeg showinfo output. Pure."""
    return [float(m) for m in _PTS_RE.findall(stderr_text or "")]


def _run_ffmpeg(args):  # pragma: no cover - subprocess I/O
    proc = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          timeout=120)
    return proc.stderr.decode("utf-8", "replace")


def extract_candidates(mkv, seg_start, out_dir, base, *, runner=None,
                       scene=DEFAULT_SCENE, cap=DEFAULT_SEGMENT_CAP):
    """Scene-change candidate JPEGs (+1 uniform mid-segment frame) with epoch stamps.

    The uniform frame exists so a slow or static subject cannot slip through a
    scene-change-only filter. Any ffmpeg failure degrades to fewer frames, never raises.
    """
    runner = runner or _run_ffmpeg
    out = []
    pattern = os.path.join(out_dir, f"{base}_sc_%02d.jpg")
    try:
        stderr = runner([
            "ffmpeg", "-hide_banner", "-y", "-i", mkv,
            "-vf", f"select='gt(scene,{scene})',showinfo,scale=1280:-2",
            "-vsync", "vfr", "-frames:v", str(int(cap)), "-q:v", "2", pattern,
        ])
        times = parse_showinfo_times(stderr)
        for k, offset in enumerate(times[:cap], start=1):
            path = pattern % k
            if os.path.exists(path) and os.path.getsize(path) > 0:
                out.append((path, seg_start + offset))
    except Exception:  # noqa: BLE001 - a bad segment must not end the batch
        log.warning("shadow scan: scene extraction failed for %s", mkv, exc_info=True)
    mid = os.path.join(out_dir, f"{base}_mid.jpg")
    try:
        runner(["ffmpeg", "-hide_banner", "-y", "-ss", "450", "-i", mkv,
                "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "2", "-update", "1",
                mid])
        if os.path.exists(mid) and os.path.getsize(mid) > 0:
            out.append((mid, seg_start + 450.0))
    except Exception:  # noqa: BLE001
        log.warning("shadow scan: mid-frame extraction failed for %s", mkv, exc_info=True)
    return out
