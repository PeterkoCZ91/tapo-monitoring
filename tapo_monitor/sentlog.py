"""Opt-in archive of the alert photos actually sent to Telegram.

A diagnostic aid: alert frames are otherwise grabbed, sent and discarded, so a false
positive leaves no image to inspect. When ``TAPO_SENT_LOG_DIR`` is set, every photo
that :func:`tapo_monitor.notify.send_photo` pushes is copied there as a timestamped
JPEG next to an ``index.jsonl`` line (timestamp, filename, caption, delivered). Files
older than the retention window (``TAPO_SENT_LOG_RETENTION_DAYS``, default 2) are pruned
on each write, so the archive self-limits to roughly a couple of nights.

Unset ``TAPO_SENT_LOG_DIR`` disables the feature entirely. Nothing here may raise into
the send path: archiving is best-effort telemetry, never a reason to lose an alert.
"""

import json
import logging
import os
import random
import time

from . import incident as incident_mod

log = logging.getLogger(__name__)

ENV_DIR = "TAPO_SENT_LOG_DIR"
ENV_RETENTION = "TAPO_SENT_LOG_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 2.0
INDEX_NAME = "index.jsonl"

# Review log: the frames corroboration *suppressed* (held, never sent). The sent log only
# keeps what went out, so it can't show whether a hold correctly dropped an animal/empty
# scene or wrongly dropped a person. Opt-in, best-effort, defaults to a week of retention.
ENV_REVIEW_DIR = "TAPO_REVIEW_LOG_DIR"
ENV_REVIEW_RETENTION = "TAPO_REVIEW_LOG_RETENTION_DAYS"
DEFAULT_REVIEW_RETENTION_DAYS = 7.0

# Drop sample: a random fraction of the frames scored below `scorer.threshold`, archived to
# the same review log. Holds only show the band just under the send line; a person the
# scorer rated p0.05 never reaches the labelling queue without this. The hourly cap per
# camera keeps a rainy night or a flapping camera from filling the disk.
ENV_DROP_SAMPLE = "TAPO_REVIEW_DROP_SAMPLE"
ENV_DROP_MAX_PER_HOUR = "TAPO_REVIEW_DROP_MAX_PER_HOUR"
DEFAULT_DROP_SAMPLE = 0.05
DEFAULT_DROP_MAX_PER_HOUR = 6

# Pan-limit log: one frame per guard intervention — the out-of-bounds view, grabbed just
# before the recall erases it. Deliberately its own directory: the review digest reads
# the review log, and twenty guard recalls a night must not flood it.
PANLIMIT_DIR_NAME = "panlimit-log"
PANLIMIT_RETENTION_DAYS = 2.0


def archive_dir_from_env(env=None):
    """Configured archive directory, or None when the feature is off. Pure."""
    env = os.environ if env is None else env
    value = (env.get(ENV_DIR) or "").strip()
    return value or None


def retention_days_from_env(env=None):
    """Retention window in days; falls back to the default on missing/garbage input."""
    env = os.environ if env is None else env
    try:
        return float(env[ENV_RETENTION])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def _stamp(now):
    whole = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"{whole}-{int((now % 1) * 1_000_000):06d}"


def prune_old(archive_dir, now, retention_days):
    """Delete archived JPEGs older than the retention window. Returns count removed."""
    cutoff = now - retention_days * 86400
    removed = 0
    try:
        entries = os.listdir(archive_dir)
    except OSError:
        return 0
    for name in entries:
        if not name.endswith(".jpg"):
            continue
        path = os.path.join(archive_dir, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            log.debug("sentlog: could not prune %s", path, exc_info=True)
    index = os.path.join(archive_dir, INDEX_NAME)
    try:
        if os.path.exists(index) and os.path.getmtime(index) < cutoff:
            # The index only ever grew: once it is older than the window, every line in
            # it points at a JPEG that was pruned nights ago.
            os.unlink(index)
    except OSError:
        log.debug("sentlog: could not rotate %s", index, exc_info=True)
    return removed


def archive_sent(archive_dir, image_bytes, caption, *, now,
                 retention_days=DEFAULT_RETENTION_DAYS, delivered=True,
                 camera=None, score=None, incident=None):
    """Copy one sent frame + index line into ``archive_dir``; prune stale files.

    ``camera`` and ``score`` are optional: a host running two cameras cannot otherwise
    tell from the index which one sent what. ``incident`` adds the incident ID and its
    ``event_start``, so the frames of one visit group without guessing from timestamps. Absent values are left out rather than
    written as null, so a reader of the old shape sees exactly what it always saw.

    Returns the saved JPEG path, or None on any failure — it never raises, so a full
    disk or a bad path degrades to "no archive", never a lost alert.
    """
    try:
        os.makedirs(archive_dir, exist_ok=True)
        name = f"{_stamp(now)}.jpg"
        path = os.path.join(archive_dir, name)
        with open(path, "wb") as f:
            f.write(image_bytes)
        record = {"ts": now, "file": name, "caption": caption, "delivered": bool(delivered)}
        if camera:
            record["camera"] = camera
        record.update(incident_mod.index_fields(incident))
        if score is not None and hasattr(score, "person"):
            record["person"] = float(score.person)
            record["animal"] = float(score.animal)
        with open(os.path.join(archive_dir, INDEX_NAME), "a", encoding="utf-8") as idx:
            idx.write(json.dumps(record, ensure_ascii=False) + "\n")
        prune_old(archive_dir, now, retention_days)
        return path
    except OSError:
        log.debug("sentlog: archiving failed", exc_info=True)
        return None


def archive_if_configured(image_bytes, caption, *, delivered=True, now=None, env=None,
                          camera=None, score=None, incident=None):
    """Archive a sent frame when ``TAPO_SENT_LOG_DIR`` is set; otherwise a no-op."""
    archive_dir = archive_dir_from_env(env)
    if archive_dir is None:
        return None
    now = time.time() if now is None else now
    return archive_sent(archive_dir, image_bytes, caption, now=now,
                        retention_days=retention_days_from_env(env), delivered=delivered,
                        camera=camera, score=score, incident=incident)


def panlimit_dir_from_env(env=None):
    """``panlimit-log`` directory next to the review-log (or sent-log) dir, or None. Pure.

    No knob of its own: the guard's evidence lands beside whichever archive the host
    already keeps; with neither configured there is nowhere sane to write.
    """
    env = os.environ if env is None else env
    for key in (ENV_REVIEW_DIR, ENV_DIR):
        base = (env.get(key) or "").strip().rstrip("/")
        if base:
            return os.path.join(os.path.dirname(base), PANLIMIT_DIR_NAME)
    return None


def archive_panlimit_frame(archive_dir, image_path, camera, axis, value, *, now):
    """Copy one guard-intervention frame into ``archive_dir``; prune files past 2 days.

    The filename carries the camera, axis and out-of-bounds position, so a night of
    recalls is skimmable without an index. Returns the saved path, or None on any
    failure — evidence is best-effort and must never cost a recall.
    """
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        os.makedirs(archive_dir, exist_ok=True)
        name = f"panlimit_{camera}_{axis}{float(value):+.4f}_{_stamp(now)}.jpg"
        path = os.path.join(archive_dir, name)
        with open(path, "wb") as f:
            f.write(image_bytes)
        prune_old(archive_dir, now, PANLIMIT_RETENTION_DAYS)
        return path
    except (OSError, TypeError, ValueError):
        log.debug("sentlog: panlimit archiving failed", exc_info=True)
        return None


def review_meta(camera, verdict, etype, score, event=None):
    """Index metadata for one suppressed frame (camera, verdict, event type, scores). Pure.

    Shared by the live pass and the sampler so both write the same review-log shape.
    ``score`` may be a plain float or a scorer result exposing ``person``/``animal``.
    ``event`` is the camera event behind the frame: it adds ``incident`` and
    ``event_start``, the same fields the sent log carries. Without one they are omitted.
    """
    return {"camera": camera, "verdict": verdict, "etype": etype,
            "person": float(getattr(score, "person", score)),
            "animal": float(getattr(score, "animal", 0.0)),
            **incident_mod.index_fields(incident_mod.incident_id(camera, event))}


def _review_score_tag(meta):
    person = meta.get("person")
    return f"_p{person:.2f}" if isinstance(person, (int, float)) else ""


def archive_review_frame(archive_dir, image_bytes, meta, *, now, retention_days):
    """Copy one suppressed frame + index line into ``archive_dir``; prune stale files.

    The filename carries the camera, verdict and person score so the archive is skimmable
    without opening the index. Returns the saved path, or None on any failure (never raises).
    """
    try:
        os.makedirs(archive_dir, exist_ok=True)
        cam = str(meta.get("camera", "cam"))
        verdict = str(meta.get("verdict", "hold"))
        name = f"{cam}_{verdict}{_review_score_tag(meta)}_{_stamp(now)}.jpg"
        path = os.path.join(archive_dir, name)
        with open(path, "wb") as f:
            f.write(image_bytes)
        record = {"ts": now, "file": name, **meta}
        with open(os.path.join(archive_dir, INDEX_NAME), "a", encoding="utf-8") as idx:
            idx.write(json.dumps(record, ensure_ascii=False) + "\n")
        prune_old(archive_dir, now, retention_days)
        return path
    except OSError:
        log.debug("sentlog: review archiving failed", exc_info=True)
        return None


def archive_review_if_configured(image_path, meta, *, now=None, env=None):
    """Archive a suppressed frame when ``TAPO_REVIEW_LOG_DIR`` is set; otherwise a no-op.

    Reads the frame from ``image_path`` (the daemon still owns/unlinks it). Best-effort:
    a missing file, unset env or write error degrades to None, never into the alert path.
    """
    env = os.environ if env is None else env
    archive_dir = (env.get(ENV_REVIEW_DIR) or "").strip() or None
    if archive_dir is None:
        return None
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
    except OSError:
        return None
    now = time.time() if now is None else now
    try:
        retention_days = float(env[ENV_REVIEW_RETENTION])
    except (KeyError, TypeError, ValueError):
        retention_days = DEFAULT_REVIEW_RETENTION_DAYS
    return archive_review_frame(archive_dir, image_bytes, meta, now=now,
                                retention_days=retention_days)


def drop_sample_rate_from_env(env=None):
    """Fraction of below-threshold frames to archive, 0..1; garbage falls back to default."""
    env = os.environ if env is None else env
    try:
        rate = float(env[ENV_DROP_SAMPLE])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_DROP_SAMPLE
    return rate if 0.0 <= rate <= 1.0 else DEFAULT_DROP_SAMPLE


def drop_max_per_hour_from_env(env=None):
    """Sampled drops archived per camera and clock hour; garbage falls back to default."""
    env = os.environ if env is None else env
    try:
        cap = int(env[ENV_DROP_MAX_PER_HOUR])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_DROP_MAX_PER_HOUR
    return cap if cap >= 0 else DEFAULT_DROP_MAX_PER_HOUR


class DropSampleCap:
    """Per-camera count of sampled drops in the current clock hour.

    In memory only: a restart forgets the hour's count, which at worst admits one more
    hour's worth of frames. Past hours are forgotten on the next count, so it stays tiny.
    """

    def __init__(self):
        self._counts = {}

    def allows(self, camera, now, cap):
        return self._counts.get((camera, int(now // 3600)), 0) < cap

    def count(self, camera, now):
        hour = int(now // 3600)
        self._counts = {k: v for k, v in self._counts.items() if k[1] == hour}
        self._counts[(camera, hour)] = self._counts.get((camera, hour), 0) + 1


_drop_cap = DropSampleCap()


def archive_drop_sample_if_configured(image_path, meta, *, now=None, env=None, rng=None,
                                      cap=None):
    """Archive a random sample of below-threshold frames to the review log.

    No-op unless ``TAPO_REVIEW_LOG_DIR`` is set. Each frame is kept with probability
    ``TAPO_REVIEW_DROP_SAMPLE`` (default 5 %), at most ``TAPO_REVIEW_DROP_MAX_PER_HOUR``
    per camera and clock hour. The index record carries ``sample_rate`` so statistics can
    weight the sample; a ``drop`` record without it (the hub poll's) was archived in full.
    Returns the saved path or None, and never raises: it runs on the alert path, and a
    sampling failure must not change a single decision.
    """
    try:
        env = os.environ if env is None else env
        if not (env.get(ENV_REVIEW_DIR) or "").strip():
            return None
        rate = drop_sample_rate_from_env(env)
        if rate <= 0.0 or (rng or random).random() >= rate:
            return None
        now = time.time() if now is None else now
        cap = _drop_cap if cap is None else cap
        camera = str(meta.get("camera", "cam"))
        if not cap.allows(camera, now, drop_max_per_hour_from_env(env)):
            return None
        path = archive_review_if_configured(image_path, {**meta, "sample_rate": rate},
                                            now=now, env=env)
        if path:
            cap.count(camera, now)
        return path
    except Exception:  # noqa: BLE001 - sampling is telemetry, never a reason to fail a pass
        log.debug("sentlog: drop sampling failed", exc_info=True)
        return None
