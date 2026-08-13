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
