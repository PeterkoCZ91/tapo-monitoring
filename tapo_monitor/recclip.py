"""Alert snapshot from the local 24/7 recorder mkv, keyed by event time.

A separate recorder service writes stream1 to
``<base>/<host>/<YYYY-MM-DD>/<HH>/zaznam_YYYYMMDDThhmmss.mkv`` in 15-min segments,
filenames in host local time. This module locates the segment covering an event,
extracts candidate frames around it, and scores their blur. The daemon's SD follow-up
pass reuses everything else (scoring, caption, send) — this is ``sdclip`` with a local
file segment source instead of a camera-SD download.

Why a local recording beats the live grab / camera SD:
  * full stream1 resolution even for a camera whose detection runs on stream2 (a
    concurrent recorder holds stream1), so the scorer sees more;
  * a whole buffer of frames is already on disk, so we can pick the *sharpest*
    above-threshold one (ffmpeg ``blurdetect``) — night motion smears a single frame;
  * no extra camera load and no RTSP session conflict.
"""

import glob
import os
import re
import subprocess
import sys
import time as _time
from datetime import datetime

from . import snapshot

SEGMENT_SECONDS = 900
RECORDING_FRAME_EVERY = 4
# The recorder flushes continuously, but wait past the event's window so its trailing
# frames are on disk before we read them. No pytapo freshness guard applies (local file).
RECORDING_READY_MARGIN = 60

_PREFIX = "zaznam_"
_SUFFIX = ".mkv"
_BLUR_RE = re.compile(r"blur mean:\s*([0-9.]+)")


# ── segment location ─────────────────────────────────────────────────────────

def parse_segment_start(path):
    """Local epoch of a segment's start, from its filename. ValueError if it doesn't match."""
    base = os.path.basename(path)
    if not (base.startswith(_PREFIX) and base.endswith(_SUFFIX)):
        raise ValueError(f"not a segment name: {base}")
    stamp = base[len(_PREFIX):-len(_SUFFIX)]
    return datetime.strptime(stamp, "%Y%m%dT%H%M%S").timestamp()


def _hour_dir(base_dir, host, ts):
    dt = datetime.fromtimestamp(ts)
    return os.path.join(base_dir, host, dt.strftime("%Y-%m-%d"), dt.strftime("%H"))


def _default_lister(d):
    return sorted(glob.glob(os.path.join(d, _PREFIX + "*" + _SUFFIX)))


def segment_for(base_dir, host, event_start, lister=None):
    """``(mkv_path, seg_start_epoch)`` for the segment covering ``event_start``, or None.

    Scans the event's local hour dir and the previous hour (a segment started late in an
    hour spills past the hour boundary), then picks the latest-starting segment that
    still contains ``event_start``.
    """
    lister = lister or _default_lister
    seen, files = set(), []
    for ts in (event_start - 3600, event_start):
        for f in lister(_hour_dir(base_dir, host, ts)):
            if f not in seen:
                seen.add(f)
                files.append(f)
    best = None
    for f in files:
        try:
            s = parse_segment_start(f)
        except ValueError:
            continue
        if s <= event_start < s + SEGMENT_SECONDS and (best is None or s > best[1]):
            best = (f, s)
    return best


# ── blur scoring + sharpest selection ────────────────────────────────────────

def _run_blurdetect(path):  # pragma: no cover - subprocess I/O
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path, "-vf", "blurdetect", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30,
    )
    return p.stderr.decode("utf-8", "replace")


def blur_score(path, runner=None):
    """ffmpeg ``blurdetect`` 'blur mean' (lower = sharper), or None if unavailable."""
    runner = runner or _run_blurdetect
    try:
        m = _BLUR_RE.search(runner(path) or "")
    except Exception:
        return None
    return float(m.group(1)) if m else None


def select_sharpest(candidates):
    """Sharpest (lowest blur) among above-threshold candidates; first if no blur values.

    ``candidates`` is ``(frame_path, blur_or_None)`` pre-filtered above threshold and
    ordered best-score-first, so falling back to the first keeps the highest score when
    blur is unavailable. Returns None for empty input.
    """
    if not candidates:
        return None
    with_blur = [(f, b) for f, b in candidates if b is not None]
    if not with_blur:
        return candidates[0][0]
    return min(with_blur, key=lambda fb: fb[1])[0]


# ── frame extraction + fetch entry point ─────────────────────────────────────

def fresh_delay(span):
    """Seconds to wait after the event before its window is flushed to disk."""
    return int(span) + RECORDING_READY_MARGIN


def _run_ffmpeg(args):  # pragma: no cover - subprocess I/O
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=30, check=True)


def extract_frames(mkv, seg_start, event_start, span, every, out_dir, base, runner=None,
                   rotate=0):
    """One JPEG every ``every`` sec across ``span``, seeking from the event's offset in
    the segment. Returns paths (oldest first); clips the window to the segment end."""
    runner = runner or _run_ffmpeg
    out_dir = out_dir.rstrip("/")
    vf = snapshot.scaled_vf(rotate)
    base_offset = max(int(event_start - seg_start), 0)
    limit = min(base_offset + max(int(span), 1), SEGMENT_SECONDS)
    paths = []
    for k, offset in enumerate(range(base_offset, limit, max(int(every), 1))):
        out_path = os.path.join(out_dir, f"{base}_{k:02d}.jpg")
        try:
            runner(["ffmpeg", "-y", "-ss", str(offset), "-i", mkv, "-frames:v", "1",
                    "-vf", vf, "-q:v", "2", "-update", "1", out_path])
        except Exception:
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            paths.append(out_path)
    return paths


def fetch_recording_frames(cfg, event_start, span, out_dir,
                           base_dir=None,
                           segment_for=segment_for, extract=extract_frames):
    """Candidate JPEGs from the local recording around the event; ``[]`` when no segment.

    ``base_dir`` defaults to the ``RECORDING_ROOT`` env var — the same recorder tree the
    live-fallback (``snapshot.latest_recording_frame``) reads, so a deployment configures
    the recorder location in exactly one place. Empty/unset root -> ``[]`` (caller falls
    back to the SD/live path). Signature-compatible with
    ``sdclip.fetch_sd_frames_subprocess`` so the daemon's SD follow-up pass can accept it
    via ``fetch_frames=``.
    """
    base_dir = base_dir or os.getenv("RECORDING_ROOT", "")
    host = getattr(cfg, "host", None)
    if not base_dir:
        print(f"recording fetch: RECORDING_ROOT unset for host={host}", file=sys.stderr)
        return []
    seg = segment_for(base_dir, host, event_start)
    if not seg:
        print(f"recording fetch: no segment for host={host} at {int(event_start)}",
              file=sys.stderr)
        return []
    mkv, seg_start = seg
    base = f"rec_{int(event_start)}_{int(_time.time() * 1000)}"
    return extract(mkv, seg_start, event_start, span, RECORDING_FRAME_EVERY, out_dir, base,
                   rotate=getattr(cfg, "rotate", 0))
