#!/usr/bin/env python3
"""Build a labeling set from the logs of two shadow targets (see docs/operations.md).

Joins ``shadow.jsonl`` and ``shadow2.jsonl`` on ``frame`` (the first 16 hex digits of the
body's SHA-256), keeps only frames whose image survives in a ``disagree/`` directory, drops
frames already in ``labels.jsonl`` (matched by the image's SHA-256) and picks a capped,
per-source-balanced set:

* ``strong``     one model >= --high and another < --low
* ``threshold``  the models split at --high (and are not ``strong``)
* ``small``      the best box of any model is shorter than --small-height of the frame
* ``control_person`` / ``control_empty``  random samples where all models agree

Output: ``OUT/disagree<N>/*.jpg``, ``OUT/index.jsonl`` (``file``, ``ts``, ``person`` and
``boxes`` as ``[x1, y1, x2, y2, score, model]``), ``OUT/teacher.jsonl`` and
``OUT/selection-report.md``. Source ids appear only as short hashes. Stdlib only; inputs
are only read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"^(\d{8}T\d{6})(\d{3})_p\d+(?:\.\d+)?_s\d+(?:\.\d+)?(?:-\d+)?\.jpe?g$", re.I)
CATEGORIES = ("strong", "threshold", "small", "control_person", "control_empty")
DISAGREEING = ("strong", "threshold")
DEFAULT_MODELS = ("yolox640", "rfdetr", "yolox960")


def read_jsonl(path):
    rows = []
    if not path or not Path(path).is_file():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def short(source):
    return hashlib.sha256(str(source).encode()).hexdigest()[:8]


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def name_ts(name):
    """Epoch seconds from ``YYYYMMDDTHHMMSSmmm_...`` (UTC), or None."""
    m = NAME_RE.match(name)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.timestamp() + int(m.group(2)) / 1000.0


def clean_boxes(raw, model):
    out = []
    if not isinstance(raw, list):
        return out
    for box in raw:
        if isinstance(box, (list, tuple)) and len(box) >= 5:
            vals = [num(v) for v in box[:5]]
            if None not in vals:
                out.append([*vals, model])
    return out


def load_frames(log1, log2, models):
    """``({frame: {"source", "scores", "boxes"}}, rows without a frame)``; last row wins."""
    frames = {}
    missing = 0
    for path, shadow_model in ((log1, models[1]), (log2, models[2])):
        for rec in read_jsonl(path):
            frame = rec.get("frame")
            if not isinstance(frame, str) or not frame:
                missing += 1
                continue
            item = frames.setdefault(frame, {"source": rec.get("source_id"), "scores": {},
                                             "boxes": {}})
            if item["source"] is None:
                item["source"] = rec.get("source_id")
            p, s = num(rec.get("primary_person")), num(rec.get("shadow_person"))
            if p is not None:
                item["scores"][models[0]] = p
                item["boxes"][models[0]] = clean_boxes(rec.get("primary_boxes"), models[0])
            if s is not None and not rec.get("error"):
                item["scores"][shadow_model] = s
                item["boxes"][shadow_model] = clean_boxes(rec.get("shadow_boxes"), shadow_model)
    return frames, missing


def dataset_images(dirs):
    """``{frame: (path, ts, sha256, kind)}`` for every JPEG under the dataset directories.

    ``kind`` is the directory's base name (e.g. ``sent-log``); ``ts`` comes from the
    ``index.jsonl`` next to the file, else the file's mtime. The first copy of a hash wins.
    """
    found = {}
    for directory in dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        kind = root.resolve().name
        stamps = {}
        for idx in sorted(root.rglob("index.jsonl")):
            for rec in read_jsonl(idx):
                ts, name = num(rec.get("ts")), rec.get("file")
                if ts is not None and isinstance(name, str):
                    stamps[(idx.parent, name)] = ts
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in (".jpg", ".jpeg") or not path.is_file():
                continue
            try:
                sha = sha256_file(path)
                ts = stamps.get((path.parent, path.name), path.stat().st_mtime)
            except OSError:
                continue
            found.setdefault(sha[:16], (path, ts, sha, kind))
    return found


def find_images(dirs):
    """``{frame: (path, ts, sha256, "disagree")}`` from the disagree directories."""
    found = {}
    for directory in dirs:
        d = Path(directory)
        if not d.is_dir():
            continue
        for path in sorted(d.iterdir()):
            ts = name_ts(path.name)
            if ts is None or not path.is_file():
                continue
            try:
                sha = sha256_file(path)
            except OSError:
                continue
            found.setdefault(sha[:16], (path, ts, sha, "disagree"))
    return found


def labeled_hashes(path):
    out = set()
    for rec in read_jsonl(path):
        sha = rec.get("sha256")
        if isinstance(sha, str):
            out.add(sha)
    return out


def best_box_height(item, min_score):
    best = None
    for boxes in item["boxes"].values():
        for _x1, y1, _x2, y2, score, _model in boxes:
            if score >= min_score and (best is None or score > best[0]):
                best = (score, y2 - y1)
    return None if best is None else best[1]


def categorize(item, high, low, small_height, small_min_score):
    """One category per frame (priority order), plus the score spread for ordering."""
    scores = list(item["scores"].values())
    if len(scores) < 2:
        return None, 0.0
    spread = max(scores) - min(scores)
    if max(scores) >= high and min(scores) < low:
        return "strong", spread
    if max(scores) >= high > min(scores):
        return "threshold", spread
    height = best_box_height(item, small_min_score)
    if height is not None and height < small_height:
        return "small", spread
    if min(scores) >= high:
        return "control_person", spread
    if max(scores) < low:
        return "control_empty", spread
    return None, spread


def pick(candidates, quota, source_cap, source_used, budget, rng, ordered):
    """Round-robin over sources up to ``quota``/``budget``, honouring the per-source cap."""
    by_source = defaultdict(list)
    for c in candidates:
        by_source[c["source"]].append(c)
    for lst in by_source.values():
        rng.shuffle(lst)
        if ordered:
            lst.sort(key=lambda c: -c["spread"])
    queues = sorted(by_source.items(), key=lambda kv: str(kv[0]))
    chosen = []
    while queues and len(chosen) < min(quota, budget):
        remaining = []
        for source, lst in queues:
            if len(chosen) >= min(quota, budget):
                break
            if source_used[source] >= source_cap:
                continue
            chosen.append(lst.pop(0))
            source_used[source] += 1
            if lst:
                remaining.append((source, lst))
        queues = remaining
    return chosen


def select(frames, images, labeled, *, high, low, small_height, small_min_score, limit,
           source_share, control, seed):
    rng = random.Random(seed)
    stats = {"frames": len(frames), "with_image": 0, "labeled_skipped": 0, "no_category": 0}
    pool = defaultdict(list)
    for frame, item in sorted(frames.items()):
        if frame not in images:
            continue
        stats["with_image"] += 1
        path, ts, sha, kind = images[frame]
        if sha in labeled:
            stats["labeled_skipped"] += 1
            continue
        category, spread = categorize(item, high, low, small_height, small_min_score)
        if category is None:
            stats["no_category"] += 1
            continue
        pool[category].append({"frame": frame, "item": item, "source": item["source"],
                               "path": path, "ts": ts, "sha": sha, "spread": spread,
                               "category": category, "kind": kind})
    cap = max(1, int(limit * source_share))
    used = Counter()
    chosen = []
    for category in CATEGORIES:
        quota = control if category.startswith("control") else limit
        chosen += pick(pool[category], quota, cap, used, limit - len(chosen), rng,
                       category in DISAGREEING)
    stats["available"] = {c: len(pool[c]) for c in CATEGORIES}
    return chosen, stats


def teacher_score(item, primary_model):
    p = item["scores"].get(primary_model)
    others = [v for k, v in item["scores"].items() if k != primary_model]
    if p is None or not others:
        return None
    return max(others, key=lambda v: abs(v - p))


def write_output(out, chosen, models, batch):
    out = Path(out)
    sub = out / f"disagree{batch}"
    sub.mkdir(parents=True, exist_ok=True)
    index, teacher, used_names = [], [], set()
    for c in sorted(chosen, key=lambda c: c["ts"]):
        name = c["path"].name
        while name in used_names:
            name = f"{c['sha'][:8]}-{name}"
        used_names.add(name)
        shutil.copyfile(c["path"], sub / name)
        scores = c["item"]["scores"]
        rec = {"file": f"{sub.name}/{name}", "ts": c["ts"],
               "person": scores.get(models[0], max(scores.values()))}
        boxes = [b for m in models for b in c["item"]["boxes"].get(m, [])]
        if boxes:
            rec["boxes"] = boxes
        index.append(rec)
        t = teacher_score(c["item"], models[0])
        if t is not None:
            teacher.append({"sha256": c["sha"], "person": t})
    for name, rows in (("index.jsonl", index), ("teacher.jsonl", teacher)):
        with open(out / name, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")


def report(chosen, stats, args):
    lines = ["# Labeling selection", "",
             f"Log rows without `frame` (dropped, cannot be paired): {stats['no_frame']}",
             f"Frames in logs: {stats['frames']}; with image: {stats['with_image']}; "
             f"already labeled: {stats['labeled_skipped']}; no category: {stats['no_category']}",
             f"Selected: {len(chosen)} (limit {args.limit}, per-source cap "
             f"{max(1, int(args.limit * args.source_share))})",
             f"Thresholds: high {args.high}, low {args.low}, small box height < "
             f"{args.small_height}", "", "## By category", "",
             "| category | available | selected |", "|---|---|---|"]
    by_cat = Counter(c["category"] for c in chosen)
    for cat in CATEGORIES:
        lines.append(f"| {cat} | {stats['available'][cat]} | {by_cat[cat]} |")
    kinds = Counter(c["kind"] for c in chosen)
    lines += ["", "## By image source type", "", "| type | selected |", "|---|---|"]
    lines += [f"| {k} | {kinds[k]} |" for k in sorted(kinds)]
    lines += ["", "## By source (hash)", "",
              "| source | " + " | ".join(CATEGORIES) + " | total |",
              "|---|" + "---|" * (len(CATEGORIES) + 1)]
    table = defaultdict(Counter)
    for c in chosen:
        table[short(c["source"])][c["category"]] += 1
    for src in sorted(table):
        row = table[src]
        lines.append(f"| {src} | " + " | ".join(str(row[c]) for c in CATEGORIES)
                     + f" | {sum(row.values())} |")
    return "\n".join(lines) + "\n"


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--shadow-log", required=True, help="shadow.jsonl of the first target")
    ap.add_argument("--shadow2-log", required=True, help="shadow2.jsonl of the second target")
    ap.add_argument("--disagree-dir", action="append", default=[],
                    help="disagree/ directory of a target (repeat for both)")
    ap.add_argument("--image-dir", action="append", default=[],
                    help="dataset directory searched recursively for JPEGs of frames missing "
                         "from disagree/ (repeat; e.g. sent-log, review-log)")
    ap.add_argument("--labels", help="labels.jsonl; frames already in it are skipped")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--batch", default="1", help="N in OUT/disagreeN (default 1)")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS),
                    help="model names for primary,shadow,shadow2 (default %(default)s)")
    ap.add_argument("--limit", type=int, default=600, help="maximum selected frames")
    ap.add_argument("--source-share", type=float, default=0.25,
                    help="largest share of --limit one source may take (default 0.25)")
    ap.add_argument("--control", type=int, default=40,
                    help="random frames per control category (default 40)")
    ap.add_argument("--high", type=float, default=0.45)
    ap.add_argument("--low", type=float, default=0.25)
    ap.add_argument("--small-height", type=float, default=0.10)
    ap.add_argument("--small-min-score", type=float, default=0.25,
                    help="a box needs this score to count as the best box (default 0.25)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true", help="print the report, write nothing")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    models = tuple(m.strip() for m in args.models.split(","))
    if len(models) != 3 or not all(models):
        print("--models needs three comma-separated names", file=sys.stderr)
        return 2
    frames, no_frame = load_frames(args.shadow_log, args.shadow2_log, models)
    images = {**dataset_images(args.image_dir), **find_images(args.disagree_dir)}
    labeled = labeled_hashes(args.labels)
    chosen, stats = select(frames, images, labeled, high=args.high, low=args.low,
                           small_height=args.small_height,
                           small_min_score=args.small_min_score, limit=args.limit,
                           source_share=args.source_share, control=args.control,
                           seed=args.seed)
    stats["no_frame"] = no_frame
    text = report(chosen, stats, args)
    if args.dry_run:
        print(text)
        return 0
    write_output(args.out, chosen, models, args.batch)
    (Path(args.out) / "selection-report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
