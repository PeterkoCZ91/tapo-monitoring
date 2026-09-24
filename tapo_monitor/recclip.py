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
import math
import os
import re
import subprocess
import sys
import time as _time
from datetime import datetime

from . import snapshot
from .sdclip import frame_offsets

SEGMENT_SECONDS = 900
RECORDING_FRAME_EVERY = 4
# The recorder flushes continuously, but wait past the event's window so its trailing
# frames are on disk before we read them. No pytapo freshness guard applies (local file).
# A live segment measured 2-3 s behind the wall clock; the event's start comes from the
# camera clock, which the digest flags once it drifts past 5 s. 15 s covers both plus a
# poll tick. It was 60, which alone held every recording follow-up back ~45 s.
RECORDING_READY_MARGIN = 15
# First look at a recording follow-up: the opening seconds of the event, read as soon as
# they are on disk, so a subject already in view is alerted ~40 s after the event instead
# of after the whole window. Six frames at RECORDING_FRAME_EVERY. The rest of the window
# is read at its usual time only when this look finds no subject.
RECORDING_EARLY_SPAN = 24

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


MIN_CROP_PX = 16          # a subject box smaller than this in either side is noise
BOX_PAD = 0.10            # margin around the person box, fraction of its size


def _crop_bounds(box, width, height):
    """Padded, clamped integer (l, t, r, b) of ``box`` inside a width x height image, or None."""
    try:
        x1, y1, x2, y2 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if not all(map(math.isfinite, (x1, y1, x2, y2))):
        return None
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    px, py = (x2 - x1) * BOX_PAD, (y2 - y1) * BOX_PAD
    left, top = max(int(x1 - px), 0), max(int(y1 - py), 0)
    right, bottom = min(int(math.ceil(x2 + px)), width), min(int(math.ceil(y2 + py)), height)
    if right - left < MIN_CROP_PX or bottom - top < MIN_CROP_PX:
        return None
    return left, top, right, bottom


def _laplacian_variance(path, box):  # pragma: no cover - needs optional Pillow/numpy
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        bounds = _crop_bounds(box, *im.size)
        if bounds is None:
            return None
        g = np.asarray(im.convert("L").crop(bounds), dtype=np.float64)
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
    return float(lap.var())


def subject_blur(path, box, variance=None):
    """Blur of the subject crop (lower = sharper), or None if it can't be measured.

    Laplacian variance of the padded ``box`` region, mapped to ``100 / (1 + var)`` so it
    orders like ffmpeg's blurdetect (lower = sharper). Whole-frame blur barely moves when
    90 % of a night image is sharp static background; the crop isolates the walker.
    """
    variance = variance or _laplacian_variance
    try:
        var = variance(path, box)
    except Exception:  # noqa: BLE001 - Pillow/numpy missing or unreadable image
        return None
    if var is None or not math.isfinite(var) or var < 0:
        return None
    return 100.0 / (1.0 + var)


def blur_score(path, runner=None, box=None, variance=None):
    """Blur (lower = sharper), or None if unavailable.

    With a scorer ``box`` [x1,y1,x2,y2] the subject crop is measured (Laplacian variance);
    without one, or if that can't be measured, ffmpeg ``blurdetect`` 'blur mean' of the whole
    frame. Compare only scores taken the same way within one candidate set.
    """
    if box is not None:
        b = subject_blur(path, box, variance)
        if b is not None:
            return b
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


# The ``largest`` pick (config ``sd_frame_pick: largest``). The subject-crop blur score
# favours small subjects (a distant walker packs more edges per pixel) and IR grain reads
# as detail, so "sharpest" tends to send the person when they are already far away.
#
# Blur guard: a candidate more than LARGEST_MAX_BLUR_RATIO times blurrier than the
# sharpest one is out. Checked by eye on 45 recorder events (4K, day and IR night): every
# candidate the guard removed (4.2x, 4.6x) was visibly smeared or cut at the frame edge,
# while the kept ones up to 3.0x were fine or better (at night the "sharp" frame is often
# just noisier). 2.0 would have also dropped an IR frame at 2.4x whose subject was 2.4x
# taller, and the motivating SD clip needs 2.5.
# Minimum gain: the largest must beat the sharpest candidate's box area by this factor,
# otherwise the sharpest stays. Without it near-ties (box area +3 %) swapped a sharp,
# whole subject for one cut at the frame edge; with it no pick got shorter.
LARGEST_MAX_BLUR_RATIO = 3.0
LARGEST_MIN_GAIN = 1.25


def box_area(box):
    """Area of an ``[x1, y1, x2, y2]`` box, or None when it is not a usable box. Pure."""
    try:
        x1, y1, x2, y2 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    area = abs(x2 - x1) * abs(y2 - y1)
    return area if math.isfinite(area) else None


def select_largest(candidates, max_blur_ratio=LARGEST_MAX_BLUR_RATIO,
                   min_gain=LARGEST_MIN_GAIN):
    """Frame whose subject box is largest, skipping ones much blurrier than the sharpest.

    ``candidates`` is ``(frame_path, blur_or_None, box)`` in the same order as for
    :func:`select_sharpest`. A frame whose blur exceeds ``max_blur_ratio`` times the
    sharpest candidate's is out (one without a blur value too, unless none has one).
    The largest remaining box wins only when its area is at least ``min_gain`` times the
    sharpest candidate's; otherwise the sharpest does. When any candidate lacks a usable
    box the sizes are not comparable and the choice is :func:`select_sharpest`. Ties
    keep the earlier (higher-scoring) candidate. Returns None for empty input.
    """
    if not candidates:
        return None
    areas = [box_area(box) if box else None for _f, _b, box in candidates]
    if any(a is None for a in areas):
        return select_sharpest([(f, b) for f, b, _box in candidates])
    blurs = [b for _f, b, _box in candidates if b is not None]
    ceiling = min(blurs) * max_blur_ratio if blurs else None
    best = None
    for (frame, blur, _box), area in zip(candidates, areas, strict=True):
        if ceiling is not None and (blur is None or blur > ceiling):
            continue
        if best is None or area > best[1]:
            best = (frame, area)
    sharpest = select_sharpest([(f, b) for f, b, _box in candidates])
    sharpest_area = areas[[f for f, _b, _box in candidates].index(sharpest)]
    if best is None or best[1] < sharpest_area * min_gain:
        return sharpest
    return best[0]


# ── frame extraction + fetch entry point ─────────────────────────────────────

def fresh_delay(span):
    """Seconds to wait after the event before its window is flushed to disk."""
    return int(span) + RECORDING_READY_MARGIN


def early_span(span):
    """The first-look window for a ``span``-second follow-up, or None when it is too short.

    Pure. A window that ends barely later than the first look would read almost the same
    frames twice, so the split needs at least one more frame interval beyond it.
    """
    if int(span) >= RECORDING_EARLY_SPAN + RECORDING_FRAME_EVERY:
        return RECORDING_EARLY_SPAN
    return None


def _run_ffmpeg(args):  # pragma: no cover - subprocess I/O
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=30, check=True)


def extract_frames(mkv, seg_start, event_start, span, every, out_dir, base, runner=None,
                   rotate=0, on_frame=None, dense=None):
    """One JPEG every ``every`` sec across ``span``, seeking from the event's offset in
    the segment. Returns paths (oldest first); clips the window to the segment end.
    Names carry the segment epoch plus actual seek offset for pan-window filtering.
    ``on_frame(path)`` is called as each frame lands, so the caller can start scoring it
    while the next one is still being decoded. ``dense = (seconds, step)`` adds denser
    frames over the window's opening seconds (``sdclip.frame_offsets``)."""
    runner = runner or _run_ffmpeg
    out_dir = out_dir.rstrip("/")
    vf = snapshot.scaled_vf(rotate)
    base_offset = max(int(event_start - seg_start), 0)
    limit = min(base_offset + max(int(span), 1), SEGMENT_SECONDS)
    offsets = [base_offset + o for o in frame_offsets(span, every, dense)]
    paths = []
    for k, offset in enumerate(o for o in offsets if o < limit):
        out_path = os.path.join(out_dir, f"{base}_{k:02d}_at{int(seg_start + offset)}.jpg")
        try:
            runner(["ffmpeg", "-y", "-ss", str(offset), "-i", mkv, "-frames:v", "1",
                    "-vf", vf, "-q:v", "2", "-update", "1", out_path])
        except Exception:
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            paths.append(out_path)
            if on_frame is not None:
                on_frame(out_path)
    return paths


def fetch_recording_frames(cfg, event_start, span, out_dir,
                           base_dir=None,
                           segment_for=segment_for, extract=extract_frames, on_frame=None,
                           dense=None):
    """Candidate JPEGs from the local recording around the event; ``[]`` when no segment.

    ``base_dir`` defaults to the ``RECORDING_ROOT`` env var — the same recorder tree the
    live-fallback (``snapshot.latest_recording_frame``) reads, so a deployment configures
    the recorder location in exactly one place. Empty/unset root -> ``[]`` (caller falls
    back to the SD/live path). Signature-compatible with
    ``sdclip.fetch_sd_frames_subprocess`` so the daemon's SD follow-up pass can accept it
    via ``fetch_frames=``. ``dense`` is passed to the extractor only when set.
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
    extra = {"dense": dense} if dense else {}
    return extract(mkv, seg_start, event_start, span, RECORDING_FRAME_EVERY, out_dir, base,
                   rotate=getattr(cfg, "rotate", 0), on_frame=on_frame, **extra)
