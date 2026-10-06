"""Local frame-labeling tool: ground truth for the alert frames the fleet kept.

The sent log keeps what went out and the review log what corroboration held back
(:mod:`tapo_monitor.sentlog`), but neither says whether a person was really in the
frame. ``tapo-monitor label <dataset_dir>`` serves one page that shows those frames
one at a time and records a human verdict; ``tapo-monitor label-stats`` turns the
verdicts into a false-alarm rate, a miss count and the threshold that would have
separated the labeled frames best.

The dataset is whatever was copied off the hosts: every ``index.jsonl`` under
``dataset_dir`` (searched recursively) is read, a record with a ``verdict`` is a review
frame, anything else a sent frame. Images are only ever read. Verdicts are appended to
``<dataset_dir>/labels.jsonl`` keyed by the image's SHA-256, so the same frame copied
twice or re-collected later keeps its label; the latest line per hash wins, and undo is
one more line, never an edit.

The server binds ``127.0.0.1`` by default: it has no authentication, so serving it
beyond the host is the operator's explicit choice, as with the status endpoint. Only
frames listed in an index and resolving inside ``dataset_dir`` are ever served.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import math
import os
import random
import re
import statistics
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import incident, scorer, sentlog
from .labeling_i18n import DEFAULT_LANG, STRINGS, tr

log = logging.getLogger(__name__)

INDEX_NAME = "index.jsonl"
LABELS_NAME = "labels.jsonl"
# Teacher-model person scores written by tapo_monitor.autolabel, keyed by image hash.
TEACHER_NAME = "teacher.jsonl"
LABELS = ("person", "no_person", "unsure")
UNLABELED = "unlabeled"
GRAY_LOW = 0.30
GRAY_HIGH = 0.65
BANDS = ("gray", "low", "high", "none")
DEFAULT_PORT = 8791
DEFAULT_BIND = "127.0.0.1"


@dataclass(frozen=True)
class Frame:
    path: str            # relative to dataset_dir, '/'-separated
    sha256: str
    source: str          # "sent" or "review"
    camera: str | None
    ts: float | None
    score: float | None  # person score, when the record carried one
    verdict: str | None = None       # review frames: "hold", "drop", ...; None when sent
    sample_rate: float | None = None  # sampled drops: the fraction that was archived
    # Opt-in box labeling: None when the index record has no ``boxes`` key (whole-frame
    # labeling, as always); otherwise the detector boxes (x1, y1, x2, y2, score, model),
    # normalized 0-1, possibly none at all (the operator can still draw a missed one).
    boxes: tuple | None = None


def band(score):
    """Score band the queue and the stats are ordered by. Pure."""
    if score is None:
        return "none"
    if score < GRAY_LOW:
        return "low"
    if score > GRAY_HIGH:
        return "high"
    return "gray"


def _inside(root_real, path):
    real = os.path.realpath(path)
    return real != root_real and real.startswith(root_real + os.sep)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _sample_rate(value):
    rate = _number(value)
    return rate if rate is not None and 0 < rate <= 1 else None


def _index_records(root_real):
    """``(rel path, full path, record)`` for every JPEG record of every index, in order.

    ``rel`` is the name the labels use: relative to the dataset, symlinks resolved. The
    file itself need not exist any more; records resolving outside the dataset are
    dropped.
    """
    for dirpath, dirnames, filenames in os.walk(root_real):
        dirnames.sort()
        if INDEX_NAME not in filenames:
            continue
        for record in _read_jsonl(os.path.join(dirpath, INDEX_NAME)):
            name = record.get("file")
            if not isinstance(name, str) or not name.lower().endswith((".jpg", ".jpeg")):
                continue
            full = os.path.join(dirpath, name)
            if not _inside(root_real, full):
                continue
            rel = os.path.relpath(os.path.realpath(full), root_real).replace(os.sep, "/")
            yield rel, full, record


BOX_LABELS = ("person", "no_person")
BOX_SOURCES = ("model", "drawn")
MAX_BOXES = 200
MAX_MODEL_LEN = 32


def _unit(value):
    number = _number(value)
    return None if number is None or not math.isfinite(number) else min(1.0, max(0.0, number))


def _norm_box(values):
    """``[x1, y1, x2, y2]`` clamped to 0-1 and ordered, or None when it is not a box."""
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return None
    coords = [_unit(v) for v in values]
    if None in coords:
        return None
    x1, x2 = sorted(coords[0::2])
    y1, y2 = sorted(coords[1::2])
    return [x1, y1, x2, y2] if x2 > x1 and y2 > y1 else None


def parse_index_boxes(value):
    """The ``boxes`` field of an index record: ``[x1,y1,x2,y2,score,model]`` entries.

    None when the field is absent or not a list (the frame is labeled whole, as before);
    otherwise a tuple of the well-formed entries, dropping the others, so a collector's
    bad row cannot break the page.
    """
    if not isinstance(value, list):
        return None
    out = []
    for entry in value[:MAX_BOXES]:
        if not isinstance(entry, (list, tuple)) or len(entry) != 6:
            continue
        box = _norm_box(entry[:4])
        score = _unit(entry[4])
        model = entry[5]
        if box is None or score is None or not isinstance(model, str) or not model.strip():
            continue
        out.append((*box, score, model.strip()[:MAX_MODEL_LEN]))
    return tuple(out)


def parse_index_models(value):
    """The ``models`` field of an index record: names of the models that ran on the frame.

    None when absent (old format: only models with a box are known to have run), else the
    set of names, which may be empty or name models without any box.
    """
    if not isinstance(value, list):
        return None
    return frozenset(m.strip()[:MAX_MODEL_LEN] for m in value if isinstance(m, str) and m.strip())


def clean_box_labels(frame, boxes):
    """Validate the operator's box verdicts for ``frame`` into label-file entries.

    ``boxes`` is a list of ``{"label", "source", ...}``: ``source`` "model" names the
    frame's detector box by ``index`` (its coordinates, model and score come from the
    index, never from the client); "drawn" carries its own ``box``. Raises ValueError on
    anything malformed. Returns ``[{"box", "label", "source"[, "model", "score"]}]``.
    """
    if boxes is None:
        return []
    if not isinstance(boxes, list) or len(boxes) > MAX_BOXES:
        raise ValueError("boxes must be a list")
    out = []
    for item in boxes:
        if not isinstance(item, dict):
            raise ValueError("box must be an object")
        label, source = item.get("label"), item.get("source")
        if label not in BOX_LABELS or source not in BOX_SOURCES:
            raise ValueError("bad box label or source")
        if source == "model":
            index = item.get("index")
            known = frame.boxes or ()
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(known):
                raise ValueError("bad model box index")
            *coords, score, model = known[index]
            out.append({"box": coords, "label": label, "source": "model",
                        "model": model, "score": score})
        else:
            box = _norm_box(item.get("box"))
            if box is None:
                raise ValueError("bad drawn box")
            out.append({"box": box, "label": label, "source": "drawn"})
    return out


def suggest_label(boxes):
    """Whole-frame label the box verdicts imply, or None when they say nothing.

    Any person box makes the frame ``person``; boxes that are all ``no_person`` make it
    ``no_person``. Only a suggestion: a label the operator typed is never overridden.
    """
    labels = [b.get("label") for b in boxes or ()]
    if "person" in labels:
        return "person"
    return "no_person" if labels and all(x == "no_person" for x in labels) else None


def _verdict(record):
    return str(record["verdict"]) if "verdict" in record else None


def load_frames(dataset_dir):
    """Every indexed JPEG under ``dataset_dir``, one per image hash. Read-only.

    Records whose file is missing, unreadable, not a JPEG name or resolving outside
    the dataset (``..``, absolute names, symlinks) are skipped silently: a collected
    dataset is expected to have gaps where retention pruned a frame.
    """
    frames = []
    seen = set()
    for rel, full, record in _index_records(os.path.realpath(dataset_dir)):
        if not os.path.isfile(full):
            continue
        try:
            sha = _sha256(full)
        except OSError:
            continue
        if sha in seen:
            continue
        seen.add(sha)
        camera = record.get("camera")
        frames.append(Frame(
            path=rel, sha256=sha,
            source="review" if "verdict" in record else "sent",
            camera=str(camera) if camera else None,
            ts=_number(record.get("ts")),
            score=_number(record.get("person")),
            verdict=_verdict(record),
            sample_rate=_sample_rate(record.get("sample_rate")),
            boxes=parse_index_boxes(record.get("boxes")),
        ))
    return frames


def index_meta(dataset_dir):
    """``{rel path: {"ts", "verdict", "sample_rate"}}`` from the indexes, no hashing.

    What the stats need beyond a label line (which carries only path, score, camera and
    source): cheap enough to reread on every ``/stats`` request.
    """
    meta = {}
    for rel, _, record in _index_records(os.path.realpath(dataset_dir)):
        meta[rel] = {"ts": _number(record.get("ts")), "verdict": _verdict(record),
                     "sample_rate": _sample_rate(record.get("sample_rate")),
                     "index_boxes": parse_index_boxes(record.get("boxes")),
                     "index_models": parse_index_models(record.get("models"))}
    return meta


def latest_labels(dataset_dir):
    """``{sha256: latest label record}``; an undo back to unlabeled drops the hash."""
    latest = {}
    for record in _read_jsonl(os.path.join(dataset_dir, LABELS_NAME)):
        sha = record.get("sha256")
        if isinstance(sha, str):
            latest[sha] = record
    return {sha: rec for sha, rec in latest.items() if rec.get("label") in LABELS}


def teacher_scores(dataset_dir):
    """``{sha256: teacher person score}`` from ``teacher.jsonl``; latest line wins."""
    scores = {}
    for record in _read_jsonl(os.path.join(dataset_dir, TEACHER_NAME)):
        sha, person = record.get("sha256"), _number(record.get("person"))
        if isinstance(sha, str) and person is not None:
            scores[sha] = person
    return scores


def build_queue(frames, labeled, *, seed=None, low_sample=None, teacher=None):
    """Unlabeled frames in review order. Pure apart from the seeded shuffle.

    Frames a teacher model has scored come first, biggest disagreement with the
    production score first: after :mod:`tapo_monitor.autolabel` has labeled every
    frame both models agree on, what is left is exactly where one of them is wrong.
    Then the gray zone (the threshold's own uncertainty), a random sample of low scores
    (possible misses), high scores (possible false alarms, nearest the gray zone first)
    and frames without a score.
    """
    rng = random.Random(seed)
    teacher = teacher or {}
    disputed = []
    by_band = {name: [] for name in BANDS}
    for frame in frames:
        if frame.sha256 in labeled:
            continue
        if frame.sha256 in teacher:
            disputed.append(frame)
        else:
            by_band[band(frame.score)].append(frame)
    disputed.sort(key=lambda f: (-abs(teacher[f.sha256] - (0.5 if f.score is None
                                                           else f.score)), f.path))
    by_band["gray"].sort(key=lambda f: (f.ts or 0.0, f.path))
    low = sorted(by_band["low"], key=lambda f: f.path)
    rng.shuffle(low)
    if low_sample is not None:
        low = low[:max(0, int(low_sample))]
    by_band["high"].sort(key=lambda f: (f.score, f.path))
    by_band["none"].sort(key=lambda f: (f.ts or 0.0, f.path))
    return disputed + by_band["gray"] + low + by_band["high"] + by_band["none"]


class LabelSession:
    """One labeling run over a dataset: the queue, the label file, the undo stack.

    Thread-safe: the HTTP server is threaded and every mutation holds one lock.
    """

    def __init__(self, dataset_dir, *, seed=None, low_sample=None, night=None,
                 scorer_url=None, lang=DEFAULT_LANG, scorer_token_env="TAPO_SCORER_TOKEN"):
        self.dataset_dir = dataset_dir
        self.lang = lang if lang in STRINGS else DEFAULT_LANG
        self.scorer_url = scorer_url   # scoring service for the person-box overlay, or None
        self.scorer_token_env = scorer_token_env   # NAME of the env var with its token
        self._boxes: dict[str, dict | None] = {}
        self._boxes_lock = threading.Lock()
        self.night = night   # (camera, ts) -> bool for the /stats day/night split, or None
        self.root_real = os.path.realpath(dataset_dir)
        self.frames = {f.path: f for f in load_frames(dataset_dir)}
        self._labeled = latest_labels(dataset_dir)
        self._teacher = teacher_scores(dataset_dir)
        self._queue = build_queue(self.frames.values(), self._labeled,
                                  seed=seed, low_sample=low_sample, teacher=self._teacher)
        self._history: list[tuple] = []
        self._lock = threading.Lock()

    def pending(self):
        with self._lock:
            return list(self._queue)

    def next_frame(self):
        with self._lock:
            return self._queue[0] if self._queue else None

    def _frame(self, path):
        frame = self.frames.get(path)
        if frame is None:
            raise KeyError(path)
        return frame

    def _append(self, frame, label, now, *, undo=False, boxes=None):
        record = {"path": frame.path, "sha256": frame.sha256, "label": label,
                  "labeled_at": float(time.time() if now is None else now),
                  "score": frame.score, "camera": frame.camera, "source": frame.source}
        if boxes:
            record["boxes"] = boxes
        if undo:
            record["undo"] = True
        with open(os.path.join(self.dataset_dir, LABELS_NAME), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def label(self, path, label, *, now=None, boxes=None):
        """Append one verdict for ``path`` and drop it from the queue.

        ``boxes`` (box-labeled frames only, see :func:`clean_box_labels`) are stored beside
        the verdict. ``label=None`` takes the one the boxes suggest; an explicit label is
        always kept as given.
        """
        with self._lock:
            frame = self._frame(path)
            box_labels = clean_box_labels(frame, boxes)
            if label is None:
                label = suggest_label(box_labels)
            if label not in LABELS:
                raise ValueError(f"unknown label {label!r}")
            record = self._append(frame, label, now, boxes=box_labels)
            previous = self._labeled.get(frame.sha256)
            self._labeled[frame.sha256] = record
            position = next((i for i, f in enumerate(self._queue) if f.path == path), None)
            if position is not None:
                del self._queue[position]
            self._history.append(("label", frame, previous, position))
            return record

    def skip(self, path):
        """Move ``path`` to the back of the queue; nothing is written."""
        with self._lock:
            frame = self._frame(path)
            position = next((i for i, f in enumerate(self._queue) if f.path == path), None)
            if position is None:
                return
            del self._queue[position]
            self._queue.append(frame)
            self._history.append(("skip", frame, None, position))

    def undo(self, *, now=None):
        """Revert the last label or skip. A label undo is a new line, never an edit."""
        with self._lock:
            if not self._history:
                return None
            kind, frame, previous, position = self._history.pop()
            if kind == "skip":
                self._queue.remove(frame)
                self._queue.insert(position, frame)
                return frame
            restored = previous.get("label") if previous else UNLABELED
            record = self._append(frame, restored, now, undo=True,
                                  boxes=previous.get("boxes") if previous else None)
            if previous:
                self._labeled[frame.sha256] = record
            else:
                self._labeled.pop(frame.sha256, None)
                # Back to the front: the operator pressed undo to look at it again.
                self._queue.insert(0, frame)
            return frame

    def state(self):
        with self._lock:
            frame = self._queue[0] if self._queue else None
            view = _frame_view(frame)
            if view is not None:
                view["teacher"] = self._teacher.get(frame.sha256)
            return {"frame": view, "remaining": len(self._queue),
                    "labeled": len(self._labeled), "can_undo": bool(self._history)}

    def person_boxes(self, rel):
        """Person boxes the scorer sees in an indexed frame, cached per file.

        ``{"boxes", "scores", "w", "h"}`` (boxes in pixels of a ``w``x``h`` frame), or None
        when the overlay is off, the frame is unknown or the scorer fails. Never raises:
        labeling must work without the boxes. Failures are not cached.
        """
        if not self.scorer_url:
            return None
        full = self.image_path(rel)
        if full is None:
            return None
        with self._boxes_lock:
            if rel in self._boxes:
                return self._boxes[rel]
        try:
            result = scorer.score_image(self.scorer_url, full,
                                        token=scorer.resolve_token(self.scorer_token_env))
            boxes = result.get("person_boxes") if isinstance(result, dict) else None
            scores = result.get("person_scores") if isinstance(result, dict) else None
            w, h = result.get("w"), result.get("h")
            if (not isinstance(boxes, list) or not isinstance(scores, list)
                    or len(boxes) != len(scores) or not _number(w) or not _number(h)
                    or w <= 0 or h <= 0):
                return None
            out = {"boxes": [[float(v) for v in box] for box in boxes],
                   "scores": [float(v) for v in scores], "w": w, "h": h}
        except Exception as exc:  # noqa: BLE001 - a bad scorer answer must not break labeling
            log.warning("scorer overlay failed for %s: %s", rel, exc)
            return None
        with self._boxes_lock:
            self._boxes[rel] = out
        return out

    def image_path(self, rel):
        """Absolute path of an indexed frame inside the dataset, or None."""
        frame = self.frames.get(rel)
        if frame is None:
            return None
        full = os.path.join(self.root_real, *frame.path.split("/"))
        return full if _inside(self.root_real, full) and os.path.isfile(full) else None


def _frame_view(frame):
    if frame is None:
        return None
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(frame.ts)) if frame.ts else None
    return {"path": frame.path, "camera": frame.camera, "ts": frame.ts, "time": when,
            "source": frame.source, "verdict": frame.verdict, "score": frame.score,
            "band": band(frame.score),
            "boxes": None if frame.boxes is None else [list(b) for b in frame.boxes]}


# ── stats ────────────────────────────────────────────────────────────────────

# A slice's best threshold needs this many decided frames with a score, and this many of
# each class, before it reads as a recommendation. Below that it is marked "too few
# labels": on a handful of frames the minimum-error cut sits wherever one odd frame is.
MIN_SUPPORT_DECIDED = 20
MIN_SUPPORT_PER_CLASS = 5
DAY_NIGHT = ("day", "night", "unknown")


BOX_SCORE = 0.45      # a model box counts as a detection from this score
BOX_IOU = 0.3         # a drawn person is found when a detection overlaps it this much
SMALL_HEIGHT = 0.10   # "small person": drawn box height below this (frame fraction)


def iou(a, b):
    """Intersection over union of two ``[x1, y1, x2, y2]`` boxes. Pure."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _recall_block():
    return {"people": 0, "found": 0, "missed": 0, "small": 0, "small_found": 0,
            "small_missed": 0, "false_boxes": 0, "true_boxes": 0,
            "inferred": 0}


def box_stats(records, *, box_score=BOX_SCORE, box_iou=BOX_IOU):
    """Box verdicts of the labeled frames: per model and the hand-drawn ones.

    None when no label carries boxes. Records are label lines (latest per frame), so a
    relabeled or undone frame counts once, with its current boxes.
    """
    models: dict[str, dict[str, int]] = {}
    drawn = {"person": 0, "no_person": 0}
    recall: dict[str, dict[str, int]] = {}
    frames = 0
    for rec in records:
        boxes = rec.get("boxes")
        if not isinstance(boxes, list) or not boxes:
            continue
        counted = False
        index_boxes = rec.get("index_boxes") or ()
        # With a ``models`` field, exactly the models that ran count (one that found nothing
        # misses). Without it (old format) a model counts only if the index holds a box of
        # it, and its recall may be overstated.
        listed = rec.get("index_models")
        present = set(listed) if listed is not None else {b[5] for b in index_boxes}
        for item in boxes:
            if (isinstance(item, dict) and item.get("source") != "drawn"
                    and item.get("label") in BOX_LABELS):
                block = recall.setdefault(str(item.get("model") or "unknown"),
                                          _recall_block())
                score = _number(item.get("score"))
                if item["label"] == "no_person" and score is not None and score >= box_score:
                    block["false_boxes"] += 1
                elif item["label"] == "person":
                    block["true_boxes"] += 1
            if not (isinstance(item, dict) and item.get("source") == "drawn"
                    and item.get("label") == "person"):
                continue
            target = _norm_box(item.get("box"))
            if target is None:
                continue
            small = target[3] - target[1] < SMALL_HEIGHT
            for model in present:
                block = recall.setdefault(model, _recall_block())
                hit = any(b[5] == model and b[4] >= box_score
                          and iou(target, b[:4]) >= box_iou
                          for b in index_boxes)
                block["people"] += 1
                if listed is None:
                    block["inferred"] += 1
                block["found" if hit else "missed"] += 1
                if small:
                    block["small"] += 1
                    block["small_found" if hit else "small_missed"] += 1
        for item in boxes:
            if not isinstance(item, dict) or item.get("label") not in BOX_LABELS:
                continue
            counted = True
            if item.get("source") == "drawn":
                drawn[item["label"]] += 1
            else:
                name = str(item.get("model") or "unknown")
                row = models.setdefault(name, {"person": 0, "no_person": 0})
                row[item["label"]] += 1
        frames += counted
    if not frames:
        return None
    return {"frames": frames, "models": dict(sorted(models.items())), "drawn": drawn,
            "recall": dict(sorted(recall.items())),
            "params": {"score": box_score, "iou": box_iou, "small_height": SMALL_HEIGHT}}


def _rate(part, whole):
    return part / whole if whole else None


def _verdict_block(records):
    """Decided review frames of one verdict: persons among them, sampled ones scaled."""
    person = sum(1 for r in records if r["label"] == "person")
    block = {"person": person, "decided": len(records), "rate": _rate(person, len(records)),
             "sampled": sum(1 for r in records if r["sample_rate"]),
             "estimated_person": None, "estimated_decided": None}
    if block["sampled"]:
        # A sampled frame stands for 1/sample_rate dropped frames. An estimate, and one
        # that only covers the part of the sample that has been labeled.
        weights = [(r, 1 / r["sample_rate"] if r["sample_rate"] else 1.0) for r in records]
        block["estimated_person"] = sum(w for r, w in weights if r["label"] == "person")
        block["estimated_decided"] = sum(w for _, w in weights)
    return block


def _summarize(records):
    counts = {name: 0 for name in LABELS}
    fa_no = fa_decided = miss_yes = miss_decided = 0
    review = {}
    for rec in records:
        label = rec["label"]
        counts[label] += 1
        if label == "unsure":
            continue
        if rec.get("source") == "review":
            miss_decided += 1
            miss_yes += label == "person"
            review.setdefault(rec.get("verdict") or "unknown", []).append(rec)
        else:
            fa_decided += 1
            fa_no += label == "no_person"
    counts["total"] = len(records)
    return {
        "counts": counts,
        "false_alarms": {"no_person": fa_no, "decided": fa_decided,
                         "rate": _rate(fa_no, fa_decided)},
        "misses": {"person": miss_yes, "decided": miss_decided,
                   "rate": _rate(miss_yes, miss_decided)},
        # A person in a held frame is a hold error (corroboration waited on a real
        # subject); a person in a dropped frame is a real miss.
        "verdicts": {name: _verdict_block(review[name])
                     for name in sorted(review, key=lambda v: (v != "hold", v != "drop", v))},
    }


def best_threshold(pairs):
    """Threshold on ``score >= t`` with the fewest errors over ``(score, is_person)``.

    Candidates are the labeled scores themselves. Ties go to the lower threshold, i.e.
    fewer misses: a missed person costs more than one more photo to glance at.
    ``supported`` says whether there were enough labels of both classes to trust it.
    """
    pairs = [(float(s), bool(p)) for s, p in pairs]
    persons = sum(1 for _, p in pairs if p)
    result = {"threshold": None, "errors": None, "false_alarms": None, "misses": None,
              "accuracy": None, "n": len(pairs), "person": persons,
              "no_person": len(pairs) - persons,
              "supported": (len(pairs) >= MIN_SUPPORT_DECIDED
                            and min(persons, len(pairs) - persons) >= MIN_SUPPORT_PER_CLASS)}
    if not pairs:
        return result
    best = None
    for t in sorted({s for s, _ in pairs}):
        fp = sum(1 for s, p in pairs if s >= t and not p)
        fn = sum(1 for s, p in pairs if s < t and p)
        if best is None or fp + fn < best[1] + best[2]:
            best = (t, fp, fn)
    assert best is not None
    t, fp, fn = best
    result.update(threshold=t, errors=fp + fn, false_alarms=fp, misses=fn,
                  accuracy=1 - (fp + fn) / len(pairs))
    return result


def _best(records):
    return best_threshold((r["score"], r["label"] == "person") for r in records
                          if r["score"] is not None and r["label"] != "unsure")


def _slice(records):
    block = _summarize(records)
    block["threshold"] = _best(records)
    return block


def _camera(rec):
    return str(rec.get("camera") or "unknown")


def night_classifier(app, *, is_night=None):
    """``(camera, ts) -> bool``: night as the daemon would have judged it for a frame.

    Site night from :func:`tapo_monitor.replay.default_is_night` (the configured
    location, in the site's timezone; ``is_night(ts)`` replaces it, e.g. in tests), then
    the camera's own ``schedule`` via :func:`tapo_monitor.daemon.effective_night`. A
    camera missing from the config gets the site's night. Imported here, not at the top:
    only ``--config`` needs the daemon, the labeling page never does.
    """
    from . import daemon, replay

    site = is_night if is_night is not None else replay.default_is_night(app)
    cameras = {cfg.name: cfg for cfg in app.cameras}

    def night(camera, ts):
        astronomical = bool(site(ts))
        cfg = cameras.get(camera)
        return daemon.effective_night(cfg, astronomical) if cfg is not None else astronomical

    return night


def parse_camera_aliases(items):
    """``["OLD=NEW", ...]`` -> ``{"OLD": "NEW"}``; ValueError on a malformed item."""
    aliases = {}
    for item in items or ():
        old, sep, new = str(item).partition("=")
        old, new = old.strip(), new.strip()
        if not sep or not old or not new:
            raise ValueError(f"camera alias {item!r}: expected OLD=NEW")
        aliases[old] = new
    return aliases


def _aliased(camera, aliases):
    return aliases.get(camera, camera) if aliases and camera else camera


def compute_stats(dataset_dir, *, night=None, aliases=None, box_score=BOX_SCORE,
                  box_iou=BOX_IOU):
    """Label counts, false-alarm and miss estimates, per band/camera, best threshold.

    Review frames are also split by their index ``verdict`` (hold / drop). With
    ``night(camera, ts) -> bool`` (see :func:`night_classifier`) a ``day_night`` section
    repeats the summary and best threshold for day, night and frames without a time,
    and per camera for day and night. ``aliases`` (``{old: new}``) counts the records of a
    renamed camera under its new name in every table.
    """
    records = list(latest_labels(dataset_dir).values())
    meta = index_meta(dataset_dir)
    for rec in records:
        if aliases and rec.get("camera"):
            rec["camera"] = _aliased(str(rec["camera"]), aliases)
        rec["score"] = _number(rec.get("score"))
        # A label line carries no time or verdict; its frame's index record does.
        known = meta.get(rec.get("path"))
        if known is None:
            known = {"ts": _number(rec.get("ts")), "verdict": rec.get("verdict"),
                     "sample_rate": _sample_rate(rec.get("sample_rate")),
                     "index_boxes": None, "index_models": None}
        rec.update(known)
    stats = _summarize(records)
    stats["bands"] = {name: _summarize([r for r in records if band(r["score"]) == name])
                      for name in BANDS}
    cameras = sorted({_camera(r) for r in records})
    stats["cameras"] = {cam: _summarize([r for r in records if _camera(r) == cam])
                        for cam in cameras}
    auto = sum(1 for r in records if str(r.get("by") or "").startswith("auto"))
    stats["by"] = {"auto": auto, "human": len(records) - auto}
    stats["threshold"] = _best(records)
    boxes = box_stats(records, box_score=box_score, box_iou=box_iou)
    if boxes:
        stats["boxes"] = boxes
    if night is not None:
        for rec in records:
            rec["daypart"] = ("unknown" if rec["ts"] is None
                              else "night" if night(rec.get("camera"), rec["ts"]) else "day")
        parts = {name: _slice([r for r in records if r["daypart"] == name])
                 for name in DAY_NIGHT}
        parts["cameras"] = {
            cam: {name: _slice([r for r in records
                                if _camera(r) == cam and r["daypart"] == name])
                  for name in ("day", "night")}
            for cam in cameras}
        parts["min_support"] = {"decided": MIN_SUPPORT_DECIDED,
                                "per_class": MIN_SUPPORT_PER_CLASS}
        stats["day_night"] = parts
    stats["incidents"] = incident_stats(dataset_dir, night=night, aliases=aliases)
    return stats


# ── incidents ────────────────────────────────────────────────────────────────

# Without an incident ID, frames of one camera further apart than this start a new
# incident. Above the alert cooldown (120 s): a subject still in view when the cooldown
# ends is photographed again 120-150 s after the last alert, and that is the same visit.
# Above the sampler's group_gap (90 s), the daemon's own "same event group" rule; inside
# a visit the sampler grabs a frame every 30 s, so a longer silence means the scene went
# quiet. Two visits closer than this merge into one: that can hide a miss behind an
# alerted neighbour, never invent one, so the missed count errs low.
INCIDENT_GAP = 150.0
# Older sent records carry no event start, but their caption does: the daemon prints the
# camera's event start in the host's local time. It is read back in this machine's local
# time and trusted only up to this long before the frame, so a host in another timezone
# yields no start rather than a wrong one (real sends follow their event within minutes).
CAPTION_START_MAX_AGE = 3600.0
_CAPTION_TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
INCIDENT_STATUSES = ("person", "no_person", "unsure", "unlabeled")
REVIEW_VERDICTS = ("hold", "drop", "shadow")
# A sent frame from before the sent log named its delivery path.
UNKNOWN_PATH = "unknown"


def _incident_start(value):
    """The event start encoded in an incident ID (``<camera>-<start>``), or None."""
    try:
        return float(incident.parse(value)[1])
    except ValueError:
        return None


def caption_start(caption, ts):
    """The event start printed in a sent frame's caption as epoch seconds, or None.

    Pure apart from the local timezone.
    """
    found = _CAPTION_TIME.findall(caption) if isinstance(caption, str) else []
    if not found or ts is None:
        return None
    try:
        start = time.mktime(time.strptime(found[-1], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None
    return start if 0 <= ts - start <= CAPTION_START_MAX_AGE else None


def incident_frames(dataset_dir, labels=None, aliases=None):
    """Every indexed frame as a dict for :func:`group_incidents`, labeled or not. Read-only.

    ``host`` is the directory holding the host's logs (``<host>/sent-log/index.jsonl``).
    A record without ``camera`` (older hosts) takes the host's camera when every other
    record of that host names the same one, else None. ``labels`` is ``{path: label}``;
    no hashing, so it stays cheap enough for every ``/stats`` request. A sent record
    without ``delivered`` counts as delivered: the sent log writes False only when
    delivery failed. ``send_path`` is the delivery path a sent record names (its index
    ``path``), else None. ``aliases`` (``{old: new}``) renames cameras before the
    host's single camera is inferred.
    """
    labels = labels or {}
    frames = []
    for rel, _, record in _index_records(os.path.realpath(dataset_dir)):
        camera = record.get("camera")
        ident = record.get("incident")
        ts = _number(record.get("ts"))
        frames.append({
            "path": rel, "host": "/".join(rel.split("/")[:-2]),
            "camera": _aliased(str(camera), aliases) if camera else None,
            "ts": ts,
            "incident": str(ident) if ident else None,
            "event_start": _number(record.get("event_start")),
            "caption_start": caption_start(record.get("caption"), ts),
            "source": "review" if "verdict" in record else "sent",
            "verdict": _verdict(record),
            "delivered": "verdict" not in record and record.get("delivered", True) is not False,
            "send_path": (str(record["path"]) if "verdict" not in record and record.get("path")
                          else None),
            "label": labels.get(rel),
        })
    named = {}
    for frame in frames:
        if frame["camera"]:
            named.setdefault(frame["host"], set()).add(frame["camera"])
    for frame in frames:
        if frame["camera"] is None and len(named.get(frame["host"], ())) == 1:
            frame["camera"] = next(iter(named[frame["host"]]))
    return frames


def group_incidents(frames, gap=INCIDENT_GAP):
    """Frames grouped into incidents per host and camera. Pure.

    A frame with an ``incident`` ID joins that incident. A frame without one joins the
    camera's previous incident (with or without an ID) when it is at most ``gap`` seconds
    after that incident's last frame, else starts a new one. An ID frame arriving within
    ``gap`` of an incident that has no ID yet adopts it: in a period where only some
    paths wrote IDs, the same visit is not counted once per path. Frames with neither a
    time nor an ID cannot be placed and are left out. Returns ``[{"id", "host",
    "camera", "frames"}]``.
    """
    cameras = {}
    for frame in frames:
        if frame["ts"] is None and not frame["incident"]:
            continue
        cameras.setdefault((frame["host"], frame["camera"] or ""), []).append(frame)
    out = []
    for (host, camera), items in sorted(cameras.items()):
        items.sort(key=lambda f: (f["ts"] is None, f["ts"] or 0.0))

        def new(ident, host=host, camera=camera):
            out.append({"id": ident, "host": host, "camera": camera or None,
                        "frames": [], "last": None})
            return out[-1]

        by_id = {}
        current = None
        for frame in items:
            ts, ident = frame["ts"], frame["incident"]
            near = (current is not None and ts is not None and current["last"] is not None
                    and ts - current["last"] <= gap)
            if ident:
                inc = by_id.get(ident)
                if inc is None and near and current["id"] is None:
                    inc = current
                    inc["id"] = ident
                elif inc is None:
                    inc = new(ident)
                by_id[ident] = inc
            else:
                inc = current if near else new(None)
            inc["frames"].append(frame)
            if ts is not None:
                inc["last"] = ts if inc["last"] is None else max(inc["last"], ts)
            current = inc
    for inc in out:
        del inc["last"]
    return out


def summarize_incident(inc):
    """Status, alert and timing of one incident from :func:`group_incidents`. Pure.

    ``status``: ``person`` when any frame is labeled person, ``no_person`` when every
    labeled frame is no_person, ``unsure`` when labeled otherwise, else ``unlabeled``.
    ``alerted``: a delivered sent frame. ``start``: the camera's event start (a frame's
    ``event_start``, else the one in the incident ID, else the earliest caption time),
    else the first frame's time; ``start_source`` says which. ``delay`` runs from there
    to the first delivered sent frame, and ``first_path`` is the delivery path that sent
    that frame (``unknown`` when its record names none; None when not alerted).
    """
    frames = inc["frames"]
    labels = [f["label"] for f in frames if f["label"]]
    if "person" in labels:
        status = "person"
    elif labels and all(label == "no_person" for label in labels):
        status = "no_person"
    else:
        status = "unsure" if labels else "unlabeled"
    event = [f["event_start"] for f in frames if f["event_start"] is not None]
    captions = [f["caption_start"] for f in frames if f.get("caption_start") is not None]
    times = [f["ts"] for f in frames if f["ts"] is not None]
    start, source = None, None
    for candidate, name in ((min(event) if event else None, "event"),
                            (_incident_start(inc["id"]) if inc["id"] else None, "incident_id"),
                            (min(captions) if captions else None, "caption"),
                            (min(times) if times else None, "first_frame")):
        if candidate is not None:
            start, source = candidate, name
            break
    alerts = [f for f in frames if f["source"] == "sent" and f["delivered"]]
    alert_times = [f["ts"] for f in alerts if f["ts"] is not None]
    delay = min(alert_times) - start if alert_times and start is not None else None
    first_path = None
    if alerts:
        first = min(alerts, key=lambda f: (f["ts"] is None, f["ts"] or 0.0))
        first_path = first.get("send_path") or UNKNOWN_PATH
    verdicts = {}
    for frame in frames:
        if frame["source"] == "review":
            entry = verdicts.setdefault(frame["verdict"] or "unknown",
                                        {"frames": 0, "person": 0})
            entry["frames"] += 1
            entry["person"] += frame["label"] == "person"
    return {"id": inc["id"], "host": inc["host"], "camera": inc["camera"],
            "start": start, "start_source": source, "status": status,
            "frames": len(frames), "labeled": len(labels), "alerted": bool(alerts),
            "delay": delay, "first_path": first_path, "verdicts": verdicts}


def _percentile(values, fraction):
    """Nearest-rank percentile of sorted ``values``, or None when empty."""
    if not values:
        return None
    return values[max(0, math.ceil(fraction * len(values)) - 1)]


def _delay_block(incidents):
    """``n``, ``from_event_start``, median and p90 of the incidents' alert delays. Pure."""
    timed = [i for i in incidents if i["delay"] is not None]
    delays = sorted(i["delay"] for i in timed)
    return {"n": len(delays),
            "from_event_start": sum(1 for i in timed if i["start_source"] != "first_frame"),
            "median": statistics.median(delays) if delays else None,
            "p90": _percentile(delays, 0.9)}


def _path_order(name):
    """Sort key: the daemon's delivery paths in their order, others by name, unknown last."""
    if name in sentlog.SEND_PATHS:
        return (0, sentlog.SEND_PATHS.index(name), name)
    return (2 if name == UNKNOWN_PATH else 1, 0, name)


def _first_alert_block(incidents):
    """Per delivery path of the first delivered frame: how many alerted incidents it
    opened, their share and their delay from event start. Over every alerted incident,
    labeled or not: which path is late does not depend on a label. Pure."""
    alerted = [i for i in incidents if i["alerted"]]
    paths = {}
    for inc in alerted:
        paths.setdefault(inc.get("first_path") or UNKNOWN_PATH, []).append(inc)
    return {name: {"incidents": len(paths[name]),
                   "share": _rate(len(paths[name]), len(alerted)),
                   "delay": _delay_block(paths[name])}
            for name in sorted(paths, key=_path_order)}


def _incident_block(incidents):
    """Counts, miss and false-alarm rates and alert delay over some incidents. Pure."""
    counts = {name: 0 for name in INCIDENT_STATUSES}
    for inc in incidents:
        counts[inc["status"]] += 1
    person = [i for i in incidents if i["status"] == "person"]
    missed = [i for i in person if not i["alerted"]]
    alerted = [i for i in incidents if i["alerted"]]
    decided = [i for i in alerted if i["status"] in ("person", "no_person")]
    false_alarms = sum(1 for i in decided if i["status"] == "no_person")
    # Which review verdicts the frames of missed incidents had: a person in a held frame
    # means the hold swallowed the visit, one only in dropped frames the threshold did.
    verdicts = {}
    for inc in missed:
        names = inc["verdicts"] or {"none": {"frames": 0, "person": 0}}
        for name, entry in names.items():
            block = verdicts.setdefault(name, {"incidents": 0, "frames": 0,
                                               "person_incidents": 0})
            block["incidents"] += 1
            block["frames"] += entry["frames"]
            block["person_incidents"] += entry["person"] > 0
    order = {name: i for i, name in enumerate(REVIEW_VERDICTS)}
    return {
        "total": len(incidents), "counts": counts,
        "labeled": len(incidents) - counts["unlabeled"],
        "alerted": len(alerted),
        "person_alerted": len(person) - len(missed),
        "missed": {"count": len(missed), "person": len(person),
                   "rate": _rate(len(missed), len(person))},
        "false_alarms": {"count": false_alarms, "decided": len(decided),
                         "rate": _rate(false_alarms, len(decided))},
        "delay": _delay_block([i for i in person if i["alerted"]]),
        "missed_verdicts": {name: verdicts[name] for name in
                            sorted(verdicts, key=lambda v: (order.get(v, len(order)), v))},
        "first_alert": _first_alert_block(incidents),
    }


def _daypart(night, inc):
    if inc["start"] is None:
        return "unknown"
    return "night" if night(inc["camera"], inc["start"]) else "day"


def incident_stats(dataset_dir, *, night=None, gap=INCIDENT_GAP, aliases=None):
    """Quality per incident rather than per frame: the ``incidents`` stats section.

    Every indexed frame counts, labeled or not (a delivery matters even when nobody
    labeled its frame). Overall, per camera and, with ``night`` (see
    :func:`night_classifier`, applied to the incident start), per day and night; plus
    the missed person incidents themselves.
    """
    labels = {rec.get("path"): rec["label"] for rec in latest_labels(dataset_dir).values()}
    frames = incident_frames(dataset_dir, labels, aliases)
    incidents = [summarize_incident(inc) for inc in group_incidents(frames, gap)]
    result = {"gap_seconds": gap, "frames": len(frames), "all": _incident_block(incidents)}
    cameras = sorted({_camera(i) for i in incidents})
    result["cameras"] = {cam: _incident_block([i for i in incidents if _camera(i) == cam])
                         for cam in cameras}
    if night is not None:
        for inc in incidents:
            inc["daypart"] = _daypart(night, inc)
        result["day_night"] = {name: _incident_block([i for i in incidents
                                                      if i["daypart"] == name])
                               for name in DAY_NIGHT}
    result["missed"] = [
        {key: inc[key] for key in ("id", "host", "camera", "start", "frames", "verdicts")}
        for inc in sorted(incidents, key=lambda i: (i["start"] or 0.0, _camera(i)))
        if inc["status"] == "person" and not inc["alerted"]]
    return result


def _pct(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _part(name, lang):
    """Display name of a day/night slice (``unknown`` included)."""
    return tr(lang, "d_" + name)


def _stats_rows(stats, lang=DEFAULT_LANG):
    """``(name, labeled, person, no_person, unsure, false alarms, misses)`` rows."""
    rows = []
    groups = [(tr(lang, "r_all"), stats)]
    groups += [(tr(lang, "r_band", name=name), stats["bands"][name]) for name in BANDS]
    groups += [(tr(lang, "r_camera", name=name), block)
               for name, block in stats["cameras"].items()]
    for name, block in groups:
        c, fa, miss = block["counts"], block["false_alarms"], block["misses"]
        rows.append((name, str(c["total"]), str(c["person"]), str(c["no_person"]),
                     str(c["unsure"]),
                     f"{fa['no_person']}/{fa['decided']} ({_pct(fa['rate'])})",
                     f"{miss['person']}/{miss['decided']} ({_pct(miss['rate'])})"))
    return rows


def _headers(lang, *keys):
    return tuple(tr(lang, "h_" + key) for key in keys)


def stats_headers(lang=DEFAULT_LANG):
    return _headers(lang, "group", "labeled", "person", "no_person", "unsure",
                    "false_alarms_sent", "misses_review")


def day_night_headers(lang=DEFAULT_LANG):
    return _headers(lang, "slice", "n", "person", "no_person", "best_threshold", "errors",
                    "false_alarms", "misses", "note")


def incident_headers(lang=DEFAULT_LANG):
    return _headers(lang, "group", "incidents_col", "labeled", "person", "person_alerted",
                    "missed", "false_alarms", "delay_median", "delay_p90")


def first_alert_headers(lang=DEFAULT_LANG):
    return _headers(lang, "group", "path", "first_alerts", "share", "delay_n",
                    "delay_median", "delay_p90")


def _threshold_line(best, lang=DEFAULT_LANG):
    if best["threshold"] is None:
        return tr(lang, "threshold_na")
    return tr(lang, "threshold_line", threshold=best["threshold"], errors=best["errors"],
              n=best["n"], false_alarms=best["false_alarms"], misses=best["misses"],
              accuracy=_pct(best["accuracy"]))


def _verdict_lines(stats, lang=DEFAULT_LANG):
    """Misses by review verdict; nothing while every labeled review frame was a hold."""
    verdicts = stats.get("verdicts") or {}
    if not set(verdicts) - {"hold", "unknown"}:
        return []
    out = [tr(lang, "verdict_head")]
    for name, block in verdicts.items():
        text = f"  {name}: {block['person']}/{block['decided']} ({_pct(block['rate'])})"
        if block["sampled"]:
            text += tr(lang, "verdict_sampled", sampled=block["sampled"],
                       est_person=block["estimated_person"],
                       est_decided=block["estimated_decided"])
        out.append(text)
    return out


def _box_headers(lang=DEFAULT_LANG):
    return _headers(lang, "box_model", "box_person", "box_no_person", "box_total")


def _box_rows(boxes, lang=DEFAULT_LANG):
    rows = [[name, str(b["person"]), str(b["no_person"]), str(b["person"] + b["no_person"])]
            for name, b in boxes["models"].items()]
    d = boxes["drawn"]
    rows.append([tr(lang, "box_drawn"), str(d["person"]), str(d["no_person"]),
                 str(d["person"] + d["no_person"])])
    return rows


def _box_recall_headers(lang=DEFAULT_LANG):
    return _headers(lang, "box_model", "box_people", "box_found", "box_missed", "box_recall",
                    "box_small", "box_small_missed", "box_false")


def _box_recall_rows(boxes, lang=DEFAULT_LANG):
    rows = []
    for name, b in boxes["recall"].items():
        rows.append([name + (" *" if b["inferred"] else ""), str(b["people"]), str(b["found"]), str(b["missed"]),
                     _pct(_rate(b["found"], b["people"])), str(b["small"]),
                     str(b["small_missed"]), str(b["false_boxes"])])
    return rows


def _box_recall_note(boxes, lang=DEFAULT_LANG):
    p = boxes["params"]
    if any(b["inferred"] for b in boxes["recall"].values()):
        return tr(lang, "box_recall_note", score=p["score"], iou=p["iou"],
                  small=p["small_height"]) + " " + tr(lang, "box_recall_inflated")
    return tr(lang, "box_recall_note", score=p["score"], iou=p["iou"], small=p["small_height"])


def _box_summary(boxes, lang=DEFAULT_LANG):
    return tr(lang, "box_summary", frames=boxes["frames"])


def _day_night_row(name, best, lang=DEFAULT_LANG):
    def cell(value):
        return "-" if value is None else str(value)

    return (name, str(best["n"]), str(best["person"]), str(best["no_person"]),
            "n/a" if best["threshold"] is None else f"{best['threshold']:.2f}",
            cell(best["errors"]), cell(best["false_alarms"]), cell(best["misses"]),
            "" if best["supported"] else tr(lang, "too_few"))


def _day_night_rows(stats, lang=DEFAULT_LANG):
    parts = stats["day_night"]
    rows = [_day_night_row(tr(lang, "r_all"), stats["threshold"], lang)]
    rows += [_day_night_row(_part(name, lang), parts[name]["threshold"], lang)
             for name in DAY_NIGHT if name != "unknown" or parts[name]["counts"]["total"]]
    for cam, by_part in parts["cameras"].items():
        rows += [_day_night_row(tr(lang, "r_camera_part", cam=cam, name=_part(name, lang)),
                                by_part[name]["threshold"], lang)
                 for name in ("day", "night") if by_part[name]["counts"]["total"]]
    return rows


def _day_night_summary(stats, lang=DEFAULT_LANG):
    parts = stats["day_night"]
    counts = ", ".join(f"{parts[name]['counts']['total']} {_part(name, lang)}"
                       for name in DAY_NIGHT)
    return tr(lang, "day_night_summary", counts=counts)


def _day_night_note(lang=DEFAULT_LANG):
    return tr(lang, "day_night_note", too_few=tr(lang, "too_few"),
              decided=MIN_SUPPORT_DECIDED, per_class=MIN_SUPPORT_PER_CLASS)


def _table(headers, rows):
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]

    def line(values):
        return "  ".join(v.ljust(widths[i]) for i, v in enumerate(values)).rstrip()

    return [line(headers), line(tuple("-" * w for w in widths))] + [line(r) for r in rows]


def _seconds(value):
    return "-" if value is None else f"{value:.0f} s"


def _incident_row(name, block):
    miss, fa, delay = block["missed"], block["false_alarms"], block["delay"]
    return (name, str(block["total"]), str(block["labeled"]), str(block["counts"]["person"]),
            str(block["person_alerted"]),
            f"{miss['count']}/{miss['person']} ({_pct(miss['rate'])})",
            f"{fa['count']}/{fa['decided']} ({_pct(fa['rate'])})",
            _seconds(delay["median"]), _seconds(delay["p90"]))


def _incident_rows(section, lang=DEFAULT_LANG):
    rows = [_incident_row(tr(lang, "r_all"), section["all"])]
    rows += [_incident_row(tr(lang, "r_camera", name=cam), block)
             for cam, block in section["cameras"].items()]
    parts = section.get("day_night") or {}
    rows += [_incident_row(_part(name, lang), parts[name]) for name in DAY_NIGHT
             if name in parts and (name != "unknown" or parts[name]["total"])]
    return rows


def _incident_summary(section, lang=DEFAULT_LANG):
    block = section["all"]
    return tr(lang, "incident_summary", total=block["total"], frames=section["frames"],
              gap=section["gap_seconds"], labeled=block["labeled"])


def _incident_notes(section, lang=DEFAULT_LANG):
    """Explanations and the verdicts of missed incidents, one line each."""
    block = section["all"]
    delay = block["delay"]
    out = [tr(lang, "incident_note_1"),
           tr(lang, "incident_note_delay", n=delay["n"], timed=delay["from_event_start"])]
    verdicts = block["missed_verdicts"]
    if verdicts:
        parts = []
        for name, entry in verdicts.items():
            if name == "none":
                parts.append(tr(lang, "missed_none", incidents=entry["incidents"]))
            else:
                parts.append(tr(lang, "missed_verdict", name=name,
                                incidents=entry["incidents"],
                                person=entry["person_incidents"]))
        out.append(tr(lang, "missed_head") + ", ".join(parts))
    return out


def _first_alert_rows(section, lang=DEFAULT_LANG):
    """``(group, path, incidents, share, n, median, p90)`` rows; groups without an alert
    are left out."""
    groups = [(tr(lang, "r_all"), section["all"])]
    groups += [(tr(lang, "r_camera", name=cam), block)
               for cam, block in section["cameras"].items()]
    parts = section.get("day_night") or {}
    groups += [(_part(name, lang), parts[name]) for name in DAY_NIGHT if name in parts]
    rows = []
    for name, block in groups:
        for path, entry in block.get("first_alert", {}).items():
            delay = entry["delay"]
            rows.append((name, path, str(entry["incidents"]), _pct(entry["share"]),
                         str(delay["n"]), _seconds(delay["median"]), _seconds(delay["p90"])))
    return rows


def _first_alert_summary(lang=DEFAULT_LANG):
    return tr(lang, "first_alert_summary")


def format_incidents(section, lang=DEFAULT_LANG):
    out = [_incident_summary(section, lang) + ":"]
    if not section["all"]["labeled"]:
        out.append(tr(lang, "no_incident_text"))
    else:
        out += _table(incident_headers(lang), _incident_rows(section, lang))
        out += _incident_notes(section, lang)
    rows = _first_alert_rows(section, lang)
    if rows:
        out += ["", _first_alert_summary(lang) + ":"]
        out += _table(first_alert_headers(lang), rows)
    return out


def format_stats(stats, lang=DEFAULT_LANG):
    out = _table(stats_headers(lang), _stats_rows(stats, lang))
    by = stats.get("by") or {}
    out += ["", tr(lang, "labels_by", human=by.get("human", 0), auto=by.get("auto", 0)),
            _threshold_line(stats["threshold"], lang),
            tr(lang, "stats_footnote_text")]
    out += _verdict_lines(stats, lang)
    if "boxes" in stats:
        out += ["", _box_summary(stats["boxes"], lang) + ":"]
        out += _table(_box_headers(lang), _box_rows(stats["boxes"], lang))
        if stats["boxes"]["recall"]:
            out += ["", tr(lang, "box_recall_head") + ":"]
            out += _table(_box_recall_headers(lang), _box_recall_rows(stats["boxes"], lang))
            out.append(_box_recall_note(stats["boxes"], lang))
    if "day_night" in stats:
        out += ["", _day_night_summary(stats, lang) + ":"]
        out += _table(day_night_headers(lang), _day_night_rows(stats, lang))
        out.append(_day_night_note(lang))
    if "incidents" in stats:
        out += [""] + format_incidents(stats["incidents"], lang)
    return "\n".join(out)


# ── HTTP ─────────────────────────────────────────────────────────────────────

PAGE_TEMPLATE = """<!doctype html>
<html lang="@@lang@@"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@page_title@@</title>
<style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#ddd}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecece8;--muted:#9a9a94;
--card:#222220;--line:#333}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:8px 16px 16px}
#stage{position:relative}
#boxes{position:absolute;pointer-events:none;display:none}
#lay{position:absolute;display:none;cursor:crosshair;touch-action:none}
body.nobox #boxes,body.nobox #lay{visibility:hidden}
#boxhelp{display:none;margin:4px 0;color:var(--muted);font-size:13px}
#buttons button.suggest{outline:3px solid #43a047}
#confirm{display:none;width:100%;margin-top:8px}
#img{display:block;width:100%;max-height:72vh;object-fit:contain;background:#000;
border-radius:6px}
#meta{display:flex;flex-wrap:wrap;gap:4px 16px;margin:8px 0;color:var(--muted)}
#meta b{color:var(--fg)}
#buttons{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
#buttons2{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
button{font:inherit;font-size:18px;padding:14px 8px;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--fg);cursor:pointer}
button:disabled{opacity:.4}
kbd{font-size:12px;color:var(--muted)}
header{display:flex;justify-content:space-between;align-items:center;padding:8px 16px;
max-width:1100px;margin:0 auto}
a{color:inherit}
#done{display:none;padding:48px 0;text-align:center}
</style></head><body>
<header><span id="progress">@@loading@@</span><a href="/stats">@@stats_link@@</a></header>
<main>
<div id="work">
<div id="stage"><img id="img" alt="@@img_alt@@">
<svg id="boxes" xmlns="http://www.w3.org/2000/svg"></svg>
<svg id="lay" xmlns="http://www.w3.org/2000/svg"></svg></div>
<div id="boxhelp">@@box_help@@</div>
<div id="meta"><span>@@camera@@ <b id="camera"></b></span><span id="time"></span>
<span>@@source@@ <b id="source"></b></span><span>@@person@@ <b id="score"></b> <span id="band">
</span></span><span>@@teacher@@ <b id="teacher"></b></span></div>
<div id="buttons">
<button data-label="person">@@btn_person@@ <kbd>1</kbd></button>
<button data-label="no_person">@@btn_no_person@@ <kbd>2</kbd></button>
<button data-label="unsure">@@btn_unsure@@ <kbd>3</kbd></button>
</div>
<button id="confirm">@@btn_confirm@@ <kbd>Enter</kbd></button>
<div id="buttons2">
<button id="skip">@@btn_skip@@ <kbd>@@key_space@@</kbd></button>
<button id="undo">@@btn_undo@@ <kbd>u</kbd></button>
</div>
</div>
<div id="done">@@done@@ <button id="undo2">@@undo_last@@</button></div>
</main>
<script>
const T = @@js_strings@@;
let current = null, busy = false;
const $ = id => document.getElementById(id);
function show(s) {
  current = s.frame;
  $("progress").textContent = s.remaining + " " + T.left + ", " + s.labeled + " " + T.labeled_n;
  $("undo").disabled = $("undo2").disabled = !s.can_undo;
  $("work").style.display = current ? "" : "none";
  $("done").style.display = current ? "none" : "block";
  if (!current) return;
  $("img").src = "/image/" + current.path.split("/").map(encodeURIComponent).join("/");
  $("camera").textContent = current.camera || T.unknown;
  $("time").textContent = current.time || "";
  $("source").textContent = current.source + (current.verdict ? " (" + current.verdict + ")" : "");
  $("score").textContent = current.score == null ? T.na : current.score.toFixed(2);
  $("teacher").textContent = current.teacher == null ? T.na : current.teacher.toFixed(2);
  $("band").textContent = "(" + (T["band_" + current.band] || current.band) + ")";
  loadBoxes(current.path);
  startBoxes();
}
// ---- opt-in box labeling: only frames whose index record carries "boxes" ----
let marks = [], drawn = [], sel = -1, drag = null;
const HUES = [210, 28, 280, 160, 330, 55, 190];
function modelHue(m) {
  const names = (current.boxes || []).map(b => b[5]).filter((v, i, a) => a.indexOf(v) === i).sort();
  return HUES[Math.max(0, names.indexOf(m)) % HUES.length];
}
function allBoxes() {
  const out = [];
  ((current && current.boxes) || []).forEach((b, i) =>
    out.push({box: b.slice(0, 4), score: b[4], model: b[5], state: marks[i], src: "model", i}));
  drawn.forEach((d, i) => out.push({box: d.box, state: d.state, src: "drawn", i}));
  return out;
}
function startBoxes() {
  const on = !!(current && current.boxes);
  marks = on ? current.boxes.map(() => "unset") : [];
  drawn = []; sel = -1; drag = null;
  $("boxhelp").style.display = $("confirm").style.display = on ? "block" : "none";
  drawLay();
}
function suggestion() {
  const st = allBoxes().map(b => b.state).filter(x => x !== "unset");
  if (st.includes("person")) return "person";
  return st.length ? "no_person" : null;
}
function drawLay() {
  const svg = $("lay"), img = $("img");
  svg.replaceChildren(); svg.style.display = "none";
  document.querySelectorAll("#buttons button").forEach(b =>
    b.classList.toggle("suggest", b.dataset.label === suggestion()));
  if (!current || !current.boxes || !img.clientWidth || !img.naturalWidth) return;
  const k = Math.min(img.clientWidth / img.naturalWidth, img.clientHeight / img.naturalHeight);
  const dw = img.naturalWidth * k, dh = img.naturalHeight * k;
  svg.style.left = img.offsetLeft + (img.clientWidth - dw) / 2 + "px";
  svg.style.top = img.offsetTop + (img.clientHeight - dh) / 2 + "px";
  svg.style.width = dw + "px"; svg.style.height = dh + "px";
  svg.setAttribute("viewBox", "0 0 " + dw + " " + dh);
  const boxes = allBoxes();
  if (drag && drag.cur) boxes.push({box: normBox(drag.start, drag.cur), state: "person",
                                    src: "drawing"});
  boxes.forEach((b, n) => {
    const hue = b.src === "model" ? modelHue(b.model) : 120;
    const alpha = b.src === "model" ? 0.35 + 0.65 * b.score : 1;
    const fill = b.state === "person" ? "rgba(67,160,71,.12)"
      : b.state === "no_person" ? "rgba(229,57,53,.12)" : "none";
    // Thin lines drawn just outside the box, so a distant person stays visible inside it.
    const isSel = b === boxes[sel], sw = isSel ? 3 : 1.5, pad = sw / 2 + 1;
    const x = b.box[0] * dw - pad, y = b.box[1] * dh - pad,
          w = (b.box[2] - b.box[0]) * dw + 2 * pad, h = (b.box[3] - b.box[1]) * dh + 2 * pad;
    const r = document.createElementNS(SVGNS, "rect");
    r.setAttribute("x", x); r.setAttribute("y", y); r.setAttribute("width", w);
    r.setAttribute("height", h); r.setAttribute("fill", fill);
    r.setAttribute("stroke", "hsl(" + hue + ",85%,50%)"); r.setAttribute("stroke-opacity", alpha);
    r.setAttribute("stroke-width", sw);
    if (b.src === "drawing") r.setAttribute("stroke-dasharray", "6 4");
    svg.appendChild(r);
    if (b.src === "drawing" || !isSel) return;   // the tag only on the selected box
    const t = document.createElementNS(SVGNS, "text");
    t.setAttribute("x", x + 3); t.setAttribute("y", Math.max(y - 4, 13));
    t.setAttribute("fill", "hsl(" + hue + ",85%,60%)"); t.setAttribute("font-size", 13);
    t.setAttribute("font-weight", "bold");
    const tag = b.src === "model" ? b.model + " " + b.score.toFixed(2) : T.box_drawn_tag;
    t.textContent = tag + " " + (b.state === "person" ? "\u2713" : b.state === "no_person"
      ? "\u2717" : "?");
    svg.appendChild(t);
  });
  svg.style.display = "block";
}
function normBox(a, b) {
  return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
}
function layPoint(e) {
  const r = $("lay").getBoundingClientRect();
  return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
          Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
}
const NEXT = {unset: "person", person: "no_person", no_person: "unset"};
function setState(b, state) {
  if (b.src === "model") marks[b.i] = state;
  else if (state === "unset") drawn.splice(b.i, 1);
  else drawn[b.i].state = state;
}
function hit(p) {
  let best = null, area = 9;
  allBoxes().forEach((b, n) => {
    const a = (b.box[2] - b.box[0]) * (b.box[3] - b.box[1]);
    if (p[0] >= b.box[0] && p[0] <= b.box[2] && p[1] >= b.box[1] && p[1] <= b.box[3]
        && a < area) { best = n; area = a; }
  });
  return best;
}
$("lay").addEventListener("pointerdown", e => {
  e.preventDefault(); drag = {start: layPoint(e), cur: null};
  $("lay").setPointerCapture(e.pointerId);
});
$("lay").addEventListener("pointermove", e => {
  if (!drag) return; drag.cur = layPoint(e); drawLay();
});
$("lay").addEventListener("pointerup", e => {
  if (!drag) return;
  const end = layPoint(e), moved = Math.hypot(end[0] - drag.start[0], end[1] - drag.start[1]);
  const d = drag; drag = null;
  if (moved >= 0.01) {
    const box = normBox(d.start, end);
    if ((box[2] - box[0]) > 0.005 && (box[3] - box[1]) > 0.005) {
      drawn.push({box, state: "person"}); sel = allBoxes().length - 1;
    }
  } else {
    const n = hit(end);
    if (n !== null) { const b = allBoxes()[n]; sel = n; setState(b, NEXT[b.state]); }
  }
  drawLay();
});
function boxPayload() {
  const out = [];
  marks.forEach((m, i) => { if (m !== "unset") out.push({source: "model", index: i, label: m}); });
  drawn.forEach(d => out.push({source: "drawn", box: d.box, label: d.state}));
  return out;
}
function selectNext(step) {
  const n = allBoxes().length;
  if (!n) return;
  sel = ((sel + step) % n + n) % n; drawLay();
}
function markSel(state) {
  const b = allBoxes()[sel];
  if (b) { setState(b, state); if (state === "unset" && b.src === "drawn") sel = -1; drawLay(); }
}
let boxData = null;
const SVGNS = "http://www.w3.org/2000/svg";
function drawBoxes() {
  const svg = $("boxes"), img = $("img");
  svg.replaceChildren();
  svg.style.display = "none";
  const d = boxData;
  if (!d || !d.available || !d.w || !d.h || !img.clientWidth) return;
  // The image is letterboxed (object-fit: contain): place the overlay on the drawn picture.
  const k = Math.min(img.clientWidth / d.w, img.clientHeight / d.h);
  const dw = d.w * k, dh = d.h * k;
  svg.style.left = img.offsetLeft + (img.clientWidth - dw) / 2 + "px";
  svg.style.top = img.offsetTop + (img.clientHeight - dh) / 2 + "px";
  svg.style.width = dw + "px"; svg.style.height = dh + "px";
  svg.setAttribute("viewBox", "0 0 " + d.w + " " + d.h);
  const stroke = Math.max(1, d.w / 800), font = Math.max(12, d.w / 50), pad = stroke / 2 + 1;
  d.person_boxes.forEach((b, i) => {
    const sc = d.person_scores[i];
    if (sc < 0.10 || b.length < 4) return;
    const color = sc >= 0.45 ? "#e53935" : "#fbc02d";
    const r = document.createElementNS(SVGNS, "rect");
    r.setAttribute("x", b[0] - pad); r.setAttribute("y", b[1] - pad);
    r.setAttribute("width", b[2] - b[0] + 2 * pad); r.setAttribute("height", b[3] - b[1] + 2 * pad);
    r.setAttribute("fill", "none"); r.setAttribute("stroke", color);
    r.setAttribute("stroke-width", stroke);
    const t = document.createElementNS(SVGNS, "text");
    t.setAttribute("x", b[0] + stroke); t.setAttribute("y", Math.max(b[1] - stroke, font));
    t.setAttribute("fill", color); t.setAttribute("font-size", font);
    t.setAttribute("font-weight", "bold"); t.textContent = sc.toFixed(2);
    svg.appendChild(r); svg.appendChild(t);
  });
  svg.style.display = "block";
}
async function loadBoxes(path) {
  boxData = null; drawBoxes();
  try {
    const r = await fetch("/api/boxes?path=" + encodeURIComponent(path));
    if (!r.ok || !current || current.path !== path) return;
    boxData = await r.json();
    drawBoxes();
  } catch (e) { /* the overlay is optional */ }
}
$("img").addEventListener("load", () => { drawBoxes(); drawLay(); });
window.addEventListener("resize", () => { drawBoxes(); drawLay(); });
async function call(url, body) {
  if (busy) return;
  busy = true;
  try {
    const r = await fetch(url, body === undefined ? {} :
      {method: "POST", headers: {"Content-Type": "application/json"},
       body: JSON.stringify(body)});
    if (r.ok) show(await r.json());
  } finally { busy = false; }
}
function label(l) {
  if (!current) return;
  const body = {path: current.path, label: l};
  if (current.boxes) { const bx = boxPayload(); if (bx.length) body.boxes = bx; }
  if (l === null && !body.boxes) return;
  call("/api/label", body);
}
$("confirm").onclick = () => label(null);
function skip() { if (current) call("/api/skip", {path: current.path}); }
function undo() { call("/api/undo", {}); }
document.querySelectorAll("[data-label]").forEach(b =>
  b.addEventListener("click", () => label(b.dataset.label)));
$("skip").onclick = skip; $("undo").onclick = undo; $("undo2").onclick = undo;
// Hold H to see the picture without any boxes.
document.addEventListener("keyup", e => {
  if (e.code === "KeyH") document.body.classList.remove("nobox"); });
window.addEventListener("blur", () => document.body.classList.remove("nobox"));
document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.code === "KeyH") { e.preventDefault(); document.body.classList.add("nobox"); return; }
  // Physical keys (``code``), not typed characters: without Shift the top row of a Czech
  // layout types "+", "ě", "š", so ``e.key`` would never be "1", "2", "3".
  if (current && current.boxes) {
    if (e.key === "Tab") { e.preventDefault(); selectNext(e.shiftKey ? -1 : 1); return; }
    if (e.code === "KeyP") { e.preventDefault(); markSel("person"); return; }
    if (e.code === "KeyN") { e.preventDefault(); markSel("no_person"); return; }
    if (e.code === "KeyX" || e.key === "Delete") { e.preventDefault(); markSel("unset"); return; }
    if (e.key === "Enter") { e.preventDefault(); label(null); return; }
  }
  const map = {Digit1: "person", Numpad1: "person", Digit2: "no_person", Numpad2: "no_person",
               Digit3: "unsure", Numpad3: "unsure"};
  if (map[e.code]) { e.preventDefault(); label(map[e.code]); }
  else if (e.key === " " || e.code === "Space") { e.preventDefault(); skip(); }
  else if (e.code === "KeyU" || e.key === "u" || e.key === "Backspace") {
    e.preventDefault(); undo(); }
});
call("/api/next");
</script></body></html>
"""


JS_KEYS = ("left", "labeled_n", "unknown", "na", "band_gray", "band_low", "band_high",
           "band_none", "box_drawn_tag")


def page(lang=DEFAULT_LANG):
    """The labeling page in ``lang``."""
    out = PAGE_TEMPLATE.replace("@@js_strings@@", json.dumps(
        {key: tr(lang, key) for key in JS_KEYS}).replace("</", "<\\/"))
    out = out.replace("@@lang@@", lang if lang in STRINGS else DEFAULT_LANG)
    return re.sub(r"@@(\w+)@@", lambda m: tr(lang, m.group(1)), out)


PAGE = page()


def _html_table(headers, rows):
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in row) + "</tr>"
                   for row in rows)
    return f'<div class="wrap"><table><tr>{head}</tr>{body}</table></div>'


def stats_page(stats, lang=DEFAULT_LANG):
    """The ``/stats`` HTML: the same tables and lines the CLI prints."""
    def t(key):
        return tr(lang, key)

    extra = ""
    verdicts = _verdict_lines(stats, lang)
    if verdicts:
        extra += "<p>" + "<br>".join(html.escape(v.strip()) for v in verdicts) + "</p>"
    if "boxes" in stats:
        extra += (f"<h2>{t('h_boxes')}</h2>"
                  f"<p>{html.escape(_box_summary(stats['boxes'], lang))}</p>"
                  f"{_html_table(_box_headers(lang), _box_rows(stats['boxes'], lang))}")
        if stats["boxes"]["recall"]:
            extra += (f"<h3>{t('box_recall_head')}</h3>"
                      + _html_table(_box_recall_headers(lang),
                                    _box_recall_rows(stats["boxes"], lang))
                      + f"<p>{html.escape(_box_recall_note(stats['boxes'], lang))}</p>")
    if "day_night" in stats:
        extra += (f"<h2>{t('h_day_night')}</h2>"
                  f"<p>{html.escape(_day_night_summary(stats, lang))}</p>"
                  f"{_html_table(day_night_headers(lang), _day_night_rows(stats, lang))}"
                  f"<p>{html.escape(_day_night_note(lang))}</p>")
    section = stats.get("incidents")
    if section:
        extra += (f"<h2>{t('h_incidents')}</h2>"
                  f"<p>{html.escape(_incident_summary(section, lang))}</p>")
        if section["all"]["labeled"]:
            extra += (_html_table(incident_headers(lang), _incident_rows(section, lang))
                      + "<p>"
                      + "<br>".join(html.escape(n) for n in _incident_notes(section, lang))
                      + "</p>")
        else:
            extra += f"<p>{t('no_incident_html')}</p>"
        rows = _first_alert_rows(section, lang)
        if rows:
            extra += (f"<h2>{t('h_first_alert')}</h2>"
                      f"<p>{html.escape(_first_alert_summary(lang))}</p>"
                      f"{_html_table(first_alert_headers(lang), rows)}")
    return f"""<!doctype html>
<html lang="{lang if lang in STRINGS else DEFAULT_LANG}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t('stats_title')}</title>
<style>
:root{{--bg:#f6f6f4;--fg:#1d1d1b;--line:#ddd}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#ecece8;--line:#333}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:16px}}
.wrap{{overflow-x:auto}}
table{{border-collapse:collapse}}
td,th{{padding:4px 10px;border-bottom:1px solid var(--line);text-align:left;
white-space:nowrap}}
a{{color:inherit}}
</style></head><body><main>
<p><a href="/">{t('back')}</a></p>
<h1>{t('stats_title')}</h1>
{_html_table(stats_headers(lang), _stats_rows(stats, lang))}
<p>{html.escape(_threshold_line(stats["threshold"], lang))}</p>
<p>{t('stats_footnote')}</p>
{extra}
</main></body></html>
"""


def make_server(session, port=0, bind=DEFAULT_BIND):
    """HTTP server for one :class:`LabelSession`. Stdlib only, threaded."""
    lang = session.lang

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, content_type):
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                log.debug("client closed the connection before the reply")

        def _json(self, code, payload):
            self._send(code, json.dumps(payload).encode(), "application/json")

        def _html(self, text):
            self._send(200, text.encode(), "text/html; charset=utf-8")

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/":
                self._html(page(lang))
            elif path == "/api/next":
                self._json(200, session.state())
            elif path == "/stats":
                self._html(stats_page(compute_stats(session.dataset_dir,
                                                    night=session.night), lang))
            elif path == "/api/stats":
                self._json(200, compute_stats(session.dataset_dir, night=session.night))
            elif path == "/api/boxes":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                rel = (query.get("path") or [""])[0]
                found = session.person_boxes(rel)
                self._json(200, {"person_boxes": found["boxes"] if found else [],
                                 "person_scores": found["scores"] if found else [],
                                 "w": found["w"] if found else None,
                                 "h": found["h"] if found else None,
                                 "available": found is not None})
            elif path.startswith("/image/"):
                self._image(urllib.parse.unquote(path[len("/image/"):]))
            else:
                self._json(404, {"error": "not found"})

        def _image(self, rel):
            full = session.image_path(rel)
            if full is None:
                self._json(404, {"error": "not found"})
                return
            try:
                with open(full, "rb") as f:
                    body = f.read()
            except OSError:
                self._json(404, {"error": "not found"})
                return
            self._send(200, body, "image/jpeg")

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object")
                if path == "/api/label":
                    session.label(payload.get("path"), payload.get("label"),
                                  boxes=payload.get("boxes"))
                elif path == "/api/skip":
                    session.skip(payload.get("path"))
                elif path == "/api/undo":
                    session.undo()
                else:
                    self._json(404, {"error": "not found"})
                    return
            except (KeyError, ValueError, TypeError) as exc:
                self._json(400, {"error": type(exc).__name__})
                return
            except OSError as exc:
                log.warning("label write failed: %s", exc)
                self._json(500, {"error": "label not written"})
                return
            self._json(200, session.state())

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server


def serve(session, port, bind):
    """Serve until Ctrl-C. Returns the process exit code."""
    try:
        server = make_server(session, port=port, bind=bind)
    except OSError as exc:
        print(f"cannot bind {bind}:{port}: {exc}", file=sys.stderr)
        return 1
    print(f"Labeling {len(session.frames)} frames ({len(session.pending())} unlabeled); "
          f"open http://{bind}:{server.server_address[1]}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _dataset_ok(prog, value):
    if os.path.isdir(value):
        return True
    print(f"{prog}: not a directory: {value}", file=sys.stderr)
    return False


def _night_from_config(prog, path):
    """The :func:`night_classifier` for ``--config``, or None after printing why not."""
    from .config import ConfigError, load_config

    try:
        app = load_config(path)
    except (OSError, ConfigError) as exc:
        print(f"{prog}: config: {exc}", file=sys.stderr)
        return None
    return night_classifier(app)


CONFIG_HELP = ("cameras.yaml: split the stats by day and night as the daemon judges it "
               "(location, per-camera schedule)")


def label_main(argv):
    parser = argparse.ArgumentParser(
        prog="tapo-monitor label",
        description="Label collected sent/review-log frames in a local web page")
    parser.add_argument("dataset_dir")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bind", default=DEFAULT_BIND,
                        help="address to bind (default 127.0.0.1; the page has no auth)")
    parser.add_argument("--seed", type=int, default=None,
                        help="seed for the low-score sample order")
    parser.add_argument("--low-sample", type=int, default=None,
                        help="queue at most N low-score frames (default: all, shuffled)")
    parser.add_argument("--config", help=CONFIG_HELP + " on the /stats page")
    parser.add_argument("--lang", choices=sorted(STRINGS), default=DEFAULT_LANG,
                        help="language of the page and /stats (default: en)")
    parser.add_argument("--scorer-url", default=None,
                        help="scoring service URL: draw the person boxes it finds on "
                             "each frame (default: off)")
    parser.add_argument("--scorer-token-env", default="TAPO_SCORER_TOKEN", metavar="NAME",
                        help="NAME of the environment variable with the scoring service's "
                             "shared token (NAME_FILE may name a file); default "
                             "TAPO_SCORER_TOKEN; used only if the service requires one")
    args = parser.parse_args(argv)
    if not _dataset_ok(parser.prog, args.dataset_dir):
        return 2
    night = None
    if args.config:
        night = _night_from_config(parser.prog, args.config)
        if night is None:
            return 1
    session = LabelSession(args.dataset_dir, seed=args.seed, low_sample=args.low_sample,
                           night=night, scorer_url=args.scorer_url, lang=args.lang,
                           scorer_token_env=args.scorer_token_env)
    return serve(session, args.port, args.bind)


def stats_main(argv):
    parser = argparse.ArgumentParser(
        prog="tapo-monitor label-stats",
        description="Summarize labels.jsonl: false alarms, misses, best threshold, "
                    "missed incidents")
    parser.add_argument("dataset_dir")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--config", help=CONFIG_HELP)
    parser.add_argument("--camera-alias", action="append", default=[], metavar="OLD=NEW",
                        help="count the records of camera OLD (renamed) under NEW in every "
                             "table; repeatable")
    parser.add_argument("--box-score", type=float, default=BOX_SCORE, metavar="S",
                        help="box labels: a model box counts as a detection from this score "
                             f"(default {BOX_SCORE})")
    parser.add_argument("--box-iou", type=float, default=BOX_IOU, metavar="IOU",
                        help="box labels: a drawn person is found when a detection overlaps "
                             f"it at least this much (default {BOX_IOU})")
    args = parser.parse_args(argv)
    if not _dataset_ok(parser.prog, args.dataset_dir):
        return 2
    if not 0 <= args.box_score <= 1 or not 0 < args.box_iou <= 1:
        print(f"{parser.prog}: --box-score must be 0-1 and --box-iou in (0, 1]",
              file=sys.stderr)
        return 2
    try:
        aliases = parse_camera_aliases(args.camera_alias)
    except ValueError as exc:
        print(f"{parser.prog}: {exc}", file=sys.stderr)
        return 2
    night = None
    if args.config:
        night = _night_from_config(parser.prog, args.config)
        if night is None:
            return 1
    stats = compute_stats(args.dataset_dir, night=night, aliases=aliases,
                          box_score=args.box_score, box_iou=args.box_iou)
    if args.json_output:
        print(json.dumps(stats, sort_keys=True))
    else:
        print(format_stats(stats))
    return 0
