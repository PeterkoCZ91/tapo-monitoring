"""Tiny HTTP scoring service: POST a JPEG, get person/animal confidence back.

Runs a YOLOX ONNX model (Apache-2.0, e.g. yolox_tiny.onnx) on the local CPU and
answers ``{"person": 0.87, "animal": 0.0, "person_scores": [0.87, 0.52]}`` — the daemon
compares ``person`` to a config threshold instead of asking a vision LLM whether the scene
is empty, and counts ``person_scores`` at that threshold for the caption. Heavy deps
(onnxruntime / numpy / Pillow) are imported lazily so the core package never needs
them; install with ``pip install tapo-monitor[scorer]``.

Model assets: https://github.com/Megvii-BaseDetection/YOLOX/releases (yolox_tiny.onnx,
input 416). The exported model applies sigmoid to objectness/class scores internally,
so confidence is simply objectness * class_score; box decoding is not needed for the
yes/no decision and is skipped entirely.
"""

import argparse
import calendar
import hmac
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

SOURCE_ID_LENGTHS = range(16, 65)
LATENCY_SAMPLE_LIMIT = 1024
JOURNAL_STAMP = "%Y-%m-%dT%H:%M:%SZ"
METRICS_PERSIST_SECONDS = 60.0
METRICS_RETENTION_DAYS = 7.0
METRICS_RETENTION_FILES = 8
# Size backstop for the journal, on top of the age rotation. Nominal growth is ~2 MB a
# day, so seven retention days fit in ~14 MB and this cap never fires in normal
# operation — it exists so a burst of fat records cannot outgrow a small disk between
# two age checks.
METRICS_MAX_JOURNAL_BYTES = 32 * 1024 * 1024


def _parse_stamp(value):
    """Epoch for one journal ``recorded_at`` stamp, or None if it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(value, JOURNAL_STAMP)))
    except ValueError:
        return None


def _normalize_source_id(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if len(value) not in SOURCE_ID_LENGTHS:
        return None
    return value if all(character in "0123456789abcdef" for character in value) else None


def _percentile(samples, fraction):
    if not samples:
        return 0.0
    ordered = sorted(samples)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

PERSON_CLASS = 0
# COCO ids: bird, cat, dog, horse, sheep, cow, bear
ANIMAL_CLASSES = (14, 15, 16, 17, 18, 19, 21)
# COCO ids: car, motorcycle, bus, truck
VEHICLE_CLASSES = (2, 3, 5, 7)
# ``person_scores``: every separate person over this floor, at most this many, with
# anchors of one person merged at this IoU. The floor sits well under any configured
# threshold, so the daemon can count at its own (day or night) value.
PERSON_LIST_FLOOR = 0.10
PERSON_LIST_LIMIT = 10
PERSON_NMS_IOU = 0.45
COCO_NAMES = (
    "person", "bicycle", "car", "motorbike", "aeroplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "sofa",
    "pottedplant", "bed", "diningtable", "toilet", "tvmonitor", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
)


def _positive_score(value):
    try:
        score = float(value)
    except (TypeError, ValueError):
        return False
    return 0.0 < score <= 1.0


class ScorerMetrics:
    """Thread-safe aggregate metrics with optional durable, rotating snapshots."""

    _PERSISTED_FIELDS = (
        "requests", "completed", "failed", "inference_runs", "max_in_flight",
        "score_successes", "person_candidates", "animal_candidates",
        "malformed_responses", "artifact", "request_seconds_total", "request_seconds_max",
        "score_seconds_total", "score_seconds_max", "restart_count",
    )

    def __init__(self, metrics_file=None, persist_seconds=60, retention_days=7,
                 retention_files=8, max_journal_bytes=METRICS_MAX_JOURNAL_BYTES):
        self._lock = threading.Lock()
        self._metrics_file = os.fspath(metrics_file) if metrics_file else None
        self._state_file = f"{self._metrics_file}.state" if self._metrics_file else None
        self._persist_seconds = max(0.0, float(persist_seconds))
        self._retention_seconds = max(1.0, float(retention_days) * 24 * 60 * 60)
        self._retention_files = max(1, int(retention_files))
        self._max_journal_bytes = max(1, int(max_journal_bytes))
        self._last_persist = None
        self._journal_started = None
        self._request_samples = deque(maxlen=LATENCY_SAMPLE_LIMIT)
        self._score_samples = deque(maxlen=LATENCY_SAMPLE_LIMIT)
        self._sources = {}
        self.arch = None        # detector family name, set by make_server
        self.instance_id = uuid.uuid4().hex[:16]
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.started = time.monotonic()
        self.requests = self.completed = self.failed = 0
        self.inference_runs = self.in_flight = self.max_in_flight = 0
        self.score_successes = 0
        self.person_candidates = self.animal_candidates = 0
        self.malformed_responses = 0
        self.artifact = 0       # frames answered as partially decoded video, not scored
        self.failure_reasons = {}
        self.request_seconds_total = self.request_seconds_max = 0.0
        self.score_seconds_total = self.score_seconds_max = 0.0
        self.restart_count = 0
        self._load_state()
        self.restart_count += 1

    @staticmethod
    def _new_source():
        return {
            "requests": 0,
            "completed": 0,
            "failed": 0,
            "score_successes": 0,
            "person_candidates": 0,
            "animal_candidates": 0,
            "malformed_responses": 0,
            "failure_reasons": {},
            "request_samples": deque(maxlen=LATENCY_SAMPLE_LIMIT),
            "score_samples": deque(maxlen=LATENCY_SAMPLE_LIMIT),
        }

    def _source_bucket(self, source_id):
        if source_id is None:
            return None
        if source_id not in self._sources and len(self._sources) >= 64:
            return None
        return self._sources.setdefault(source_id, self._new_source())

    def _load_state(self):
        if not self._state_file:
            return
        try:
            with open(self._state_file, encoding="utf-8") as state_file:
                payload = json.load(state_file)
        except (OSError, ValueError, TypeError):
            return
        counters = payload.get("counters", payload) if isinstance(payload, dict) else {}
        if not isinstance(counters, dict):
            return
        for field in self._PERSISTED_FIELDS:
            value = counters.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(self, field, value)
        reasons = counters.get("failure_reasons")
        if isinstance(reasons, dict):
            self.failure_reasons = {
                str(key): int(value) for key, value in reasons.items()
                if isinstance(value, int) and value >= 0
            }
        sources = counters.get("sources")
        if isinstance(sources, dict):
            for source_id, source in sources.items():
                source_id = _normalize_source_id(source_id)
                if source_id is None or not isinstance(source, dict) or len(self._sources) >= 64:
                    continue
                bucket = self._source_bucket(source_id)
                for field in (
                    "requests", "completed", "failed", "score_successes",
                    "person_candidates", "animal_candidates", "malformed_responses",
                ):
                    value = source.get(field)
                    if isinstance(value, int) and value >= 0:
                        bucket[field] = value
                reasons = source.get("failure_reasons")
                if isinstance(reasons, dict):
                    bucket["failure_reasons"] = {
                        str(key): int(value) for key, value in reasons.items()
                        if isinstance(value, int) and value >= 0
                    }

    def _source_snapshot_unlocked(self, bucket):
        return {
            key: value for key, value in bucket.items()
            if key not in ("request_samples", "score_samples")
        } | {
            "request_seconds_p50": round(_percentile(bucket["request_samples"], 0.50), 6),
            "request_seconds_p95": round(_percentile(bucket["request_samples"], 0.95), 6),
            "score_seconds_p50": round(_percentile(bucket["score_samples"], 0.50), 6),
            "score_seconds_p95": round(_percentile(bucket["score_samples"], 0.95), 6),
        }

    def _sources_snapshot_unlocked(self):
        return {
            source_id: self._source_snapshot_unlocked(bucket)
            for source_id, bucket in self._sources.items()
        }

    def _persistent_snapshot_unlocked(self):
        return {field: getattr(self, field) for field in self._PERSISTED_FIELDS} | {
            "failure_reasons": dict(self.failure_reasons),
            "sources": self._sources_snapshot_unlocked(),
        }

    def _rotated_paths(self):
        directory = os.path.dirname(self._metrics_file) or "."
        prefix = os.path.basename(self._metrics_file) + "."
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        return [
            os.path.join(directory, name) for name in names
            if name.startswith(prefix) and name[len(prefix):len(prefix) + 8].isdigit()
        ]

    def _journal_started_at(self):
        """Epoch of the current journal's oldest record.

        Deliberately not the file mtime: the journal is appended every persist
        interval, so its mtime is always fresh and an mtime-based age never reaches
        the retention window — the file would grow forever. Foreign content that
        carries no parseable stamp falls back to mtime, which is the only age signal
        left for a file this process did not write.
        """
        if self._journal_started is not None:
            return self._journal_started
        try:
            with open(self._metrics_file, encoding="utf-8") as journal:
                first_line = journal.readline()
        except OSError:
            return None
        try:
            payload = json.loads(first_line)
        except ValueError:
            payload = {}
        started = _parse_stamp(payload.get("recorded_at") if isinstance(payload, dict) else None)
        if started is None:
            try:
                started = os.path.getmtime(self._metrics_file)
            except OSError:
                return None
        self._journal_started = started
        return started

    def _journal_oversized(self):
        """True when the journal has outgrown the size cap.

        The size is read fresh from the filesystem, so growth this process did not
        write (a crashed twin, a copy truncated and re-fed by an operator) counts too.
        """
        try:
            return os.path.getsize(self._metrics_file) >= self._max_journal_bytes
        except OSError:
            return False

    def _rotate_if_needed(self, now):
        started = self._journal_started_at()
        aged_out = started is not None and now - started >= self._retention_seconds
        if not aged_out and not self._journal_oversized():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
        rotated = f"{self._metrics_file}.{stamp}"
        suffix = 1
        while os.path.exists(rotated):
            rotated = f"{self._metrics_file}.{stamp}.{suffix}"
            suffix += 1
        os.replace(self._metrics_file, rotated)
        self._journal_started = None            # the next append starts a fresh journal
        paths = sorted(self._rotated_paths(), key=os.path.getmtime, reverse=True)
        for path in paths[self._retention_files:]:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _persist_unlocked(self):
        if not self._metrics_file:
            return
        directory = os.path.dirname(self._metrics_file) or "."
        os.makedirs(directory, exist_ok=True)
        counters = self._persistent_snapshot_unlocked()
        state_tmp = f"{self._state_file}.tmp"
        with open(state_tmp, "w", encoding="utf-8") as state_file:
            json.dump({"version": 1, "counters": counters}, state_file,
                      sort_keys=True, separators=(",", ":"))
            state_file.write("\n")
        os.chmod(state_tmp, 0o640)
        os.replace(state_tmp, self._state_file)
        now = time.time()
        self._rotate_if_needed(now)
        with open(self._metrics_file, "a", encoding="utf-8") as journal:
            json.dump({"recorded_at": time.strftime(JOURNAL_STAMP, time.gmtime(now)),
                       **counters}, journal, sort_keys=True, separators=(",", ":"))
            journal.write("\n")
        os.chmod(self._metrics_file, 0o640)
        if self._journal_started is None:
            self._journal_started = now

    def _persist_if_due_unlocked(self):
        if not self._metrics_file:
            return
        now = time.monotonic()
        if self._last_persist is None or now - self._last_persist >= self._persist_seconds:
            try:
                self._persist_unlocked()
            except (OSError, TypeError, ValueError) as exc:
                # Persistence is observability only; a full/read-only disk must not
                # turn an otherwise valid scoring request into a 500 response.
                log.warning("scorer metrics persistence failed: %s", type(exc).__name__)
            self._last_persist = now

    def flush(self):
        """Durably write the current aggregate snapshot, if persistence is enabled."""
        with self._lock:
            if self._metrics_file:
                try:
                    self._persist_unlocked()
                except (OSError, TypeError, ValueError) as exc:
                    log.warning("scorer metrics persistence failed: %s", type(exc).__name__)
                self._last_persist = time.monotonic()

    def begin(self, tiles, source_id=None):
        with self._lock:
            self.requests += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.inference_runs += 1 + (tiles * tiles if tiles > 1 else 0)
            bucket = self._source_bucket(_normalize_source_id(source_id))
            if bucket is not None:
                bucket["requests"] += 1

    def finish(self, request_seconds, score_seconds, ok, result=None, failure_reason=None,
               source_id=None):
        with self._lock:
            self.in_flight -= 1
            self.completed += 1
            self.failed += int(not ok)
            if ok and isinstance(result, dict):
                self.score_successes += 1
                self.person_candidates += int(_positive_score(result.get("person")))
                self.animal_candidates += int(_positive_score(result.get("animal")))
                self.artifact += int(result.get("artifact") is True)
            elif ok:
                self.malformed_responses += 1
                failure_reason = "malformed_response"
            if failure_reason:
                self.failure_reasons[failure_reason] = self.failure_reasons.get(
                    failure_reason, 0) + 1
            self.request_seconds_total += request_seconds
            self.request_seconds_max = max(self.request_seconds_max, request_seconds)
            self.score_seconds_total += score_seconds
            self.score_seconds_max = max(self.score_seconds_max, score_seconds)
            self._request_samples.append(request_seconds)
            self._score_samples.append(score_seconds)
            bucket = self._source_bucket(_normalize_source_id(source_id))
            if bucket is not None:
                bucket["completed"] += 1
                bucket["failed"] += int(not ok)
                bucket["request_samples"].append(request_seconds)
                bucket["score_samples"].append(score_seconds)
                if ok and isinstance(result, dict):
                    bucket["score_successes"] += 1
                    bucket["person_candidates"] += int(_positive_score(result.get("person")))
                    bucket["animal_candidates"] += int(_positive_score(result.get("animal")))
                elif ok:
                    bucket["malformed_responses"] += 1
                    failure_reason = "malformed_response"
                if failure_reason:
                    bucket["failure_reasons"][failure_reason] = (
                        bucket["failure_reasons"].get(failure_reason, 0) + 1
                    )
            self._persist_if_due_unlocked()

    def snapshot(self):
        with self._lock:
            snapshot = {
                "uptime_seconds": round(time.monotonic() - self.started, 3),
                "requests": self.requests,
                "completed": self.completed,
                "failed": self.failed,
                "score_successes": self.score_successes,
                "person_candidates": self.person_candidates,
                "animal_candidates": self.animal_candidates,
                "malformed_responses": self.malformed_responses,
                "artifact": self.artifact,
                "failure_reasons": dict(self.failure_reasons),
                "inference_runs": self.inference_runs,
                "in_flight": self.in_flight,
                "max_in_flight": self.max_in_flight,
                "request_seconds_total": round(self.request_seconds_total, 6),
                "request_seconds_max": round(self.request_seconds_max, 6),
                "score_seconds_total": round(self.score_seconds_total, 6),
                "score_seconds_max": round(self.score_seconds_max, 6),
                "request_seconds_p50": round(_percentile(self._request_samples, 0.50), 6),
                "request_seconds_p95": round(_percentile(self._request_samples, 0.95), 6),
                "score_seconds_p50": round(_percentile(self._score_samples, 0.50), 6),
                "score_seconds_p95": round(_percentile(self._score_samples, 0.95), 6),
                "instance_id": self.instance_id,
                "started_at": self.started_at,
                "restart_count": self.restart_count,
                "sources": self._sources_snapshot_unlocked(),
            }
            if self.arch:
                snapshot["arch"] = self.arch
            return snapshot


def scores_from_output(output, num_classes=80, floor=0.01):
    """Max person/animal and per-class confidence from a raw YOLOX head output. Pure."""
    preds = output[0]                      # (N, 5+C)
    if not len(preds):
        return {"person": 0.0, "animal": 0.0, "vehicle": 0.0, "classes": {}}
    obj = preds[:, 4]
    cls = preds[:, 5:5 + num_classes]
    conf = obj[:, None] * cls              # (N, C)
    per_class = conf.max(axis=0)
    person = float(per_class[PERSON_CLASS])
    animal = float(conf[:, list(ANIMAL_CLASSES)].max())
    vehicle = float(conf[:, list(VEHICLE_CLASSES)].max()) if num_classes > max(VEHICLE_CLASSES) \
        else 0.0
    named = {
        COCO_NAMES[i]: round(float(score), 4)
        for i, score in enumerate(per_class[:min(num_classes, len(COCO_NAMES))])
        if float(score) >= floor
    }
    return {"person": round(person, 4), "animal": round(animal, 4),
            "vehicle": round(vehicle, 4), "classes": named}


def _grids_and_strides(input_size, strides):
    """YOLOX anchor grid centres and per-anchor stride for a square input. Pure (numpy)."""
    import numpy as np

    grids, expanded = [], []
    for s in strides:
        hw = input_size // s
        xv, yv = np.meshgrid(np.arange(hw), np.arange(hw))
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        expanded.append(np.full((hw * hw, 1), s))
    return np.concatenate(grids, 0), np.concatenate(expanded, 0)


def best_person_box(output, input_size=640, strides=(8, 16, 32), floor=0.05):
    """Input-coord xyxy box of the highest-confidence person anchor, or None.

    This YOLOX export applies sigmoid to obj/class but leaves the box head **raw**, so the
    anchor's ``(x, y, w, h)`` must be decoded against its grid cell and stride:
    ``xy = (raw_xy + grid) * stride``, ``wh = exp(raw_wh) * stride`` (input pixels). We pick
    the anchor with the top person confidence (obj * person class); ``None`` when it fails
    ``floor``. If the anchor count doesn't match the grid (a different/pre-decoded export),
    fall back to treating the head as already-decoded ``cx,cy,w,h``. Pure.
    """
    import numpy as np

    preds = output[0]
    if not len(preds):
        return None
    conf = preds[:, 4] * preds[:, 5 + PERSON_CLASS]
    i = int(conf.argmax())
    if float(conf[i]) < floor:
        return None
    grids, expanded = _grids_and_strides(input_size, strides)
    if len(grids) == len(preds):
        s = float(expanded[i, 0])
        cx = (float(preds[i, 0]) + float(grids[i, 0])) * s
        cy = (float(preds[i, 1]) + float(grids[i, 1])) * s
        w = float(np.exp(preds[i, 2])) * s
        h = float(np.exp(preds[i, 3])) * s
    else:
        cx, cy, w, h = (float(v) for v in preds[i, :4])
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def _box_iou(box, boxes):
    """IoU of one xyxy box against an (N, 4) array of them. Pure (numpy)."""
    import numpy as np

    x1 = np.maximum(box[0], boxes[:, 0])
    y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2])
    y2 = np.minimum(box[3], boxes[:, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area = (box[2] - box[0]) * (box[3] - box[1])
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    return inter / np.maximum(area + areas - inter, 1e-9)


def _scores_and_boxes(output, conf, input_size, strides, floor, iou, limit, with_boxes):
    """Anchors of ``conf`` over ``floor``, decoded and thinned by greedy NMS. Pure.

    Shared by :func:`person_scores` and :func:`vehicle_scores`: ``conf`` is the per-anchor
    confidence of whichever class set the caller counts.
    """
    import numpy as np

    preds = output[0]
    keep = np.flatnonzero(conf >= floor)
    if not len(keep):
        return ([], []) if with_boxes else []
    grids, expanded = _grids_and_strides(input_size, strides)
    if len(grids) == len(preds):
        s = expanded[keep, 0].astype(np.float64)
        cx = (preds[keep, 0] + grids[keep, 0]) * s
        cy = (preds[keep, 1] + grids[keep, 1]) * s
        w = np.exp(preds[keep, 2]) * s
        h = np.exp(preds[keep, 3]) * s
    else:
        cx, cy, w, h = (preds[keep, k].astype(np.float64) for k in range(4))
    boxes = np.stack((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), axis=1)
    order = np.argsort(-conf[keep], kind="stable")
    boxes, scores = boxes[order], conf[keep][order]
    found, found_boxes = [], []
    while len(scores) and len(found) < limit:
        found.append(round(float(scores[0]), 4))
        found_boxes.append(tuple(float(v) for v in boxes[0]))
        rest = _box_iou(boxes[0], boxes[1:]) < iou
        boxes, scores = boxes[1:][rest], scores[1:][rest]
    return (found, found_boxes) if with_boxes else found


def person_scores(output, input_size=640, strides=(8, 16, 32), floor=PERSON_LIST_FLOOR,
                  iou=PERSON_NMS_IOU, limit=PERSON_LIST_LIMIT, with_boxes=False):
    """Confidence of each separate person in a raw YOLOX head output, highest first. Pure.

    One person lights up many neighbouring anchors, so the anchors over ``floor`` are
    decoded (as in :func:`best_person_box`) and thinned by greedy non-maximum suppression
    at ``iou``: what survives is one entry per person. The first entry is the ``person``
    score itself. The caller counts the entries at its own threshold; the list is capped
    at ``limit`` so a crowd cannot bloat the response. ``with_boxes`` also returns each
    person's ``(x1, y1, x2, y2)`` in model-input pixels, parallel to the scores.
    """
    preds = output[0]
    if not len(preds):
        return ([], []) if with_boxes else []
    conf = preds[:, 4] * preds[:, 5 + PERSON_CLASS]
    return _scores_and_boxes(output, conf, input_size, strides, floor, iou, limit, with_boxes)


def vehicle_scores(output, input_size=640, strides=(8, 16, 32), floor=PERSON_LIST_FLOOR,
                   iou=PERSON_NMS_IOU, limit=PERSON_LIST_LIMIT, with_boxes=False):
    """Like :func:`person_scores`, for vehicles (car, motorcycle, bus, truck). Pure.

    An anchor's vehicle confidence is its objectness times its best vehicle class, so a
    van that is half "car", half "truck" is one vehicle, not two.
    """
    preds = output[0]
    if not len(preds):
        return ([], []) if with_boxes else []
    conf = preds[:, 4] * preds[:, 5:][:, list(VEHICLE_CLASSES)].max(axis=1)
    return _scores_and_boxes(output, conf, input_size, strides, floor, iou, limit, with_boxes)


def tile_rects(width, height, tiles, overlap=0.15):
    """Original-coord ``(x0, y0, x1, y1)`` rects: the whole frame plus a tiles×tiles grid.

    Scoring each grid cell separately lets a distant person — a few pixels once the whole
    frame is letterboxed to the model's small input — fill a cell and register. The whole
    frame stays in the list so a close, large subject is still caught. ``overlap`` widens
    each cell so a subject on a seam isn't split. Pure.
    """
    rects = [(0.0, 0.0, float(width), float(height))]
    n = int(tiles)
    if n <= 1:
        return rects
    tw, th = width / n, height / n
    ox, oy = tw * overlap, th * overlap
    for j in range(n):
        for i in range(n):
            x0 = max(0.0, i * tw - ox)
            y0 = max(0.0, j * th - oy)
            x1 = min(float(width), (i + 1) * tw + ox)
            y1 = min(float(height), (j + 1) * th + oy)
            rects.append((x0, y0, x1, y1))
    return rects


def scale_box(box, ratio, off_x, off_y):
    """Map a tile-input-coord xyxy box to full-image original coords. Pure.

    ``ratio`` is the letterbox scale used for that tile (original->input); dividing undoes
    it, then the tile's top-left ``(off_x, off_y)`` offset places it in the full frame.
    """
    x1, y1, x2, y2 = box
    return (x1 / ratio + off_x, y1 / ratio + off_y, x2 / ratio + off_x, y2 / ratio + off_y)


ARTIFACT_GREY = 128
ARTIFACT_GREY_BAND = 4          # |luma - 128| <= 4 counts as the flat grey base
ARTIFACT_GREY_SHARE = 0.6       # share of grey pixels; real scenes in the data stay under 0.13
ARTIFACT_MEAN_LOW, ARTIFACT_MEAN_HIGH = 120.0, 136.0
ARTIFACT_SIZE = (160, 90)


def artifact_filter_enabled(env=None):
    """``TAPO_SCORER_ARTIFACT_FILTER=0`` (or off/false/no) turns the filter off; default on."""
    env = os.environ if env is None else env
    value = (env.get("TAPO_SCORER_ARTIFACT_FILTER") or "").strip().lower()
    return value not in ("0", "off", "false", "no")


def is_decode_artifact(jpeg_bytes):
    """True for a partially decoded video frame: a flat luma-128 base with a few real blocks.

    A damaged HEVC decode leaves most of the picture mid-grey and only some intra-coded
    blocks with content; the detector then "finds" a person in those blocks. Judged on a
    small luma copy: at least ``ARTIFACT_GREY_SHARE`` of the pixels within
    ``ARTIFACT_GREY_BAND`` of 128 and a mean luma of 120-136. Night IR, dark and foggy
    frames are textured or off 128 and never match. Anything undecodable is not an artifact.
    """
    try:
        import io

        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(jpeg_bytes))
        img.draft("L", (320, 320))
        luma = np.asarray(img.convert("L").resize(ARTIFACT_SIZE), dtype=np.float32)
    except Exception:  # noqa: BLE001 - the scorer itself reports undecodable input
        return False
    share = float(np.mean(np.abs(luma - ARTIFACT_GREY) <= ARTIFACT_GREY_BAND))
    return share >= ARTIFACT_GREY_SHARE and ARTIFACT_MEAN_LOW <= float(luma.mean()) <= ARTIFACT_MEAN_HIGH


def artifact_result():
    """Reply for a decode artifact: nothing detected, flagged for newer clients."""
    return {"person": 0, "animal": 0, "vehicle": 0, "classes": {}, "box": None,
            "person_scores": [], "person_boxes": [], "vehicle_scores": [],
            "vehicle_boxes": [], "artifact": True}


def preprocess_image(img, input_size):
    """PIL RGB image -> (1,3,S,S) float32 letterboxed BGR array + used ratio."""
    import numpy as np
    from PIL import Image

    ratio = min(input_size / img.width, input_size / img.height)
    resized = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                         Image.BILINEAR)
    canvas = Image.new("RGB", (input_size, input_size), (114, 114, 114))
    canvas.paste(resized, (0, 0))
    arr = np.asarray(canvas, dtype=np.float32)[:, :, ::-1]   # RGB -> BGR (YOLOX convention)
    return arr.transpose(2, 0, 1)[None].copy(), ratio


def preprocess(jpeg_bytes, input_size):
    """JPEG bytes -> (1,3,S,S) float32 letterboxed BGR array + used ratio."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    return preprocess_image(img, input_size)


def combine_rect_scores(rect_results):
    """Fold per-rect scores (full frame first, then tiles) into one response. Pure.

    The decision-grade ``person``/``animal``/``classes`` come from the FULL FRAME only:
    a tile is a blown-up crop, and at night the upscaled IR grain hallucinates 0.3-0.6
    "person" scores — and taking a max over five crops turns that into a near-certain
    false alert per event. Tiles still earn their keep for localisation: ``box`` falls
    back to the best-person tile when the full frame has none (a distant subject the
    caller wants to crop to), and ``tile_person`` reports the best tile score for
    diagnostics. ``person_scores`` (one entry per person, see :func:`person_scores`)
    is the full frame's too, for the same reason: one person split across two
    overlapping tiles would otherwise count twice.
    """
    full = rect_results[0]
    combined = {"person": full["person"], "animal": full["animal"],
                "classes": full["classes"], "box": full["box"]}
    # ``vehicle*`` is optional: a rect from an older shape simply lacks it.
    for key in ("vehicle", "vehicle_scores", "vehicle_boxes", "person_scores", "person_boxes"):
        if key in full:
            combined[key] = full[key]
    tiles = rect_results[1:]
    if tiles:
        best_tile = max(tiles, key=lambda r: r["person"])
        combined["tile_person"] = best_tile["person"]
        if combined["box"] is None:
            combined["box"] = best_tile["box"]
    return combined


ENV_PROVIDER = "TAPO_SCORER_PROVIDER"


def select_providers(env=None):
    """ONNX Runtime ``(providers, provider_options)`` for the configured backend. Pure.

    ``TAPO_SCORER_PROVIDER=openvino-gpu`` runs on the Intel iGPU through OpenVINO;
    anything else, including unset, keeps the CPU provider so an unconfigured host
    behaves exactly as before.
    """
    env = os.environ if env is None else env
    if (env.get(ENV_PROVIDER) or "").strip().lower() == "openvino-gpu":
        return ["OpenVINOExecutionProvider"], [{"device_type": "GPU"}]
    return ["CPUExecutionProvider"], None


def _open_session(ort, model_path):
    """Create the inference session; a GPU that is missing must not take scoring down.

    A failed GPU start (no /dev/dri access, runtime not installed, provider not built
    in) falls back to CPU with a loud warning: a scorer that answers slowly beats one
    that does not answer, and the scorer being down blocks every camera's gating.
    """
    providers, options = select_providers()
    if providers != ["CPUExecutionProvider"]:
        try:
            session = ort.InferenceSession(
                model_path, providers=providers, provider_options=options)
            if session.get_providers()[0] == providers[0]:
                log.info("scorer inference provider: %s", providers[0])
                return session
            log.warning("scorer provider %s not active (got %s); falling back to CPU",
                        providers[0], session.get_providers())
        except Exception as exc:  # noqa: BLE001 - any GPU start failure means CPU
            log.warning("scorer provider %s failed to start (%s: %s); falling back to CPU",
                        providers[0], type(exc).__name__, exc)
    return ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])


def build_score_fn(model_path, input_size=416, decoder=None):
    """Load the ONNX model once and return score_fn(jpeg_bytes, tiles=1) -> dict.

    ``decoder`` (see :mod:`tapo_monitor.detectors`) owns the model-specific preprocessing
    and output decoding; the default is the YOLOX decoder at ``input_size``, i.e. the
    behaviour from before detectors were pluggable. The returned function carries the
    family name as ``score_fn.arch``.

    ``tiles > 1`` also scores a tiles×tiles grid (see :func:`tile_rects`), but the
    grid only refines localisation — see :func:`combine_rect_scores` for why the
    send-decision scores stay full-frame. The response gains a ``box`` (full-image
    xyxy in original pixels) for the winning person detection, so a caller can
    crop/zoom to it; ``box`` is ``None`` when no person.
    """
    import io

    import onnxruntime as ort
    from PIL import Image

    from . import detectors

    decoder = decoder or detectors.YoloxDecoder(input_size)
    session = _open_session(ort, model_path)
    input_name = session.get_inputs()[0].name

    def _run(img, full):
        tensor, ctx = decoder.preprocess(img)
        outputs = session.run(None, {input_name: tensor})
        return decoder.decode(outputs, ctx, full)

    def _shift(box, x0, y0):
        return [box[0] + x0, box[1] + y0, box[2] + x0, box[3] + y0]

    def score_fn(jpeg_bytes, tiles=1):
        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        rects = tile_rects(img.width, img.height, tiles)
        results = []
        for (x0, y0, x1, y1) in rects:
            crop = img if len(rects) == 1 else img.crop((int(x0), int(y0), int(x1), int(y1)))
            full = not results      # full frame only, like the decision scores
            decoded = _run(crop, full)
            box = decoded["box"]
            entry = {"person": decoded["person"], "animal": decoded["animal"],
                     "vehicle": decoded["vehicle"],
                     "classes": decoded["classes"],
                     "box": _shift(box, x0, y0) if box else None}
            if full:
                entry["person_scores"] = decoded["person_scores"]
                # Parallel to person_scores, in original pixels: lets a camera ignore a
                # static person-shaped object (an ignore zone) without losing the others.
                entry["person_boxes"] = [[round(v, 1) for v in _shift(b, x0, y0)]
                                         for b in decoded["person_boxes"]]
                # Same for vehicles; older clients ignore these fields.
                entry["vehicle_scores"] = decoded["vehicle_scores"]
                entry["vehicle_boxes"] = [[round(v, 1) for v in _shift(b, x0, y0)]
                                          for b in decoded["vehicle_boxes"]]
            results.append(entry)
        combined = combine_rect_scores(results)
        combined["w"], combined["h"] = img.width, img.height
        return combined

    score_fn.arch = decoder.name
    return score_fn


class TokenConfigError(ValueError):
    """The shared-token settings are unusable; the service must not start unprotected."""


def load_token(env=None):
    """The shared bearer token from ``TAPO_SCORER_TOKEN_FILE`` or ``TAPO_SCORER_TOKEN``.

    ``None`` when neither is set (authentication stays off). The file wins over the value
    and must not be readable by group or others (0600); a file that is named but missing,
    unreadable, too open or empty is an error, never a silent fall back to an open service.
    """
    env = os.environ if env is None else env
    return _load_secret(env, "TAPO_SCORER_TOKEN_FILE", "TAPO_SCORER_TOKEN")


def _load_secret(env, file_var, value_var):
    """A secret from ``file_var`` (a 0600 file, wins) or ``value_var``; ``None`` if unset."""
    path = (env.get(file_var) or "").strip()
    if path:
        try:
            mode = os.stat(path).st_mode
            if mode & 0o077:
                raise TokenConfigError(
                    f"{file_var} must be mode 0600 (not readable by group/others)")
            with open(path, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as exc:
            raise TokenConfigError(
                f"cannot read {file_var}: {exc.strerror or 'error'}") from None
        if not token:
            raise TokenConfigError(f"{file_var} is empty")
        return token
    return (env.get(value_var) or "").strip() or None


SHADOW_QUEUE_SIZE = 8
SHADOW_TIMEOUT = 8.0
SHADOW_DISAGREE_MAX = 300
SHADOW_HIGH = 0.45
SHADOW_LOW = 0.25
SHADOW_WARN_SECONDS = 60.0
ENSEMBLE_LOW = 0.12
ENSEMBLE_HIGH = 0.45
ENSEMBLE_BAND = 0.10
ENSEMBLE_TIMEOUT = 2.0


@dataclass
class ShadowConfig:
    """Second detector run on a copy of every scored frame; never part of the reply."""

    url: str
    log_path: str
    token: str | None = None
    disagree_dir: str | None = None
    disagree_max: int = SHADOW_DISAGREE_MAX
    timeout: float = SHADOW_TIMEOUT
    queue_size: int = SHADOW_QUEUE_SIZE


def load_shadow(env=None):
    """Shadow settings from ``TAPO_SCORER_SHADOW_*``; ``None`` (feature off) unless both
    ``..._URL`` and ``..._LOG`` are set (``..._LOG`` may be left out when
    ``TAPO_SCORER_ENSEMBLE`` is on; nothing is then logged). Token handling is the same as ``load_token``."""
    env = os.environ if env is None else env
    url = (env.get("TAPO_SCORER_SHADOW_URL") or "").strip()
    log_path = (env.get("TAPO_SCORER_SHADOW_LOG") or "").strip()
    ensemble_on = bool((env.get("TAPO_SCORER_ENSEMBLE") or "").strip())
    if not url or (not log_path and not ensemble_on):
        return None
    return ShadowConfig(
        url=url,
        log_path=log_path,
        token=_load_secret(env, "TAPO_SCORER_SHADOW_TOKEN_FILE", "TAPO_SCORER_SHADOW_TOKEN"),
        disagree_dir=(env.get("TAPO_SCORER_SHADOW_DISAGREE_DIR") or "").strip() or None,
        disagree_max=max(1, _env_setting(env, "TAPO_SCORER_SHADOW_DISAGREE_MAX", int,
                                         SHADOW_DISAGREE_MAX)),
        timeout=_env_setting(env, "TAPO_SCORER_SHADOW_TIMEOUT", float, SHADOW_TIMEOUT),
    )


def _shadow_call(url, token, body, tiles, timeout):
    """POST ``body`` to a scorer service; ``(payload, None)`` or ``(None, error_class)``.

    The error class is a short fixed word (``timeout``, ``connection``, ``http_<code>``,
    ``bad_response``); the URL and the token never leave this function.
    """
    if tiles > 1:
        url += ("&" if "?" in url else "?") + f"tiles={tiles}"
    headers = {"Content-Type": "image/jpeg"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return None, f"http_{exc.code}"
    except ValueError:
        return None, "bad_response"
    except OSError as exc:      # URLError, timeouts, refused connections
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
        return None, "timeout" if timed_out else "connection"
    value = payload.get("person") if isinstance(payload, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "bad_response"
    return payload, None


class EnsembleConfigError(ValueError):
    """``TAPO_SCORER_ENSEMBLE*`` settings are unusable; the service must not start."""


@dataclass
class EnsembleConfig:
    """Combine the primary and the shadow person score into one reply (``gate`` mode).

    ``host`` is the person threshold the clients apply (the old ``high``); ``trust`` is the
    shadow score trusted on its own and defaults to ``host``. A shadow score in
    ``[trust, host)`` is remapped linearly into ``[host, host + band)`` so that clients that
    compare ``person`` with their own threshold (and its hold band) accept it.
    """

    mode: str = "gate"
    low: float = ENSEMBLE_LOW
    host: float = ENSEMBLE_HIGH
    timeout: float = ENSEMBLE_TIMEOUT
    trust: float | None = None
    band: float = ENSEMBLE_BAND

    def __post_init__(self):
        if self.trust is None:
            self.trust = self.host

    @property
    def high(self):
        """Backward-compatible name of ``host``."""
        return self.host


def load_ensemble(env=None, shadow=None):
    """Ensemble settings from ``TAPO_SCORER_ENSEMBLE*``; ``None`` (off) when unset.

    Needs the shadow settings (the second service); raises ``EnsembleConfigError`` for an
    unknown mode, a missing shadow or unusable numbers rather than starting half-configured.
    ``TAPO_SCORER_ENSEMBLE_HOST`` (alias ``_HIGH``), ``_TRUST`` (default: HOST), ``_BAND``,
    ``_LOW`` and ``_TIMEOUT`` tune the rule.
    """
    env = os.environ if env is None else env
    mode = (env.get("TAPO_SCORER_ENSEMBLE") or "").strip().lower()
    if not mode or mode == "off":
        return None
    if mode != "gate":
        raise EnsembleConfigError("TAPO_SCORER_ENSEMBLE must be 'gate' (or unset)")
    if shadow is None:
        raise EnsembleConfigError("TAPO_SCORER_ENSEMBLE needs TAPO_SCORER_SHADOW_URL")
    host_default = _env_setting(env, "TAPO_SCORER_ENSEMBLE_HIGH", float, ENSEMBLE_HIGH)
    host = _env_setting(env, "TAPO_SCORER_ENSEMBLE_HOST", float, host_default)
    cfg = EnsembleConfig(
        mode=mode,
        low=_env_setting(env, "TAPO_SCORER_ENSEMBLE_LOW", float, ENSEMBLE_LOW),
        host=host,
        timeout=_env_setting(env, "TAPO_SCORER_ENSEMBLE_TIMEOUT", float, ENSEMBLE_TIMEOUT),
        trust=_env_setting(env, "TAPO_SCORER_ENSEMBLE_TRUST", float, host),
        band=_env_setting(env, "TAPO_SCORER_ENSEMBLE_BAND", float, ENSEMBLE_BAND),
    )
    if not (0.0 < cfg.low < cfg.trust <= cfg.host <= 1.0) or cfg.band < 0 or cfg.timeout <= 0:
        raise EnsembleConfigError(
            "TAPO_SCORER_ENSEMBLE_LOW/TRUST/HOST/BAND/TIMEOUT need "
            "0 < LOW < TRUST <= HOST <= 1, BAND >= 0 and TIMEOUT > 0")
    return cfg


def _remap_marginal(config, score):
    """Map a shadow score in ``[trust, host)`` linearly into ``[host, host + band)``."""
    mapped = config.host + (score - config.trust) / (config.host - config.trust) * config.band
    return min(mapped, 1.0)


def combine_ensemble(config, primary, shadow):
    """One reply from the primary result and the shadow payload (both dicts with ``person``).

    1. shadow >= host -> shadow's person and boxes.
    2. trust <= shadow < host -> person remapped into ``[host, host + band)``, shadow boxes
       (``person_scores`` from the shadow, remapped the same way), ``used = shadow_marginal``.
    3. primary >= host and shadow >= low -> the primary reply.
    4. else ``min(max(primary, shadow), host - 0.01)`` on the primary reply.
    Returns ``(reply, used)``.
    """
    p = float(primary["person"])
    s = float(shadow["person"])
    reply = dict(primary)
    marginal = config.trust <= s < config.host
    if s >= config.host or marginal:
        used = "shadow_marginal" if marginal else "shadow"
        reply["person"] = round(_remap_marginal(config, s) if marginal else s, 4)
        for key in ("box", "person_boxes", "person_scores"):
            if key in shadow:
                reply[key] = shadow[key]
            else:
                reply.pop(key, None)
        scores = reply.get("person_scores")
        if marginal and isinstance(scores, list):
            # Clients rebuild ``person`` from the list too: remap it with the same rule.
            reply["person_scores"] = [
                round(_remap_marginal(config, float(x)), 4)
                if config.trust <= float(x) < config.host else x for x in scores]
    elif p >= config.host and s >= config.low:
        used = "primary"
    else:
        used = "neither"
        cap = config.host - 0.01
        reply["person"] = round(min(max(p, s), cap), 4)
        scores = reply.get("person_scores")
        if isinstance(scores, list):
            # A person the second model denied must not return through the list: a client
            # that rebuilds ``person`` from it (alert zones) would otherwise revive it.
            reply["person_scores"] = [min(float(x), cap) for x in scores]
    reply["ensemble"] = {"mode": config.mode, "primary": round(p, 4),
                         "shadow": round(s, 4), "used": used}
    return reply, used


class EnsembleGate:
    """Per-request ensemble: starts the shadow call, joins it within the timeout, combines.

    Any shadow problem yields the primary reply untouched (``used: primary_only``). Failures
    are counted and warned about at most once a minute, by error class only.
    """

    def __init__(self, config, shadow):
        self.config = config
        self.shadow = shadow
        self._lock = threading.Lock()
        self._last_warn = None
        self.counts = {"requests": 0, "shadow": 0, "primary": 0, "shadow_marginal": 0,
                       "neither": 0, "primary_only": 0, "failed": 0, "timeouts": 0}

    def snapshot(self):
        with self._lock:
            return {"mode": self.config.mode, **self.counts}

    def _count(self, *keys):
        with self._lock:
            for key in keys:
                self.counts[key] += 1

    def start(self, body, tiles):
        """Begin the shadow call in the background; returns the handle for ``finish``."""
        started = time.monotonic()
        box = {}

        def run():
            try:
                box["out"] = _shadow_call(self.shadow.url, self.shadow.token, body, tiles,
                                          self.config.timeout)
            except Exception as exc:  # noqa: BLE001 - never raise out of the worker
                box["out"] = (None, "connection")
                log.debug("ensemble call failed: %s", type(exc).__name__)

        thread = threading.Thread(target=run, name="scorer-ensemble", daemon=True)
        thread.start()
        return thread, box, started

    def finish(self, handle, primary):
        """``(reply, shadow_person, error, shadow_seconds)``; waits at most the timeout
        counted from ``start``."""
        thread, box, started = handle
        thread.join(max(0.0, started + self.config.timeout - time.monotonic()))
        out = box.get("out")
        seconds = time.monotonic() - started
        self._count("requests")
        primary_person = primary.get("person") if isinstance(primary, dict) else None
        usable = (not isinstance(primary_person, bool)
                  and isinstance(primary_person, (int, float)))
        payload, error = out if out is not None else (None, "timeout")
        if payload is not None and usable:
            reply, used = combine_ensemble(self.config, primary, payload)
            self._count(used)
            return reply, float(payload["person"]), None, seconds
        self._count("primary_only")
        if error:
            self._count("failed")
            if error == "timeout":
                self._count("timeouts")
            self._warn(error)
        reply = primary
        if usable:
            reply = dict(primary)
            reply["ensemble"] = {"mode": self.config.mode,
                                 "primary": round(float(primary_person), 4),
                                 "shadow": None, "used": "primary_only"}
        shadow_person = float(payload["person"]) if payload is not None else None
        return reply, shadow_person, error, seconds

    def _warn(self, error):
        now = time.monotonic()
        if self._last_warn is None or now - self._last_warn >= SHADOW_WARN_SECONDS:
            self._last_warn = now
            log.warning("ensemble shadow call failed, serving the primary score: %s", error)


def _score_or_none(result, key):
    value = result.get(key) if isinstance(result, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 4)


class ShadowRunner:
    """Bounded queue plus one worker thread that sends frames to the shadow service.

    ``submit`` never blocks and never raises: a full queue drops the frame and counts it.
    Everything that goes wrong in the worker is contained there, so the primary reply
    path is untouched. The URL and the token are never logged or written anywhere.
    """

    def __init__(self, config):
        self.config = config
        self._queue = queue.Queue(maxsize=max(1, config.queue_size))
        self._lock = threading.Lock()
        self.counts = {"sent": 0, "failed": 0, "dropped": 0, "disagree": 0}
        self._last_warn = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="scorer-shadow", daemon=True)
        self._thread.start()

    def snapshot(self):
        with self._lock:
            return dict(self.counts)

    def _count(self, key):
        with self._lock:
            self.counts[key] += 1

    def submit(self, body, tiles, source_id, result, primary_seconds, outcome=None):
        """Queue a frame. ``outcome`` is ``(shadow_person, error, seconds)`` when the shadow
        was already called for this frame (ensemble mode): it is logged, not sent again."""
        try:
            self._queue.put_nowait((body, tiles, source_id, result, primary_seconds, outcome))
        except queue.Full:
            self._count("dropped")

    def close(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process(*item)
            except Exception as exc:  # noqa: BLE001 - the shadow must never raise out
                log.debug("shadow item failed: %s", type(exc).__name__)

    def _process(self, body, tiles, source_id, result, primary_seconds, outcome=None):
        reused = outcome is not None
        if reused:
            shadow_person, error, shadow_seconds = outcome
        else:
            started = time.monotonic()
            payload, error = _shadow_call(self.config.url, self.config.token, body, tiles,
                                          self.config.timeout)
            shadow_person = float(payload["person"]) if payload is not None else None
            shadow_seconds = time.monotonic() - started
        self._count("failed" if error else "sent")
        if error and not reused:
            self._warn(error)
        primary_person = _score_or_none(result, "person")
        self._write_log(source_id, primary_person, _score_or_none(result, "vehicle"),
                        shadow_person, primary_seconds, shadow_seconds, error)
        if (shadow_person is not None and primary_person is not None
                and self.config.disagree_dir
                and ((primary_person >= SHADOW_HIGH and shadow_person < SHADOW_LOW)
                     or (shadow_person >= SHADOW_HIGH and primary_person < SHADOW_LOW))):
            if self._save_disagreement(body, primary_person, shadow_person):
                self._count("disagree")

    def _warn(self, error):
        now = time.monotonic()
        if self._last_warn is None or now - self._last_warn >= SHADOW_WARN_SECONDS:
            self._last_warn = now
            log.warning("shadow scoring failed: %s", error)

    def _write_log(self, source_id, primary_person, primary_vehicle, shadow_person,
                   primary_seconds, shadow_seconds, error):
        row = {
            "ts": time.strftime(JOURNAL_STAMP, time.gmtime()),
            "source_id": source_id,
            "primary_person": primary_person,
            "primary_vehicle": primary_vehicle,
            "shadow_person": shadow_person,
            "primary_seconds": round(primary_seconds, 4),
            "shadow_seconds": round(shadow_seconds, 4),
            "error": error,
        }
        path = self.config.log_path
        if not path:
            return
        try:
            new = not os.path.exists(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            if new:
                os.chmod(path, 0o600)
        except OSError as exc:
            log.warning("shadow log write failed: %s", type(exc).__name__)

    def _save_disagreement(self, body, primary_person, shadow_person):
        directory = self.config.disagree_dir
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
            now = time.time()
            stem = (time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
                    + f"{int(now * 1000) % 1000:03d}"
                    + f"_p{primary_person:.2f}_s{shadow_person:.2f}")
            path = os.path.join(directory, stem + ".jpg")
            n = 1
            while os.path.exists(path):
                path = os.path.join(directory, f"{stem}-{n}.jpg")
                n += 1
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            names = sorted(n for n in os.listdir(directory) if n.endswith(".jpg"))
            for old in names[:-self.config.disagree_max]:
                try:
                    os.unlink(os.path.join(directory, old))
                except OSError:
                    pass
            return True
        except OSError as exc:
            log.warning("shadow disagreement save failed: %s", type(exc).__name__)
            return False


def _authorized(header, token):
    """True when ``header`` is ``Bearer <token>``; constant-time comparison, never raises."""
    if not isinstance(header, str):
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip().encode("utf-8", "replace"),
                               token.encode("utf-8", "replace"))


def make_server(score_fn, port=8765, metrics_file=None, metrics_persist_seconds=60,
                metrics_retention_days=7, metrics_retention_files=8,
                metrics_max_journal_bytes=METRICS_MAX_JOURNAL_BYTES, arch=None, token=None,
                shadow=None, ensemble=None, artifact_filter=None):
    """HTTP server: POST /score (JPEG body) -> JSON scores; GET /health -> ok.

    ``arch`` (the detector family name, never the model path) is reported by /health and
    /metrics; ``None`` leaves both replies exactly as they were.

    With a ``token``, /score and /metrics need ``Authorization: Bearer <token>`` and answer
    401 without detail otherwise; /health stays open (failover checks, systemd) and carries
    nothing sensitive. ``None`` leaves every endpoint open, as before.

    With a ``shadow`` (``ShadowConfig``) every scored frame is also handed, after the reply
    has been written, to a bounded background queue that sends it to a second scorer
    service and logs both scores; the reply itself is never delayed or changed.

    With an ``ensemble`` (``EnsembleConfig``, needs the ``shadow`` for the second service's
    URL and token) the second service is instead called in parallel with the primary
    inference and the reply carries one combined person score; the reply waits for it at most
    ``ensemble.timeout`` seconds, after which the primary reply goes out unchanged.

    With ``artifact_filter`` (default: on unless ``TAPO_SCORER_ARTIFACT_FILTER=0``) a
    partially decoded video frame (see :func:`is_decode_artifact`) is answered with zero
    scores, empty boxes and ``"artifact": true`` without running any inference, and counted
    in /metrics as ``artifact``.

    Threaded so /health, /metrics and slow clients never queue behind a long
    inference (a 4K tiled request can hold the CPU for seconds). Inference itself
    stays serialized behind one lock: parallel CPU runs would just slow each other
    down past the callers' timeouts.
    """
    metrics = ScorerMetrics(
        metrics_file=metrics_file,
        persist_seconds=metrics_persist_seconds,
        retention_days=metrics_retention_days,
        retention_files=metrics_retention_files,
        max_journal_bytes=metrics_max_journal_bytes,
    )
    metrics.arch = arch or getattr(score_fn, "arch", None)
    if artifact_filter is None:
        artifact_filter = artifact_filter_enabled()
    inference_lock = threading.Lock()
    runner = ShadowRunner(shadow) if shadow is not None else None
    if ensemble is not None and shadow is None:
        raise EnsembleConfigError("ensemble mode needs a shadow service")
    gate = EnsembleGate(ensemble, shadow) if ensemble is not None else None

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The client gave up and closed the socket; there is nobody left
                # to reply to, and writing an error into the same dead socket
                # would only crash the handler a second time.
                log.debug("client closed the connection before the reply")

        def _deny(self):
            """Answer 401 (and True) when a token is required and not presented."""
            if token is None or _authorized(self.headers.get("Authorization"), token):
                return False
            self._reply(401, {"error": "unauthorized"})
            return True

        def do_GET(self):
            if self.path == "/health":
                self._reply(200, {"ok": True, "arch": metrics.arch} if metrics.arch
                            else {"ok": True})
            elif self.path == "/metrics":
                if not self._deny():
                    snap = metrics.snapshot()
                    if runner is not None:
                        snap["shadow"] = runner.snapshot()
                    if gate is not None:
                        snap["ensemble"] = gate.snapshot()
                    self._reply(200, snap)
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(self.path)
            if parsed.path != "/score":
                self._reply(404, {"error": "not found"})
                return
            if self._deny():
                return
            try:
                tiles = max(1, int(parse_qs(parsed.query).get("tiles", ["1"])[0]))
            except (TypeError, ValueError):
                tiles = 1
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            source_id = _normalize_source_id(self.headers.get("X-Tapo-Source-ID"))
            metrics.begin(tiles, source_id)
            request_started = time.monotonic()
            score_started = time.monotonic()
            ok = False
            result = None
            failure_reason = None
            artifact = artifact_filter and is_decode_artifact(body)
            handle = gate.start(body, tiles) if gate is not None and not artifact else None
            outcome = None
            try:
                if artifact:
                    result = artifact_result()
                else:
                    with inference_lock:
                        result = score_fn(body, tiles)
                ok = True
            except Exception:  # noqa: BLE001 - report, don't kill the server
                failure_reason = "inference_error"
                log.warning("scoring failed: inference_error")
                self._reply(500, {"error": "scoring failed"})
            else:
                reply = result
                if handle is not None:
                    reply, shadow_person, error, shadow_seconds = gate.finish(handle, result)
                    outcome = (shadow_person, error, shadow_seconds)
                self._reply(200, reply)
            finally:
                score_seconds = time.monotonic() - score_started
                metrics.finish(time.monotonic() - request_started, score_seconds, ok,
                               result=result, failure_reason=failure_reason, source_id=source_id)
                if runner is not None and ok and not artifact and isinstance(result, dict):
                    runner.submit(body, tiles, source_id, result,
                                  time.monotonic() - request_started, outcome)

        def log_message(self, fmt, *args):  # route http.server chatter to logging
            log.debug(fmt, *args)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    server.metrics = metrics
    server.shadow = runner
    return server


def _env_setting(env, name, cast, default):
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("ignoring unparseable %s, using %s", name, default)
        return default


def metrics_settings(env=None):
    """Durable-metrics options taken from the environment.

    Unset, blank and unparseable all mean the same thing here: keep the default.
    Metrics are observability, so a missing variable must never be the reason the
    scorer refuses to start — under ``Restart=always`` that is an invisible crash loop.
    """
    env = os.environ if env is None else env
    return {
        "metrics_file": (env.get("TAPO_SCORER_METRICS_FILE") or "").strip() or None,
        "metrics_persist_seconds": _env_setting(
            env, "TAPO_SCORER_METRICS_PERSIST_SECONDS", float, METRICS_PERSIST_SECONDS),
        "metrics_retention_days": _env_setting(
            env, "TAPO_SCORER_METRICS_RETENTION_DAYS", float, METRICS_RETENTION_DAYS),
        "metrics_retention_files": _env_setting(
            env, "TAPO_SCORER_METRICS_RETENTION_FILES", int, METRICS_RETENTION_FILES),
        "metrics_max_journal_bytes": _env_setting(
            env, "TAPO_SCORER_METRICS_MAX_JOURNAL_BYTES", int,
            METRICS_MAX_JOURNAL_BYTES),
    }


def _blank_tolerant(cast, default):
    """argparse type that reads a quoted-empty ${VAR} expansion as "use the default"."""
    def convert(value):
        text = str(value).strip()
        if not text:
            return default
        try:
            return cast(text)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(
                f"expected {cast.__name__}, got {value!r}") from None
    return convert


def build_parser(env=None):
    """CLI parser whose metrics defaults come from the environment.

    The metrics flags accept a missing value (``nargs="?"``) as well as an empty one,
    because a unit file that interpolates an undefined ``${VAR}`` outside quotes drops
    the word entirely and argparse would otherwise exit 2 before the model ever loads.
    """
    settings = metrics_settings(env)
    parser = argparse.ArgumentParser(description="tapo-monitor scoring service")
    parser.add_argument("--model", required=True,
                        help="path to .onnx weights (family: TAPO_SCORER_ARCH)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--input-size", nargs="?", const=None,
                        type=_blank_tolerant(int, None), default=None,
                        help="model input side; default TAPO_SCORER_INPUT_SIZE, else the "
                             "detector family's default (yolox 416, detr 640)")
    parser.add_argument("--metrics-file", nargs="?", const=None,
                        type=_blank_tolerant(str, None),
                        default=settings["metrics_file"],
                        help="JSONL aggregate metrics journal path")
    for flag, key, cast in (
        ("--metrics-persist-seconds", "metrics_persist_seconds", float),
        ("--metrics-retention-days", "metrics_retention_days", float),
        ("--metrics-retention-files", "metrics_retention_files", int),
        ("--metrics-max-journal-bytes", "metrics_max_journal_bytes", int),
    ):
        parser.add_argument(flag, nargs="?", const=settings[key],
                            type=_blank_tolerant(cast, settings[key]),
                            default=settings[key])
    return parser


def main(argv=None):  # pragma: no cover - thin entry point, needs model weights
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from . import detectors
    try:
        token = load_token()
    except TokenConfigError as exc:
        print(f"scorer config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        config = detectors.detector_config(os.environ, args.input_size)
    except detectors.DetectorConfigError as exc:
        print(f"scorer config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        shadow = load_shadow()
        ensemble = load_ensemble(os.environ, shadow)
    except (TokenConfigError, EnsembleConfigError) as exc:
        print(f"scorer config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    decoder = detectors.build_decoder(config)
    score_fn = build_score_fn(args.model, config.input_size, decoder)
    server = make_server(
        score_fn,
        arch=decoder.name,
        token=token,
        shadow=shadow,
        ensemble=ensemble,
        port=args.port,
        metrics_file=args.metrics_file,
        metrics_persist_seconds=args.metrics_persist_seconds,
        metrics_retention_days=args.metrics_retention_days,
        metrics_retention_files=args.metrics_retention_files,
        metrics_max_journal_bytes=args.metrics_max_journal_bytes,
    )
    log.info("scorer listening on :%d (model=%s, arch=%s, input=%d, auth=%s)",
             args.port, args.model, decoder.name, config.input_size,
             "token" if token else "off")
    if shadow is not None:
        log.info("shadow scoring on (disagreement images %s)",
                 "kept" if shadow.disagree_dir else "off")
    if ensemble is not None:
        log.info("ensemble %s on (low %.2f, trust %.2f, host %.2f, band %.2f, timeout %.1fs)",
                 ensemble.mode, ensemble.low, ensemble.trust, ensemble.host, ensemble.band,
                 ensemble.timeout)

    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        server.metrics.flush()
        if server.shadow is not None:
            server.shadow.close()
        server.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()
