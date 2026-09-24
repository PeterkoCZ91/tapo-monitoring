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

from . import incident, sentlog

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
                     "sample_rate": _sample_rate(record.get("sample_rate"))}
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

    def __init__(self, dataset_dir, *, seed=None, low_sample=None, night=None):
        self.dataset_dir = dataset_dir
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

    def _append(self, frame, label, now, *, undo=False):
        record = {"path": frame.path, "sha256": frame.sha256, "label": label,
                  "labeled_at": float(time.time() if now is None else now),
                  "score": frame.score, "camera": frame.camera, "source": frame.source}
        if undo:
            record["undo"] = True
        with open(os.path.join(self.dataset_dir, LABELS_NAME), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def label(self, path, label, *, now=None):
        """Append one verdict for ``path`` and drop it from the queue."""
        if label not in LABELS:
            raise ValueError(f"unknown label {label!r}")
        with self._lock:
            frame = self._frame(path)
            record = self._append(frame, label, now)
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
            record = self._append(frame, restored, now, undo=True)
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
            "band": band(frame.score)}


# ── stats ────────────────────────────────────────────────────────────────────

# A slice's best threshold needs this many decided frames with a score, and this many of
# each class, before it reads as a recommendation. Below that it is marked "too few
# labels": on a handful of frames the minimum-error cut sits wherever one odd frame is.
MIN_SUPPORT_DECIDED = 20
MIN_SUPPORT_PER_CLASS = 5
DAY_NIGHT = ("day", "night", "unknown")


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


def compute_stats(dataset_dir, *, night=None):
    """Label counts, false-alarm and miss estimates, per band/camera, best threshold.

    Review frames are also split by their index ``verdict`` (hold / drop). With
    ``night(camera, ts) -> bool`` (see :func:`night_classifier`) a ``day_night`` section
    repeats the summary and best threshold for day, night and frames without a time,
    and per camera for day and night.
    """
    records = list(latest_labels(dataset_dir).values())
    meta = index_meta(dataset_dir)
    for rec in records:
        rec["score"] = _number(rec.get("score"))
        # A label line carries no time or verdict; its frame's index record does.
        known = meta.get(rec.get("path"))
        if known is None:
            known = {"ts": _number(rec.get("ts")), "verdict": rec.get("verdict"),
                     "sample_rate": _sample_rate(rec.get("sample_rate"))}
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
    stats["incidents"] = incident_stats(dataset_dir, night=night)
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


def incident_frames(dataset_dir, labels=None):
    """Every indexed frame as a dict for :func:`group_incidents`, labeled or not. Read-only.

    ``host`` is the directory holding the host's logs (``<host>/sent-log/index.jsonl``).
    A record without ``camera`` (older hosts) takes the host's camera when every other
    record of that host names the same one, else None. ``labels`` is ``{path: label}``;
    no hashing, so it stays cheap enough for every ``/stats`` request. A sent record
    without ``delivered`` counts as delivered: the sent log writes False only when
    delivery failed. ``send_path`` is the delivery path a sent record names (its index
    ``path``), else None.
    """
    labels = labels or {}
    frames = []
    for rel, _, record in _index_records(os.path.realpath(dataset_dir)):
        camera = record.get("camera")
        ident = record.get("incident")
        ts = _number(record.get("ts"))
        frames.append({
            "path": rel, "host": "/".join(rel.split("/")[:-2]),
            "camera": str(camera) if camera else None,
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


def incident_stats(dataset_dir, *, night=None, gap=INCIDENT_GAP):
    """Quality per incident rather than per frame: the ``incidents`` stats section.

    Every indexed frame counts, labeled or not (a delivery matters even when nobody
    labeled its frame). Overall, per camera and, with ``night`` (see
    :func:`night_classifier`, applied to the incident start), per day and night; plus
    the missed person incidents themselves.
    """
    labels = {rec.get("path"): rec["label"] for rec in latest_labels(dataset_dir).values()}
    frames = incident_frames(dataset_dir, labels)
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


def _stats_rows(stats):
    """``(name, labeled, person, no_person, unsure, false alarms, misses)`` rows."""
    rows = []
    groups = [("all", stats)]
    groups += [(f"band {name}", stats["bands"][name]) for name in BANDS]
    groups += [(f"camera {name}", block) for name, block in stats["cameras"].items()]
    for name, block in groups:
        c, fa, miss = block["counts"], block["false_alarms"], block["misses"]
        rows.append((name, str(c["total"]), str(c["person"]), str(c["no_person"]),
                     str(c["unsure"]),
                     f"{fa['no_person']}/{fa['decided']} ({_pct(fa['rate'])})",
                     f"{miss['person']}/{miss['decided']} ({_pct(miss['rate'])})"))
    return rows


STATS_HEADERS = ("group", "labeled", "person", "no_person", "unsure",
                 "false alarms (sent)", "misses (review)")
DAY_NIGHT_HEADERS = ("slice", "n", "person", "no_person", "best threshold", "errors",
                     "false alarms", "misses", "note")
TOO_FEW = "too few labels"


def _threshold_line(best):
    if best["threshold"] is None:
        return "best threshold: n/a (no decided frames with a score)"
    return (f"best threshold: person >= {best['threshold']:.2f} -> "
            f"{best['errors']} errors of {best['n']} ({best['false_alarms']} false alarms, "
            f"{best['misses']} misses, accuracy {_pct(best['accuracy'])})")


def _verdict_lines(stats):
    """Misses by review verdict; nothing while every labeled review frame was a hold."""
    verdicts = stats.get("verdicts") or {}
    if not set(verdicts) - {"hold", "unknown"}:
        return []
    out = ["misses by review verdict (person in a held frame = hold error, in a dropped "
           "frame = real miss):"]
    for name, block in verdicts.items():
        text = f"  {name}: {block['person']}/{block['decided']} ({_pct(block['rate'])})"
        if block["sampled"]:
            text += (f", {block['sampled']} of them sampled -> estimated "
                     f"{block['estimated_person']:.0f} person of "
                     f"{block['estimated_decided']:.0f} frames (count / sample_rate; an "
                     "estimate)")
        out.append(text)
    return out


def _day_night_row(name, best):
    def cell(value):
        return "-" if value is None else str(value)

    return (name, str(best["n"]), str(best["person"]), str(best["no_person"]),
            "n/a" if best["threshold"] is None else f"{best['threshold']:.2f}",
            cell(best["errors"]), cell(best["false_alarms"]), cell(best["misses"]),
            "" if best["supported"] else TOO_FEW)


def _day_night_rows(stats):
    parts = stats["day_night"]
    rows = [_day_night_row("all", stats["threshold"])]
    rows += [_day_night_row(name, parts[name]["threshold"]) for name in DAY_NIGHT
             if name != "unknown" or parts[name]["counts"]["total"]]
    for cam, by_part in parts["cameras"].items():
        rows += [_day_night_row(f"camera {cam} {name}", by_part[name]["threshold"])
                 for name in ("day", "night") if by_part[name]["counts"]["total"]]
    return rows


def _day_night_summary(stats):
    parts = stats["day_night"]
    counts = ", ".join(f"{parts[name]['counts']['total']} {name}" for name in DAY_NIGHT)
    return (f"day/night of the labeled frames ({counts}; night as the daemon judged it, "
            "camera schedules applied)")


def _day_night_note():
    return (f"{TOO_FEW} = fewer than {MIN_SUPPORT_DECIDED} decided frames with a score or "
            f"fewer than {MIN_SUPPORT_PER_CLASS} of either class: noise, not a threshold "
            "to ship")


def _table(headers, rows):
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]

    def line(values):
        return "  ".join(v.ljust(widths[i]) for i, v in enumerate(values)).rstrip()

    return [line(headers), line(tuple("-" * w for w in widths))] + [line(r) for r in rows]


INCIDENT_HEADERS = ("group", "incidents", "labeled", "person", "person alerted",
                    "missed", "false alarms", "delay median", "delay p90")


def _seconds(value):
    return "-" if value is None else f"{value:.0f} s"


def _incident_row(name, block):
    miss, fa, delay = block["missed"], block["false_alarms"], block["delay"]
    return (name, str(block["total"]), str(block["labeled"]), str(block["counts"]["person"]),
            str(block["person_alerted"]),
            f"{miss['count']}/{miss['person']} ({_pct(miss['rate'])})",
            f"{fa['count']}/{fa['decided']} ({_pct(fa['rate'])})",
            _seconds(delay["median"]), _seconds(delay["p90"]))


def _incident_rows(section):
    rows = [_incident_row("all", section["all"])]
    rows += [_incident_row(f"camera {cam}", block)
             for cam, block in section["cameras"].items()]
    parts = section.get("day_night") or {}
    rows += [_incident_row(name, parts[name]) for name in DAY_NIGHT
             if name in parts and (name != "unknown" or parts[name]["total"])]
    return rows


def _incident_summary(section):
    block = section["all"]
    return (f"incidents: {block['total']} from {section['frames']} frames (one incident "
            f"per incident ID, else frames of a camera less than {section['gap_seconds']:.0f}"
            f" s apart), {block['labeled']} with a labeled frame")


def _incident_notes(section):
    """Explanations and the verdicts of missed incidents, one line each."""
    block = section["all"]
    delay = block["delay"]
    out = ["person = any frame labeled person; missed = person incident without a "
           "delivered alert; false alarms = alerted incidents whose labeled frames are all "
           "no_person, of alerted incidents with a decided label",
           f"delay = event start to the first delivered alert, over {delay['n']} alerted "
           f"person incidents ({delay['from_event_start']} timed from the camera's event "
           "start or its time in the caption, the rest from their first archived frame)"]
    verdicts = block["missed_verdicts"]
    if verdicts:
        parts = []
        for name, entry in verdicts.items():
            if name == "none":
                parts.append(f"no review frame {entry['incidents']}")
            else:
                parts.append(f"{name} {entry['incidents']} (person labeled in "
                             f"{entry['person_incidents']})")
        out.append("missed person incidents by the verdicts of their review frames: "
                   + ", ".join(parts))
    return out


FIRST_ALERT_HEADERS = ("group", "path", "first alerts", "share", "delay n",
                       "delay median", "delay p90")


def _first_alert_rows(section):
    """``(group, path, incidents, share, n, median, p90)`` rows; groups without an alert
    are left out."""
    groups = [("all", section["all"])]
    groups += [(f"camera {cam}", block) for cam, block in section["cameras"].items()]
    parts = section.get("day_night") or {}
    groups += [(name, parts[name]) for name in DAY_NIGHT if name in parts]
    rows = []
    for name, block in groups:
        for path, entry in block.get("first_alert", {}).items():
            delay = entry["delay"]
            rows.append((name, path, str(entry["incidents"]), _pct(entry["share"]),
                         str(delay["n"]), _seconds(delay["median"]), _seconds(delay["p90"])))
    return rows


def _first_alert_summary():
    return ("first alert by delivery path (every alerted incident, labeled or not; delay "
            "= event start to the first delivered alert, sent by that path; unknown = a "
            "sent-log record from before the path was recorded)")


def format_incidents(section):
    out = [_incident_summary(section) + ":"]
    if not section["all"]["labeled"]:
        out.append("no incident has a labeled frame yet: label frames to measure missed "
                   "people per incident")
    else:
        out += _table(INCIDENT_HEADERS, _incident_rows(section))
        out += _incident_notes(section)
    rows = _first_alert_rows(section)
    if rows:
        out += ["", _first_alert_summary() + ":"]
        out += _table(FIRST_ALERT_HEADERS, rows)
    return out


def format_stats(stats):
    out = _table(STATS_HEADERS, _stats_rows(stats))
    by = stats.get("by") or {}
    out += ["", f"labels: {by.get('human', 0)} by a person, {by.get('auto', 0)} automatic "
                "(both models agreed)",
            _threshold_line(stats["threshold"]),
            "false alarms = sent frames labeled no_person; misses = review (held) frames "
            "labeled person; unsure excluded"]
    out += _verdict_lines(stats)
    if "day_night" in stats:
        out += ["", _day_night_summary(stats) + ":"]
        out += _table(DAY_NIGHT_HEADERS, _day_night_rows(stats))
        out.append(_day_night_note())
    if "incidents" in stats:
        out += [""] + format_incidents(stats["incidents"])
    return "\n".join(out)


# ── HTTP ─────────────────────────────────────────────────────────────────────

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Frame labeling</title>
<style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#ddd}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecece8;--muted:#9a9a94;
--card:#222220;--line:#333}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:8px 16px 16px}
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
<header><span id="progress">loading...</span><a href="/stats">stats</a></header>
<main>
<div id="work">
<img id="img" alt="frame">
<div id="meta"><span>camera <b id="camera"></b></span><span id="time"></span>
<span>source <b id="source"></b></span><span>person <b id="score"></b> <span id="band">
</span></span><span>teacher <b id="teacher"></b></span></div>
<div id="buttons">
<button data-label="person">&#9989; person <kbd>1</kbd></button>
<button data-label="no_person">&#10060; no person <kbd>2</kbd></button>
<button data-label="unsure">&#10067; unsure <kbd>3</kbd></button>
</div>
<div id="buttons2">
<button id="skip">skip <kbd>space</kbd></button>
<button id="undo">undo <kbd>u</kbd></button>
</div>
</div>
<div id="done">Nothing left to label. <button id="undo2">undo last</button></div>
</main>
<script>
let current = null, busy = false;
const $ = id => document.getElementById(id);
function show(s) {
  current = s.frame;
  $("progress").textContent = s.remaining + " left, " + s.labeled + " labeled";
  $("undo").disabled = $("undo2").disabled = !s.can_undo;
  $("work").style.display = current ? "" : "none";
  $("done").style.display = current ? "none" : "block";
  if (!current) return;
  $("img").src = "/image/" + current.path.split("/").map(encodeURIComponent).join("/");
  $("camera").textContent = current.camera || "unknown";
  $("time").textContent = current.time || "";
  $("source").textContent = current.source + (current.verdict ? " (" + current.verdict + ")" : "");
  $("score").textContent = current.score == null ? "n/a" : current.score.toFixed(2);
  $("teacher").textContent = current.teacher == null ? "n/a" : current.teacher.toFixed(2);
  $("band").textContent = "(" + current.band + ")";
}
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
function label(l) { if (current) call("/api/label", {path: current.path, label: l}); }
function skip() { if (current) call("/api/skip", {path: current.path}); }
function undo() { call("/api/undo", {}); }
document.querySelectorAll("[data-label]").forEach(b =>
  b.addEventListener("click", () => label(b.dataset.label)));
$("skip").onclick = skip; $("undo").onclick = undo; $("undo2").onclick = undo;
document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const map = {"1": "person", "2": "no_person", "3": "unsure"};
  if (map[e.key]) { e.preventDefault(); label(map[e.key]); }
  else if (e.key === " ") { e.preventDefault(); skip(); }
  else if (e.key === "u" || e.key === "Backspace") { e.preventDefault(); undo(); }
});
call("/api/next");
</script></body></html>
"""


def _html_table(headers, rows):
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in row) + "</tr>"
                   for row in rows)
    return f'<div class="wrap"><table><tr>{head}</tr>{body}</table></div>'


def stats_page(stats):
    """The ``/stats`` HTML: the same tables and lines the CLI prints."""
    extra = ""
    verdicts = _verdict_lines(stats)
    if verdicts:
        extra += "<p>" + "<br>".join(html.escape(v.strip()) for v in verdicts) + "</p>"
    if "day_night" in stats:
        extra += (f"<h2>Day and night</h2><p>{html.escape(_day_night_summary(stats))}</p>"
                  f"{_html_table(DAY_NIGHT_HEADERS, _day_night_rows(stats))}"
                  f"<p>{html.escape(_day_night_note())}</p>")
    section = stats.get("incidents")
    if section:
        extra += f"<h2>Incidents</h2><p>{html.escape(_incident_summary(section))}</p>"
        if section["all"]["labeled"]:
            extra += (_html_table(INCIDENT_HEADERS, _incident_rows(section)) + "<p>"
                      + "<br>".join(html.escape(n) for n in _incident_notes(section))
                      + "</p>")
        else:
            extra += "<p>No incident has a labeled frame yet.</p>"
        rows = _first_alert_rows(section)
        if rows:
            extra += (f"<h2>First alert by path</h2><p>{html.escape(_first_alert_summary())}"
                      f"</p>{_html_table(FIRST_ALERT_HEADERS, rows)}")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Label stats</title>
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
<p><a href="/">&larr; back to labeling</a></p>
<h1>Label stats</h1>
{_html_table(STATS_HEADERS, _stats_rows(stats))}
<p>{html.escape(_threshold_line(stats["threshold"]))}</p>
<p>False alarms = sent frames labeled no_person. Misses = review (held) frames labeled
person. Unsure is excluded from both rates.</p>
{extra}
</main></body></html>
"""


def make_server(session, port=0, bind=DEFAULT_BIND):
    """HTTP server for one :class:`LabelSession`. Stdlib only, threaded."""

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
                self._html(PAGE)
            elif path == "/api/next":
                self._json(200, session.state())
            elif path == "/stats":
                self._html(stats_page(compute_stats(session.dataset_dir,
                                                    night=session.night)))
            elif path == "/api/stats":
                self._json(200, compute_stats(session.dataset_dir, night=session.night))
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
                    session.label(payload.get("path"), payload.get("label"))
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
    args = parser.parse_args(argv)
    if not _dataset_ok(parser.prog, args.dataset_dir):
        return 2
    night = None
    if args.config:
        night = _night_from_config(parser.prog, args.config)
        if night is None:
            return 1
    session = LabelSession(args.dataset_dir, seed=args.seed, low_sample=args.low_sample,
                           night=night)
    return serve(session, args.port, args.bind)


def stats_main(argv):
    parser = argparse.ArgumentParser(
        prog="tapo-monitor label-stats",
        description="Summarize labels.jsonl: false alarms, misses, best threshold, "
                    "missed incidents")
    parser.add_argument("dataset_dir")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--config", help=CONFIG_HELP)
    args = parser.parse_args(argv)
    if not _dataset_ok(parser.prog, args.dataset_dir):
        return 2
    night = None
    if args.config:
        night = _night_from_config(parser.prog, args.config)
        if night is None:
            return 1
    stats = compute_stats(args.dataset_dir, night=night)
    if args.json_output:
        print(json.dumps(stats, sort_keys=True))
    else:
        print(format_stats(stats))
    return 0
