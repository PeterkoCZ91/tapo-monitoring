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
import time

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
    return removed


def archive_sent(archive_dir, image_bytes, caption, *, now,
                 retention_days=DEFAULT_RETENTION_DAYS, delivered=True):
    """Copy one sent frame + index line into ``archive_dir``; prune stale files.

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
        with open(os.path.join(archive_dir, INDEX_NAME), "a", encoding="utf-8") as idx:
            idx.write(json.dumps(record, ensure_ascii=False) + "\n")
        prune_old(archive_dir, now, retention_days)
        return path
    except OSError:
        log.debug("sentlog: archiving failed", exc_info=True)
        return None


def archive_if_configured(image_bytes, caption, *, delivered=True, now=None, env=None):
    """Archive a sent frame when ``TAPO_SENT_LOG_DIR`` is set; otherwise a no-op."""
    archive_dir = archive_dir_from_env(env)
    if archive_dir is None:
        return None
    now = time.time() if now is None else now
    return archive_sent(archive_dir, image_bytes, caption, now=now,
                        retention_days=retention_days_from_env(env), delivered=delivered)


def review_meta(camera, verdict, etype, score):
    """Index metadata for one suppressed frame (camera, verdict, event type, scores). Pure.

    Shared by the live pass and the sampler so both write the same review-log shape.
    ``score`` may be a plain float or a scorer result exposing ``person``/``animal``.
    """
    return {"camera": camera, "verdict": verdict, "etype": etype,
            "person": float(getattr(score, "person", score)),
            "animal": float(getattr(score, "animal", 0.0))}


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
