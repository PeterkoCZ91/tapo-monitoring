#!/usr/bin/env python3
"""Build a labeling set from the logs of two to four shadow targets (see docs/operations.md).

Joins ``shadow.jsonl``, ``shadow2.jsonl`` and the optional third and fourth logs on ``frame``
(the first 16 hex digits of the body's SHA-256); a tiled target scores
``max(shadow_person, shadow_tile_person)``, keeps only frames whose image survives in a ``disagree/`` directory or a
dataset directory (``--image-dir``), drops frames already labeled by hand in ``labels.jsonl``
(matched by the image's SHA-256; teacher labels do not count) and picks a capped,
per-source-balanced set:

* ``strong``     one model >= --high and another < --low
* ``threshold``  the models split at --high (and are not ``strong``)
* ``missed``     primary < --missed-primary while the second model (--models order) scores
                 >= --missed-shadow, optionally with its best box centre inside
                 --missed-region; checked first, quota --missed
* ``small``      the best box of any model is shorter than --small-height of the frame
* ``control_person`` / ``control_empty``  random samples where all models agree; reserved
  within ``--limit`` before the others, ``--source-share`` caps only the others

``threshold``, ``missed`` (up to ``--missed``) and ``small`` (up to ``--small``) are picked
before ``strong``, which takes
the rest of the limit.

Output: ``OUT/disagree<N>/*.jpg``, ``OUT/index.jsonl`` (``file``, ``ts``, ``person``,
``camera`` when a dataset index names it for the frame's source, and ``boxes`` as
``[x1, y1, x2, y2, score, model]``), ``OUT/teacher.jsonl`` and
``OUT/selection-report.md``. Source ids appear only as short hashes. Stdlib only; inputs
are only read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"^(\d{8}T\d{6})(\d{3})_p\d+(?:\.\d+)?_s\d+(?:\.\d+)?(?:-\d+)?\.jpe?g$", re.I)
CATEGORIES = ("strong", "threshold", "missed", "small", "control_person", "control_empty")
DISAGREEING = ("strong", "threshold")
CONTROLS = ("control_person", "control_empty")
DEFAULT_MODELS = ("yolox640", "rfdetr", "yolox960", "rfdetr_m896", "rfdetr_m576_t2")
# Label values of a hand label (tapo_monitor.labeling.LABELS; the tool stays stdlib-only).
HAND_LABELS = ("person", "no_person", "unsure")


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
    return float(value) if math.isfinite(value) else None


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


def shadow_score(rec):
    """The target's person score; a tiled target also reports its best tile."""
    s, t = num(rec.get("shadow_person")), num(rec.get("shadow_tile_person"))
    if s is None or t is None:
        return s
    return max(s, t)


def load_frames(logs, models):
    """``({frame: {"source", "scores", "boxes"}}, rows without a frame)``; last row wins.

    ``logs[i]`` is the log of the target named ``models[i + 1]``.
    """
    frames = {}
    missing = 0
    for path, shadow_model in zip(logs, models[1:], strict=True):
        for rec in read_jsonl(path):
            frame = rec.get("frame")
            if not isinstance(frame, str) or not frame:
                missing += 1
                continue
            item = frames.setdefault(frame, {"source": rec.get("source_id"), "scores": {},
                                             "boxes": {}, "primary": models[0]})
            if item["source"] is None:
                item["source"] = rec.get("source_id")
            p, s = num(rec.get("primary_person")), shadow_score(rec)
            if p is not None:
                item["scores"][models[0]] = p
                item["boxes"][models[0]] = clean_boxes(rec.get("primary_boxes"), models[0])
            if s is not None and not rec.get("error"):
                item["scores"][shadow_model] = s
                item["boxes"][shadow_model] = clean_boxes(rec.get("shadow_boxes"), shadow_model)
    return frames, missing


def dataset_images(dirs):
    """``{frame: (path, ts, sha256, kind, camera)}`` for every JPEG under the dataset dirs.

    ``kind`` is the directory's base name (e.g. ``sent-log``); ``ts`` and ``camera`` come
    from the ``index.jsonl`` next to the file (else the file's mtime and None). The first
    copy of a hash wins.
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
                    camera = rec.get("camera")
                    stamps[(idx.parent, name)] = (ts, camera if isinstance(camera, str) else None)
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in (".jpg", ".jpeg") or not path.is_file():
                continue
            try:
                sha = sha256_file(path)
                ts, camera = stamps.get((path.parent, path.name), (path.stat().st_mtime, None))
            except OSError:
                continue
            found.setdefault(sha[:16], (path, ts, sha, kind, camera))
    return found


def find_images(dirs):
    """``{frame: (path, ts, sha256, "disagree", None)}`` from the disagree directories.

    The ``missed/`` subdirectory of each one is searched too; it may not exist.
    """
    found = {}
    for directory in [x for d in dirs for x in (Path(d), Path(d) / "missed")]:
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
            found.setdefault(sha[:16], (path, ts, sha, "disagree", None))
    return found


def labeled_hashes(path):
    """Hashes whose latest record is a hand label.

    Teacher labels (``"by": "auto:<model>"``) do not count: the selection exists to
    hand-check the models, and the teacher is one of the families being compared.
    """
    latest = {}
    for rec in read_jsonl(path):
        sha = rec.get("sha256")
        if isinstance(sha, str):
            latest[sha] = rec
    return {sha for sha, rec in latest.items()
            if rec.get("label") in HAND_LABELS
            and not str(rec.get("by") or "").startswith("auto")}


def best_box_height(item, min_score):
    best = None
    for boxes in item["boxes"].values():
        for _x1, y1, _x2, y2, score, _model in boxes:
            if score >= min_score and (best is None or score > best[0]):
                best = (score, y2 - y1)
    return None if best is None else best[1]


def is_missed(item, shadow_model, primary_max, shadow_min, region):
    """Primary below ``primary_max`` while ``shadow_model`` scores >= ``shadow_min``.

    With a ``region`` (x1, y1, x2, y2) the centre of the shadow's best box must lie in it.
    """
    p, s = item["scores"].get(item["primary"]), item["scores"].get(shadow_model)
    if p is None or s is None or p >= primary_max or s < shadow_min:
        return False
    if region is None:
        return True
    boxes = item["boxes"].get(shadow_model) or []
    if not boxes:
        return False
    x1, y1, x2, y2, _score, _model = max(boxes, key=lambda b: b[4])
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    return region[0] <= cx <= region[2] and region[1] <= cy <= region[3]


def categorize(item, high, low, small_height, small_min_score, missed=None):
    """One category per frame (priority order), plus the score spread for ordering.

    ``missed`` is ``(shadow_model, primary_max, shadow_min, region)`` or None (off).
    """
    scores = list(item["scores"].values())
    if len(scores) < 2:
        return None, 0.0
    spread = max(scores) - min(scores)
    if missed and is_missed(item, *missed):
        return "missed", spread
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
           source_share, control, seed, small=20, missed=30, missed_args=None):
    rng = random.Random(seed)
    stats = {"frames": len(frames), "with_image": 0, "labeled_skipped": 0, "no_category": 0}
    pool = defaultdict(list)
    seen_cameras = defaultdict(Counter)
    for frame, item in sorted(frames.items()):
        if frame not in images:
            continue
        stats["with_image"] += 1
        path, ts, sha, kind, camera = images[frame]
        if camera:
            seen_cameras[item["source"]][camera] += 1
        if sha in labeled:
            stats["labeled_skipped"] += 1
            continue
        category, spread = categorize(item, high, low, small_height, small_min_score,
                                      missed_args)
        if category is None:
            stats["no_category"] += 1
            continue
        pool[category].append({"frame": frame, "item": item, "source": item["source"],
                               "path": path, "ts": ts, "sha": sha, "spread": spread,
                               "category": category, "kind": kind})
    # Controls are reserved off the top: they come mostly from the busiest sources, which
    # the disagreeing categories would otherwise fill up to the source cap first.
    controls = []
    for category in CONTROLS:
        controls += pick(pool[category], control, control, Counter(), limit - len(controls),
                         rng, False)
    rest = limit - len(controls)
    cap = max(1, int(rest * source_share))
    used = Counter()
    chosen = []
    # The rare categories go first (small up to --small), so the plentiful strong
    # disagreements take only what is left instead of crowding them out.
    quotas = {"small": small, "missed": missed}
    for category in ("threshold", "missed", "small", "strong"):
        quota = quotas.get(category, rest)
        chosen += pick(pool[category], quota, cap, used, rest - len(chosen), rng,
                       category in DISAGREEING)
    chosen += controls
    # A source is one camera: a frame without its own camera (disagree/) takes the camera
    # its source's dataset frames name most often.
    cameras = {src: cnt.most_common(1)[0][0] for src, cnt in seen_cameras.items()}
    for c in chosen:
        c["camera"] = cameras.get(c["source"])
    stats["controls"], stats["cap"] = len(controls), cap
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
        if c.get("camera"):
            rec["camera"] = c["camera"]
        rec["models"] = [m for m in models if m in scores]
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
             f"Selected: {len(chosen)} (limit {args.limit}, controls reserved "
             f"{stats['controls']}, per-source cap of the rest {stats['cap']})",
             f"Thresholds: high {args.high}, low {args.low}, small box height < "
             f"{args.small_height}, missed primary < {args.missed_primary} and shadow >= "
             f"{args.missed_shadow} (region {args.missed_region or 'any'})", "", "## By category", "",
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


def parse_region(text):
    try:
        vals = tuple(float(v) for v in text.split(","))
    except ValueError:
        vals = ()
    if len(vals) != 4:
        raise argparse.ArgumentTypeError("expected x1,y1,x2,y2")
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in vals):
        raise argparse.ArgumentTypeError("region coordinates must be finite and within [0, 1]")
    if vals[0] >= vals[2] or vals[1] >= vals[3]:
        raise argparse.ArgumentTypeError("region needs x1 < x2 and y1 < y2")
    return vals


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--shadow-log", required=True, help="shadow.jsonl of the first target")
    ap.add_argument("--shadow2-log", required=True, help="shadow2.jsonl of the second target")
    ap.add_argument("--shadow3-log", help="shadow.jsonl of an optional third target")
    ap.add_argument("--shadow4-log", help="shadow.jsonl of an optional fourth target "
                                          "(needs --shadow3-log)")
    ap.add_argument("--disagree-dir", action="append", default=[],
                    help="disagree/ directory of a target (repeat for both)")
    ap.add_argument("--image-dir", action="append", default=[],
                    help="dataset directory searched recursively for JPEGs of frames missing "
                         "from disagree/ (repeat; e.g. sent-log, review-log)")
    ap.add_argument("--labels", help="labels.jsonl; frames labeled by hand in it are skipped "
                                     "(teacher labels, by=auto:*, are not)")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--batch", default="1", help="N in OUT/disagreeN (default 1)")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS),
                    help="model names for primary,shadow,shadow2[,shadow3[,shadow4]]; extra "
                         "names without a log are ignored (default %(default)s)")
    ap.add_argument("--limit", type=int, default=600, help="maximum selected frames")
    ap.add_argument("--source-share", type=float, default=0.25,
                    help="largest share of --limit one source may take (default 0.25)")
    ap.add_argument("--small", type=int, default=20,
                    help="most small-person frames, picked before the strong disagreements "
                         "(default 20)")
    ap.add_argument("--missed", type=int, default=30,
                    help="most missed-person frames (primary low, second model high), picked "
                         "before the strong disagreements (default 30)")
    ap.add_argument("--missed-primary", type=float, default=0.10,
                    help="missed: the primary scores below this (default 0.10)")
    ap.add_argument("--missed-shadow", type=float, default=0.25,
                    help="missed: the second model scores at least this (default 0.25)")
    ap.add_argument("--missed-region", type=parse_region, default=None,
                    help="x1,y1,x2,y2 (normalized): the second model's best box centre must "
                         "lie inside (default: anywhere)")
    ap.add_argument("--control", type=int, default=20,
                    help="random frames per control category, reserved within --limit "
                         "(default 20)")
    ap.add_argument("--high", type=float, default=0.45)
    ap.add_argument("--low", type=float, default=0.25)
    ap.add_argument("--small-height", type=float, default=0.10)
    ap.add_argument("--small-min-score", type=float, default=0.25,
                    help="a box needs this score to count as the best box (default 0.25)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true", help="print the report, write nothing")
    args = ap.parse_args(argv)
    if args.missed < 0:
        ap.error("--missed must be nonnegative")
    for option in ("missed_primary", "missed_shadow"):
        value = getattr(args, option)
        if not math.isfinite(value) or not 0 <= value <= 1:
            ap.error(f"--{option.replace('_', '-')} must be finite and within [0, 1]")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.shadow4_log and not args.shadow3_log:
        print("--shadow4-log needs --shadow3-log", file=sys.stderr)
        return 2
    logs = [args.shadow_log, args.shadow2_log, args.shadow3_log, args.shadow4_log]
    logs = [p for p in logs if p]
    models = tuple(m.strip() for m in args.models.split(","))[:len(logs) + 1]
    if len(models) != len(logs) + 1 or not all(models):
        print(f"--models needs {len(logs) + 1} comma-separated names "
              f"(primary + one per log)", file=sys.stderr)
        return 2
    frames, no_frame = load_frames(logs, models)
    images = {**dataset_images(args.image_dir), **find_images(args.disagree_dir)}
    labeled = labeled_hashes(args.labels)
    chosen, stats = select(frames, images, labeled, high=args.high, low=args.low,
                           small_height=args.small_height,
                           small_min_score=args.small_min_score, limit=args.limit,
                           source_share=args.source_share, control=args.control,
                           small=args.small, missed=args.missed,
                           missed_args=(models[1], args.missed_primary, args.missed_shadow,
                                        args.missed_region),
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
