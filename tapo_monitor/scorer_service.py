"""Tiny HTTP scoring service: POST a JPEG, get person/animal confidence back.

Runs a YOLOX ONNX model (Apache-2.0, e.g. yolox_tiny.onnx) on the local CPU and
answers ``{"person": 0.87, "animal": 0.0}`` — the daemon compares that to a config
threshold instead of asking a vision LLM whether the scene is empty. Heavy deps
(onnxruntime / numpy / Pillow) are imported lazily so the core package never needs
them; install with ``pip install tapo-monitor[scorer]``.

Model assets: https://github.com/Megvii-BaseDetection/YOLOX/releases (yolox_tiny.onnx,
input 416). The exported model applies sigmoid to objectness/class scores internally,
so confidence is simply objectness * class_score; box decoding is not needed for the
yes/no decision and is skipped entirely.
"""

import argparse
import calendar
import json
import logging
import os
import signal
import threading
import time
import uuid
from collections import deque
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
        "malformed_responses", "request_seconds_total", "request_seconds_max",
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
        self.instance_id = uuid.uuid4().hex[:16]
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.started = time.monotonic()
        self.requests = self.completed = self.failed = 0
        self.inference_runs = self.in_flight = self.max_in_flight = 0
        self.score_successes = 0
        self.person_candidates = self.animal_candidates = 0
        self.malformed_responses = 0
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
            return {
                "uptime_seconds": round(time.monotonic() - self.started, 3),
                "requests": self.requests,
                "completed": self.completed,
                "failed": self.failed,
                "score_successes": self.score_successes,
                "person_candidates": self.person_candidates,
                "animal_candidates": self.animal_candidates,
                "malformed_responses": self.malformed_responses,
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


def scores_from_output(output, num_classes=80, floor=0.01):
    """Max person/animal and per-class confidence from a raw YOLOX head output. Pure."""
    preds = output[0]                      # (N, 5+C)
    if not len(preds):
        return {"person": 0.0, "animal": 0.0, "classes": {}}
    obj = preds[:, 4]
    cls = preds[:, 5:5 + num_classes]
    conf = obj[:, None] * cls              # (N, C)
    per_class = conf.max(axis=0)
    person = float(per_class[PERSON_CLASS])
    animal = float(conf[:, list(ANIMAL_CLASSES)].max())
    named = {
        COCO_NAMES[i]: round(float(score), 4)
        for i, score in enumerate(per_class[:min(num_classes, len(COCO_NAMES))])
        if float(score) >= floor
    }
    return {"person": round(person, 4), "animal": round(animal, 4), "classes": named}


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
    diagnostics.
    """
    full = rect_results[0]
    combined = {"person": full["person"], "animal": full["animal"],
                "classes": full["classes"], "box": full["box"]}
    tiles = rect_results[1:]
    if tiles:
        best_tile = max(tiles, key=lambda r: r["person"])
        combined["tile_person"] = best_tile["person"]
        if combined["box"] is None:
            combined["box"] = best_tile["box"]
    return combined


def build_score_fn(model_path, input_size=416):
    """Load the ONNX model once and return score_fn(jpeg_bytes, tiles=1) -> dict.

    ``tiles > 1`` also scores a tiles×tiles grid (see :func:`tile_rects`), but the
    grid only refines localisation — see :func:`combine_rect_scores` for why the
    send-decision scores stay full-frame. The response gains a ``box`` (full-image
    xyxy in original pixels) for the winning person detection, so a caller can
    crop/zoom to it; ``box`` is ``None`` when no person.
    """
    import io

    import onnxruntime as ort
    from PIL import Image

    session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    def _run(img):
        tensor, ratio = preprocess_image(img, input_size)
        (output,) = session.run(None, {input_name: tensor})
        return output, ratio

    def score_fn(jpeg_bytes, tiles=1):
        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        rects = tile_rects(img.width, img.height, tiles)
        results = []
        for (x0, y0, x1, y1) in rects:
            crop = img if len(rects) == 1 else img.crop((int(x0), int(y0), int(x1), int(y1)))
            output, ratio = _run(crop)
            scores = scores_from_output(output)
            box = best_person_box(output, input_size)
            results.append({"person": scores["person"], "animal": scores["animal"],
                            "classes": scores["classes"],
                            "box": list(scale_box(box, ratio, x0, y0)) if box else None})
        combined = combine_rect_scores(results)
        combined["w"], combined["h"] = img.width, img.height
        return combined

    return score_fn


def make_server(score_fn, port=8765, metrics_file=None, metrics_persist_seconds=60,
                metrics_retention_days=7, metrics_retention_files=8,
                metrics_max_journal_bytes=METRICS_MAX_JOURNAL_BYTES):
    """HTTP server: POST /score (JPEG body) -> JSON scores; GET /health -> ok.

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
    inference_lock = threading.Lock()

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

        def do_GET(self):
            if self.path == "/health":
                self._reply(200, {"ok": True})
            elif self.path == "/metrics":
                self._reply(200, metrics.snapshot())
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(self.path)
            if parsed.path != "/score":
                self._reply(404, {"error": "not found"})
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
            try:
                with inference_lock:
                    result = score_fn(body, tiles)
                ok = True
            except Exception:  # noqa: BLE001 - report, don't kill the server
                failure_reason = "inference_error"
                log.warning("scoring failed: inference_error")
                self._reply(500, {"error": "scoring failed"})
            else:
                self._reply(200, result)
            finally:
                score_seconds = time.monotonic() - score_started
                metrics.finish(time.monotonic() - request_started, score_seconds, ok,
                               result=result, failure_reason=failure_reason, source_id=source_id)

        def log_message(self, fmt, *args):  # route http.server chatter to logging
            log.debug(fmt, *args)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    server.metrics = metrics
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
    parser.add_argument("--model", required=True, help="path to YOLOX .onnx weights")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--input-size", type=int, default=416)
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
    score_fn = build_score_fn(args.model, args.input_size)
    server = make_server(
        score_fn,
        port=args.port,
        metrics_file=args.metrics_file,
        metrics_persist_seconds=args.metrics_persist_seconds,
        metrics_retention_days=args.metrics_retention_days,
        metrics_retention_files=args.metrics_retention_files,
        metrics_max_journal_bytes=args.metrics_max_journal_bytes,
    )
    log.info("scorer listening on :%d (model=%s, input=%d)",
             args.port, args.model, args.input_size)
    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        server.metrics.flush()
        server.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()
