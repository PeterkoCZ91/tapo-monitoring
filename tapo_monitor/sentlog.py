"""Opt-in archive of the alert photos actually sent to Telegram.

A diagnostic aid: alert frames are otherwise grabbed, sent and discarded, so a false
positive leaves no image to inspect. When ``TAPO_SENT_LOG_DIR`` is set, every photo
that :func:`tapo_monitor.notify.send_photo` pushes is copied there as a timestamped
JPEG next to an ``index.jsonl`` line (timestamp, filename, caption, delivered). Files
older than the retention window (``TAPO_SENT_LOG_RETENTION_DAYS``, default 2) are pruned
on each write. Daily background maintenance also cleans quiet archives and expires
index records using their timestamps, even when the index is still being appended.

Unset ``TAPO_SENT_LOG_DIR`` disables the feature entirely. Nothing here may raise into
the send path: archiving is best-effort telemetry, never a reason to lose an alert.
"""

import fcntl
import json
import logging
import math
import os
import random
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager

from . import incident as incident_mod

log = logging.getLogger(__name__)

ENV_DIR = "TAPO_SENT_LOG_DIR"
ENV_RETENTION = "TAPO_SENT_LOG_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 2.0
INDEX_NAME = "index.jsonl"

# Which delivery path sent a frame: the sent-log index's ``path`` field. The names are
# the audit paths (``live``, ``sampler``, ``sd``, ``hubpoll``) where a path sends in one
# way; the sends the audit tells apart only by ``reason`` get a name of their own here,
# because each has its own latency: ``hubpoll_retry`` (audit ``hubpoll`` / ``retry``),
# ``hold_rescue`` (``sampler`` / ``hold_rescue_recall``) and ``hold_expiry``
# (``sampler`` / ``hold_expiry_send``). ``sd`` covers the SD clip and the recording
# follow-up alike, as the audit does. ``outbox`` is a late delivery after an outage
# (see tapo_monitor.outbox); its latency is the outage, not a pipeline's.
SEND_PATHS = ("live", "sampler", "sd", "hubpoll", "hubpoll_retry", "hold_rescue",
              "hold_expiry", "outbox")

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

# Disk telemetry: 14-day retention plus the drop sample must never be what fills a host's
# disk. The digest reports each log's size and file count; below the free-space floor the
# daemon warns once. 0 turns the warning off. The scan is flat and capped per directory,
# so a runaway log costs a bounded directory walk, never a stalled tick.
ENV_LOG_DISK_MIN_FREE = "TAPO_LOG_DISK_MIN_FREE_MB"
DEFAULT_LOG_DISK_MIN_FREE_MB = 1024
USAGE_MAX_ENTRIES = 50_000
_MB = 1024 * 1024


def archive_dir_from_env(env=None):
    """Configured archive directory, or None when the feature is off. Pure."""
    env = os.environ if env is None else env
    value = (env.get(ENV_DIR) or "").strip()
    return value or None


def retention_days_from_env(env=None):
    """Retention window in days; falls back to the default on missing/garbage input."""
    env = os.environ if env is None else env
    try:
        value = float(env[ENV_RETENTION])
        return value if math.isfinite(value) and value > 0 else DEFAULT_RETENTION_DAYS
    except (KeyError, TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def _stamp(now):
    whole = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"{whole}-{int((now % 1) * 1_000_000):06d}"


@contextmanager
def _index_lock(archive_dir):
    # Lock a stable sidecar: replacing the index must not replace its lock inode.
    with open(os.path.join(archive_dir, ".index.lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _append_index(archive_dir, record):
    with _index_lock(archive_dir):
        with open(os.path.join(archive_dir, INDEX_NAME), "a", encoding="utf-8") as index:
            index.write(json.dumps(record, ensure_ascii=False) + "\n")


def compact_index(archive_dir, now, retention_days):
    """Remove expired metadata without holding the append lock during the scan.

    Unknown or malformed records are preserved. If an append raced the snapshot,
    discard the candidate and try on the next maintenance pass, never losing a row.
    """
    path = os.path.join(archive_dir, INDEX_NAME)
    candidate = None
    removed = 0
    try:
        with open(path, "rb") as source:
            snapshot = os.fstat(source.fileno())
            with tempfile.NamedTemporaryFile(dir=archive_dir, prefix=".index-",
                                             delete=False) as output:
                candidate = output.name
                remaining = snapshot.st_size
                while remaining:
                    line = source.readline(remaining)
                    if not line:
                        break
                    remaining -= len(line)
                    expired = False
                    try:
                        record = json.loads(line)
                        ts = record.get("ts") if isinstance(record, dict) else None
                        expired = (not isinstance(ts, bool) and isinstance(ts, (int, float))
                                   and math.isfinite(ts) and ts < now - retention_days * 86400)
                    except (ValueError, UnicodeError):
                        pass
                    if expired:
                        removed += 1
                    else:
                        output.write(line)
        if removed:
            with _index_lock(archive_dir):
                current = os.stat(path)
                if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                        snapshot.st_ino, snapshot.st_size, snapshot.st_mtime_ns):
                    return 0
                os.chmod(candidate, snapshot.st_mode & 0o777)
                os.replace(candidate, path)
                candidate = None
        return removed
    except OSError:
        log.debug("sentlog: metadata compaction failed", exc_info=True)
        return 0
    finally:
        if candidate is not None:
            try:
                os.unlink(candidate)
            except OSError:
                pass


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


class RetentionMaintenance:
    """Daily archive and ledger retention independent of incoming camera events.

    ``tick`` only schedules work. A single daemon thread scans metadata and removes
    expired rows in small SQLite transactions, keeping maintenance off the alert loop.
    """

    def __init__(self, event_ledger=None, ledger_retention_days=30, env=None):
        self.event_ledger = event_ledger
        try:
            days = float(ledger_retention_days)
        except (TypeError, ValueError):
            days = 30.0
        self.ledger_retention_days = days if math.isfinite(days) and days > 0 else 30.0
        self.env = dict(os.environ if env is None else env)
        self._next = float("-inf")
        self._worker = None

    def tick(self, now):
        if now < self._next or (self._worker is not None and self._worker.is_alive()):
            return False
        self._next = now + 86400
        self._worker = threading.Thread(target=self._run, args=(now,),
                                        name="tapo-retention", daemon=True)
        self._worker.start()
        return True

    def _run(self, now):
        retentions = {"sent": retention_days_from_env(self.env),
                      "pan-limit": PANLIMIT_RETENTION_DAYS}
        try:
            retentions["review"] = float(self.env[ENV_REVIEW_RETENTION])
            if not math.isfinite(retentions["review"]) or retentions["review"] <= 0:
                retentions["review"] = DEFAULT_REVIEW_RETENTION_DAYS
        except (KeyError, TypeError, ValueError):
            retentions["review"] = DEFAULT_REVIEW_RETENTION_DAYS
        for name, path in log_dirs_from_env(self.env):
            try:
                days = retentions[name]
                prune_old(path, now, days)
                compact_index(path, now, days)
            except Exception:  # noqa: BLE001 - maintenance must not break alerting
                log.warning("retention: archive cleanup failed", exc_info=True)
        if self.event_ledger is not None:
            try:
                self.event_ledger.cleanup_bounded(self.ledger_retention_days * 86400, now=now)
            except Exception:  # noqa: BLE001 - maintenance is best effort
                log.warning("retention: ledger cleanup failed", exc_info=True)


def archive_sent(archive_dir, image_bytes, caption, *, now,
                 retention_days=DEFAULT_RETENTION_DAYS, delivered=True,
                 camera=None, score=None, incident=None, send_path=None):
    """Copy one sent frame + index line into ``archive_dir``; prune stale files.

    ``camera`` and ``score`` are optional: a host running two cameras cannot otherwise
    tell from the index which one sent what. ``incident`` adds the incident ID and its
    ``event_start``, so the frames of one visit group without guessing from timestamps.
    ``send_path`` is written as ``path``: the delivery path that sent the frame (see
    :data:`SEND_PATHS`). Absent values are left out rather than written as null, so a
    reader of the old shape sees exactly what it always saw.

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
        if send_path:
            record["path"] = str(send_path)
        if score is not None and hasattr(score, "person"):
            record["person"] = float(score.person)
            record["animal"] = float(score.animal)
        _append_index(archive_dir, record)
        prune_old(archive_dir, now, retention_days)
        return path
    except OSError:
        log.debug("sentlog: archiving failed", exc_info=True)
        return None


def archive_if_configured(image_bytes, caption, *, delivered=True, now=None, env=None,
                          camera=None, score=None, incident=None, send_path=None):
    """Archive a sent frame when ``TAPO_SENT_LOG_DIR`` is set; otherwise a no-op."""
    archive_dir = archive_dir_from_env(env)
    if archive_dir is None:
        return None
    now = time.time() if now is None else now
    return archive_sent(archive_dir, image_bytes, caption, now=now,
                        retention_days=retention_days_from_env(env), delivered=delivered,
                        camera=camera, score=score, incident=incident,
                        send_path=send_path)


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
        _append_index(archive_dir, record)
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
        if not math.isfinite(retention_days) or retention_days <= 0:
            retention_days = DEFAULT_REVIEW_RETENTION_DAYS
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


def log_dirs_from_env(env=None):
    """``(name, path)`` for each configured archive: sent, review, pan-limit. Pure."""
    env = os.environ if env is None else env
    dirs = [("sent", archive_dir_from_env(env)),
            ("review", (env.get(ENV_REVIEW_DIR) or "").strip() or None),
            ("pan-limit", panlimit_dir_from_env(env))]
    return [(name, path) for name, path in dirs if path]


def log_disk_min_free_mb_from_env(env=None):
    """Free-space floor in MB for the log filesystem; 0 disables, garbage -> default."""
    env = os.environ if env is None else env
    try:
        floor = int(env[ENV_LOG_DISK_MIN_FREE])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_LOG_DISK_MIN_FREE_MB
    return floor if floor >= 0 else DEFAULT_LOG_DISK_MIN_FREE_MB


def dir_usage(path, max_entries=USAGE_MAX_ENTRIES):
    """``(bytes, files, complete)`` of the files directly in ``path``; None if unreadable.

    Flat on purpose: every archive here is one directory. ``complete`` is False when the
    scan stopped at ``max_entries``, and the numbers are then a lower bound.
    """
    total = files = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if files >= max_entries:
                    return total, files, False
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                        files += 1
                except OSError:
                    continue
    except OSError:
        return None
    return total, files, True


def free_mb(paths):
    """Least free space in MB across the filesystems holding ``paths``; None if none exist."""
    free = []
    for path in paths:
        try:
            free.append(shutil.disk_usage(path).free / _MB)
        except OSError:
            continue
    return min(free) if free else None


def log_usage(env=None):
    """Size, file count and free space of the archives, for the daily digest.

    None when no archive is configured. A directory not created yet (no pan-limit recall so
    far) is left out rather than reported empty. Never raises: it feeds a heartbeat.
    """
    try:
        dirs = log_dirs_from_env(env)
        if not dirs:
            return None
        usage = []
        for name, path in dirs:
            found = dir_usage(path)
            if found is not None:
                size, files, complete = found
                usage.append({"name": name, "mb": size / _MB, "files": files,
                              "complete": complete})
        return {"dirs": usage, "free_mb": free_mb([path for _, path in dirs]),
                "floor_mb": log_disk_min_free_mb_from_env(env)}
    except Exception:  # noqa: BLE001 - telemetry, never a reason to lose the digest
        log.debug("sentlog: log usage failed", exc_info=True)
        return None
