#!/usr/bin/env python3
"""Score every frame of dumped SD clips and record what the frame pickers look at.

Step 2 of "Evaluating the follow-up frame pick" (docs/operations.md). Input is one
directory per event as written by ``tools/sd_frame_dump.py`` (``<event_start>/*.jpg``),
or a parent of such directories. Output is a JSON object ``{event_start: [frame, ...]}``
with, per frame: ``person`` score and subject ``box`` from the scorer (tiled), subject
``blur`` (``recclip.blur_score`` on the box), ``crop_var`` (Laplacian variance of the
box crop), whole-frame detail ``frame_var`` (``recclip.frame_detail``, what the smear
guard compares) and the frame ``size``. ``tools/frame_pick_sim.py`` replays the pickers
on it without touching the images again.

Every frame is one scorer request. The production scorer is shared with the live
daemons, so keep ``--workers`` low or point ``--url`` at a backup scorer::

    .venv/bin/python tools/frame_pick_eval.py frame-eval/ --out features.json \\
        --url http://scorer.example:8766/score --workers 2

Several ``--url`` are tried in order per frame (the first answer wins). Frames that
cannot be read are left out with a note on stderr. Read-only apart from ``--out``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from tapo_monitor import recclip, scorer


def event_dirs(paths):
    """``(event name, dir)`` for each event directory under ``paths``, sorted by name.

    A path holding JPEGs is an event itself; otherwise each subdirectory holding JPEGs is.
    """
    found = {}
    for p in paths:
        p = os.path.normpath(p)
        if _jpegs(p):
            found[os.path.basename(p)] = p
            continue
        for name in sorted(os.listdir(p)) if os.path.isdir(p) else ():
            sub = os.path.join(p, name)
            if _jpegs(sub):
                found[name] = sub
    return sorted(found.items())


def _jpegs(directory):
    if not os.path.isdir(directory):
        return []
    return sorted(os.path.join(directory, n) for n in os.listdir(directory)
                  if n.lower().endswith(".jpg"))


def score_first(urls, path, score, **kwargs):
    """The first non-None scorer answer among ``urls`` for ``path``, or None."""
    for url in urls:
        result = score(url, path, **kwargs)
        if result is not None:
            return result
    return None


def _crop_variance(path, box):
    try:
        return recclip._laplacian_variance(path, box)
    except Exception:  # noqa: BLE001 - Pillow/numpy missing or unreadable image
        return None


def _image_size(path):
    from PIL import Image
    with Image.open(path) as im:
        im.load()
        return list(im.size)


def frame_features(path, result, *, size, blur=recclip.blur_score, crop=_crop_variance,
                   detail=recclip.frame_detail):
    """The feature record of one frame from its scorer ``result`` (None = not scored).

    The measuring functions are injectable so the record's shape is testable without
    images. A frame without a subject box has no ``blur``/``crop_var``.
    """
    result = result or {}
    box = scorer.subject_box(result)
    return {
        "frame": path,
        "size": size,
        "scored": bool(result),
        "person": result.get("person"),
        "box": box,
        "people": result.get("people") or result.get("count"),
        "blur": blur(path, box=box) if box else None,
        "crop_var": crop(path, box) if box else None,
        "frame_var": detail(path),
    }


def evaluate(events, measure, workers=2, log=None):
    """``{event: [features...]}`` with ``measure(path)`` returning a record or None to skip."""
    out = {}
    with ThreadPoolExecutor(max(int(workers), 1)) as ex:
        for name, frames in events:
            records = list(ex.map(measure, frames))
            out[name] = [r for r in records if r is not None]
            if log:
                log(f"{name}: {len(out[name])}/{len(frames)} frames")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dirs", nargs="+", help="event dirs, or parents of event dirs")
    ap.add_argument("--url", action="append", required=True,
                    help="scorer /score URL; repeat for fallbacks tried in order")
    ap.add_argument("--out", required=True, help="features JSON to write")
    ap.add_argument("--tiles", type=int, default=2, help="tiled scoring grid (2)")
    ap.add_argument("--workers", type=int, default=2,
                    help="parallel requests (2; the production scorer is shared)")
    ap.add_argument("--timeout", type=float, default=30.0, help="seconds per request (30)")
    ap.add_argument("--token-env", default=None,
                    help="env var NAME holding the scorer token (or <NAME>_FILE)")
    args = ap.parse_args(argv)

    token = scorer.resolve_token(args.token_env)

    def measure(path):
        try:
            size = _image_size(path)
        except Exception as exc:  # noqa: BLE001 - truncated or not an image
            print(f"skip unreadable {path}: {exc}", file=sys.stderr)
            return None
        result = score_first(args.url, path, scorer.score_image, timeout=args.timeout,
                             tiles=args.tiles, token=token)
        return frame_features(path, result, size=size)

    events = [(name, _jpegs(d)) for name, d in event_dirs(args.dirs)]
    feats = evaluate(events, measure, args.workers, log=lambda m: print(m, flush=True))
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(feats, f, indent=1)
    print(f"{len(feats)} events -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
