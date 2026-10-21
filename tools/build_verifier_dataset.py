#!/usr/bin/env python3
"""Build a crop dataset for a small person/no-person verifier (roadmap 9.4). Data only.

Reads a labelling dataset directory (``index.jsonl``, ``labels.jsonl`` and, when present,
``teacher.jsonl``; see ``tapo_monitor/labeling.py``) and cuts the scorer's boxes out of the
labelled frames. Boxes come from the ``boxes`` field of the index records
(``[x1, y1, x2, y2, score, model]``, normalized) and, for frames without any, from optional
scorer shadow logs (``--log``; ``primary_boxes`` entries ``[x1, y1, x2, y2, score]``,
joined on ``frame`` = first 16 hex digits of the image SHA-256).

Every crop gets the label of its frame: ``person/`` or ``no_person/``. Frames labelled
``unsure`` (or unlabelled) are skipped. Crops from ``person`` frames can include
non-person boxes (the label is per frame, not per box); review before training.

Output: ``OUT/{person,no_person}/*.jpg`` and ``OUT/manifest.jsonl`` with ``file``,
``source_sha256``, ``camera``, ``day``, ``box`` (normalized, as detected), ``crop_px``
(padded pixel box), ``score``, ``model``, ``label``, ``split``. The train/val split is
deterministic (``--seed``) and made per camera-day, never per frame. Pillow only; inputs
are only read; nothing is trained.
"""

import argparse
import datetime
import json
import math
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from tapo_monitor import labeling  # noqa: E402

CLASSES = ("person", "no_person")
WARN_BELOW = 50


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def log_boxes(paths):
    """``{frame id: [(x1, y1, x2, y2, score, model)]}`` from shadow logs; last row wins."""
    out = {}
    for path in paths:
        for rec in read_jsonl(path):
            frame, raw = rec.get("frame"), rec.get("primary_boxes")
            if not isinstance(frame, str) or not isinstance(raw, list):
                continue
            boxes = []
            for b in raw:
                if isinstance(b, (list, tuple)) and len(b) >= 5:
                    vals = [_num(v) for v in b[:5]]
                    if None not in vals:
                        boxes.append((*vals, "primary"))
            out[frame] = boxes
    return out


def pad_box(box, size, pad, min_size):
    """Pixel crop ``(l, t, r, b)`` of a normalized box: padded by ``pad`` of its size per
    side, grown to at least ``min_size`` px per side where the image allows, clamped."""
    w, h = size
    x1, y1, x2, y2 = (max(0.0, min(1.0, float(v))) for v in box[:4])
    x1, x2, y1, y2 = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
    cx, cy = (x1 + x2) / 2 * w, (y1 + y2) / 2 * h
    bw = max((x2 - x1) * w * (1 + 2 * pad), min(min_size, w))
    bh = max((y2 - y1) * h * (1 + 2 * pad), min(min_size, h))
    left = min(max(0.0, cx - bw / 2), max(0.0, w - bw))
    top = min(max(0.0, cy - bh / 2), max(0.0, h - bh))
    right, bottom = min(float(w), left + bw), min(float(h), top + bh)
    return (round(left), round(top), round(right), round(bottom))


def camera_day(frame, full_path):
    day = "unknown"
    ts = frame.ts
    if ts is None:
        try:
            ts = os.path.getmtime(full_path)
        except OSError:
            ts = None
    if ts is not None:
        day = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
    return frame.camera or "unknown", day


def split_groups(groups, val_fraction, seed):
    """``{camera-day: "train"|"val"}``; deterministic for the same groups and seed."""
    ordered = sorted(groups)
    random.Random(seed).shuffle(ordered)
    n_val = 0
    if val_fraction > 0 and len(ordered) >= 2:
        n_val = min(len(ordered) - 1, max(1, round(len(ordered) * val_fraction)))
    return {g: ("val" if i < n_val else "train") for i, g in enumerate(ordered)}


def build(dataset, out, *, logs=(), pad=0.15, min_size=32, resize=None, min_score=0.0,
          max_boxes=10, val_fraction=0.2, seed=0, quality=92):
    dataset = os.path.realpath(dataset)
    labels = labeling.latest_labels(dataset)
    extra = log_boxes(logs)
    items = []  # (frame, label, boxes)
    skipped = {"unsure": 0, "unlabeled": 0, "no_boxes": 0}
    for frame in labeling.load_frames(dataset):
        rec = labels.get(frame.sha256)
        label = rec.get("label") if rec else None
        if label is None:
            skipped["unlabeled"] += 1
            continue
        if label not in CLASSES:
            skipped["unsure"] += 1
            continue
        boxes = list(frame.boxes or ()) or list(extra.get(frame.sha256[:16], ()))
        boxes = sorted((b for b in boxes if b[4] >= min_score), key=lambda b: -b[4])[:max_boxes]
        if not boxes:
            skipped["no_boxes"] += 1
            continue
        items.append((frame, label, boxes))
    groups = {"/".join(camera_day(f, os.path.join(dataset, f.path))) for f, _, _ in items}
    assign = split_groups(groups, val_fraction, seed)
    out = Path(out)
    for cls in CLASSES:
        (out / cls).mkdir(parents=True, exist_ok=True)
    manifest = []
    for frame, label, boxes in sorted(items, key=lambda it: it[0].sha256):
        cam, day = camera_day(frame, os.path.join(dataset, frame.path))
        try:
            with Image.open(os.path.join(dataset, frame.path)) as im:
                im = im.convert("RGB")
                for i, box in enumerate(boxes):
                    px = pad_box(box, im.size, pad, min_size)
                    if px[2] - px[0] < 2 or px[3] - px[1] < 2:
                        continue
                    crop = im.crop(px)
                    if resize:
                        crop = crop.resize((resize, resize), Image.BILINEAR)
                    name = f"{label}/{frame.sha256[:16]}_{i:02d}.jpg"
                    crop.save(out / name, "JPEG", quality=quality)
                    manifest.append({
                        "file": name, "source_sha256": frame.sha256, "camera": cam,
                        "day": day, "box": [round(v, 5) for v in box[:4]],
                        "crop_px": list(px), "score": round(box[4], 4), "model": box[5],
                        "label": label, "split": assign[f"{cam}/{day}"]})
        except (OSError, ValueError):
            continue
    with open(out / "manifest.jsonl", "w", encoding="utf-8") as f:
        for row in manifest:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    counts = {c: sum(1 for r in manifest if r["label"] == c) for c in CLASSES}
    return manifest, counts, skipped, assign


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dataset", help="labelling dataset dir (index.jsonl, labels.jsonl)")
    ap.add_argument("out", help="output dir (created)")
    ap.add_argument("--log", action="append", default=[], metavar="JSONL",
                    help="scorer shadow log with primary_boxes (repeatable)")
    ap.add_argument("--pad", type=float, default=0.15, help="padding per side, fraction of box")
    ap.add_argument("--min-size", type=int, default=32, help="min crop side in px")
    ap.add_argument("--resize", type=int, default=None, help="resize crops to NxN px")
    ap.add_argument("--min-score", type=float, default=0.0, help="drop boxes below this score")
    ap.add_argument("--max-boxes", type=int, default=10, help="best N boxes per frame")
    ap.add_argument("--val-fraction", type=float, default=0.2, help="share of camera-days in val")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    if not os.path.isdir(args.dataset):
        ap.error(f"dataset dir not found: {args.dataset}")
    manifest, counts, skipped, assign = build(
        args.dataset, args.out, logs=args.log, pad=args.pad, min_size=args.min_size,
        resize=args.resize, min_score=args.min_score, max_boxes=args.max_boxes,
        val_fraction=args.val_fraction, seed=args.seed)
    for cls in CLASSES:
        n_train = sum(1 for r in manifest if r["label"] == cls and r["split"] == "train")
        n_val = sum(1 for r in manifest if r["label"] == cls and r["split"] == "val")
        print(f"{cls:<10} {counts[cls]} crops (train {n_train}, val {n_val})")
    n_tr = sum(1 for s in assign.values() if s == "train")
    print(f"camera-days: {n_tr} train, {len(assign) - n_tr} val")
    print(f"skipped frames: {skipped['unsure']} unsure, {skipped['unlabeled']} unlabeled, "
          f"{skipped['no_boxes']} without boxes")
    for cls in CLASSES:
        if counts[cls] < WARN_BELOW:
            print(f"WARNING: class {cls} has only {counts[cls]} crops (< {WARN_BELOW})")
    print(f"manifest: {Path(args.out) / 'manifest.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
