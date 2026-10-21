#!/usr/bin/env python3
"""Suggest ``scorer.ignore_boxes`` rects from person boxes that repeat at the same place.

Read-only: it reads scorer shadow/score JSONL logs (``primary_boxes`` / ``shadow_boxes`` of
``[x1, y1, x2, y2, score]``, normalized, plus ``ts``, ``source_id`` or ``camera`` and
``frame``) and prints a report. It never edits a config; paste the snippet yourself.

A static false alarm (a swing roof, a coat on a hook) is boxed again and again at the same
place and size. A person walking by is not. Boxes of one camera are clustered by IoU; a
cluster that shows up in many distinct frames on several distinct days is a candidate.
With ``--labels DATASET_DIR`` (a ``tapo-monitor label`` dataset) a cluster is dropped when
any of its frames is labelled ``person``: the frame id of the log is the first 16 hex
digits of the JPEG sha256, which is how the labels are matched.

Usage:
  tools/suggest_ignore_zones.py shadow.jsonl [more.jsonl ...] [--labels ~/tapo-dataset]
      [--min-frames 20] [--min-days 2] [--min-score 0.25] [--iou 0.7] [--pad 0.01]
      [--field primary|shadow|both] [--camera NAME] [--json]

Start every suggestion with ``ignore_boxes_mode: observe``: a rect only matches a person
box of the same size AND place (IoU >= 0.75), see docs/configuration.md.
"""

import argparse
import json
import os
import sys

FIELDS = {"primary": ("primary_boxes",), "shadow": ("shadow_boxes",),
          "both": ("primary_boxes", "shadow_boxes")}


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _iou(a, b):
    iw = min(a[2], b[2]) - max(a[0], b[0])
    ih = min(a[3], b[3]) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _boxes(value):
    out = []
    if not isinstance(value, list):
        return out
    for b in value:
        if isinstance(b, (list, tuple)) and len(b) == 5:
            n = [_num(v) for v in b]
            if all(v is not None for v in n) and n[0] < n[2] and n[1] < n[3]:
                out.append(n)
    return out


def load_observations(paths, fields, camera=None):
    """``[(camera, frame_id, day, box5)]``; unreadable lines and rows with an error skip."""
    obs = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("error"):
                    continue
                cam = rec.get("source_id") or rec.get("camera")
                if not isinstance(cam, str) or not cam or (camera and cam != camera):
                    continue
                ts = rec.get("ts")
                day = ts[:10] if isinstance(ts, str) and len(ts) >= 10 else None
                frame = rec.get("frame")
                if not isinstance(frame, str) or not frame:
                    frame = f"{os.path.basename(path)}:{n}"   # no id: unique per row
                for field in fields:
                    for box in _boxes(rec.get(field)):
                        obs.append((cam, frame, day, box))
    return obs


def person_frame_prefixes(dataset_dir):
    """Frame-id prefixes (16 hex digits) of every frame labelled ``person``."""
    from tapo_monitor import labeling
    return {sha[:16] for sha, rec in labeling.latest_labels(dataset_dir).items()
            if rec.get("label") == "person"}


def cluster(boxes, iou):
    """Greedy clusters of ``(frame, day, box5)``; each tracks a running mean of its box."""
    clusters = []
    for item in boxes:
        box = item[2]
        best, best_iou = None, iou
        for c in clusters:
            v = _iou(box, c["mean"])
            if v >= best_iou:
                best, best_iou = c, v
        if best is None:
            best = {"mean": list(box[:4]), "items": []}
            clusters.append(best)
        best["items"].append(item)
        k = len(best["items"])
        best["mean"] = [m + (b - m) / k for m, b in zip(best["mean"], box[:4], strict=True)]
    return clusters


def _median(values):
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def suggest(obs, *, min_frames=20, min_days=2, min_score=0.25, iou=0.7, pad=0.01,
            person_frames=frozenset()):
    """``{camera: [suggestion, ...]}``, biggest cluster first. Pure."""
    by_cam = {}
    for cam, frame, day, box in obs:
        if box[4] >= min_score:
            by_cam.setdefault(cam, []).append((frame, day, box))
    result = {}
    for cam, items in sorted(by_cam.items()):
        found = []
        for c in cluster(items, iou):
            frames = {f for f, _, _ in c["items"]}
            days = {d for _, d, _ in c["items"] if d}
            if len(frames) < min_frames or len(days) < min_days:
                continue
            labelled = sorted(f for f in frames if f[:16] in person_frames)
            if labelled:
                continue
            boxes = [b for _, _, b in c["items"]]
            med = [_median([b[i] for b in boxes]) for i in range(4)]
            rect = [round(max(0.0, med[0] - pad), 3), round(max(0.0, med[1] - pad), 3),
                    round(min(1.0, med[2] + pad), 3), round(min(1.0, med[3] + pad), 3)]
            scores = [b[4] for b in boxes]
            found.append({"rect": rect, "frames": len(frames), "days": len(days),
                          "score_min": round(min(scores), 3),
                          "score_max": round(max(scores), 3)})
        if found:
            found.sort(key=lambda s: -s["frames"])
            result[cam] = found
    return result


def render_yaml(suggestions):
    out = []
    for cam, items in suggestions.items():
        out += [f"# camera: {cam}", "scorer:", "  ignore_boxes_mode: observe",
                "  ignore_boxes:"]
        for s in items:
            r = ", ".join(f"{v:.3f}" for v in s["rect"])
            out.append(f"    - [{r}]   # {s['frames']} frames, {s['days']} days, "
                       f"score {s['score_min']:.2f}-{s['score_max']:.2f}")
        out.append("")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("logs", nargs="+", help="scorer shadow/score JSONL logs")
    ap.add_argument("--labels", help="tapo-monitor label dataset dir; person-labelled "
                                     "clusters are excluded")
    ap.add_argument("--min-frames", type=int, default=20)
    ap.add_argument("--min-days", type=int, default=2)
    ap.add_argument("--min-score", type=float, default=0.25)
    ap.add_argument("--iou", type=float, default=0.7, help="cluster IoU (default 0.7)")
    ap.add_argument("--pad", type=float, default=0.01, help="padding per side (fraction)")
    ap.add_argument("--field", choices=sorted(FIELDS), default="both")
    ap.add_argument("--camera", help="only this camera (source_id)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        obs = load_observations(args.logs, FIELDS[args.field], args.camera)
        persons = person_frame_prefixes(args.labels) if args.labels else frozenset()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    res = suggest(obs, min_frames=args.min_frames, min_days=args.min_days,
                  min_score=args.min_score, iou=args.iou, pad=args.pad,
                  person_frames=persons)
    if args.json:
        print(json.dumps(res, indent=2))
    elif not res:
        print(f"No repeating box found ({len(obs)} boxes read; needs >= {args.min_frames} "
              f"frames on >= {args.min_days} days). Nothing to suggest.")
    else:
        print("# Suggestions only - review the frames, nothing was changed.\n")
        print(render_yaml(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
