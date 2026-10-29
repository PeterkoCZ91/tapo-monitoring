"""Opt-in persistent outbox for alerts Telegram never took ("delayed" delivery).

The in-memory retries (the SD follow-up, the hub retry) give up after about ten minutes,
which covers a Telegram blip but not an outage: a site whose internet was down overnight
lost every alert of the night, persons included, without a trace on the phone. With the
``outbox:`` block enabled, :func:`tapo_monitor.daemon.send_alert_photo` copies each
alert frame whose real send failed into a directory, one JPEG plus a JSON sidecar per
incident, and the daemon drains that directory once Telegram answers again: one summary
text per camera, then the person photos (capped), the rest to the review log.

Keyed by incident ID (:func:`tapo_monitor.incident.incident_id`): every path computes
the same ID for one camera event, so a later delivery of the same incident by any path
removes its entry (:meth:`Outbox.resolve`) and nothing reaches the phone twice. A path
that deliberately decides *not* to send an incident resolves it too, so the outbox never
revives an alert the pipeline suppressed. On disk on purpose, so a restart during the
outage keeps what was captured.

Only failures of a real send are captured. A collect-only or app-silenced camera reports
success, and an event in a camera's mute window never reaches the send path at all — so
nothing here needs to know about mute windows: what was never going to be sent live is
never sent late either.

Nothing here may raise into the send path; every disk error degrades to a log line.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace

from . import incident as incident_mod

log = logging.getLogger(__name__)

# How long an entry waits before a drain may take it: the in-memory retries run first
# (they send the real, current caption and arm the cooldowns); the outbox is the net
# under them, not a second sender racing them.
MIN_AGE = 120
# How often Telegram is probed while it is unreachable, and how long a successful send
# or probe counts as proof that it is back.
PROBE_INTERVAL = 60
FRESH_OK = 60
# Failed late sends of one entry before it goes to the review log instead: one photo
# Telegram refuses (a broken frame) must not hold up everything queued behind it.
MAX_SEND_ATTEMPTS = 3
# An unscored non-person entry waits this long (since its failure) for the scorer to
# come back before its camera event type decides alone. The event type says "motion"
# for most persons, so deciding on it early would bury them in the review log.
UNSCORED_WAIT = 6 * 3600
# Per-tick budget of one drain. The drain runs on the main loop, between live polls: a
# backlog of 300 entries must trickle out over ticks, not stall detection for minutes.
MAX_RESCORES_PER_TICK = 5
MAX_SENDS_PER_TICK = 3
TICK_BUDGET_S = 10.0
# Eviction only (the box knows no camera threshold): a stored score at least this high
# counts as a person when a full outbox picks what to drop first.
EVICT_PERSON_HINT = 0.5
# Leftovers older than this are swept: a temp file from an interrupted write, or a frame
# whose sidecar never got written.
STALE_AFTER = 3600
# Suffixes of one entry. The JSON is written last and is what a listing reads, so a
# crash between the two writes leaves an orphan frame, never a sidecar without a frame.
_IMAGE = ".jpg"
_META = ".json"
_TMP = ".tmp"
# An ``announced`` flag never confirmed by a successful send (the process died between the
# write and the send) is trusted for this long, then the summary goes out again.
ANNOUNCE_CONFIRM_WAIT = 300


@dataclass
class Entry:
    """One undelivered alert as read back from disk."""
    key: str
    camera: str
    image: str                          # path of the stored frame
    caption: str
    failed_at: float
    incident: str | None = None
    event_start: float | None = None
    etype: str | None = None
    score: dict | None = None           # {"person", "animal", "persons"} or None
    send_path: str | None = None
    announced: bool = False             # a summary counting this entry went out
    announce_started_at: float | None = None   # when that summary was about to be sent
    announce_sent: bool = False         # ...and the send was confirmed
    attempts: int = 0                   # failed late sends so far

    @property
    def event_time(self) -> float:
        """When the event happened: the camera's start, else when the send failed."""
        return self.event_start if self.event_start is not None else self.failed_at


def plural(n, one, few, many) -> str:
    """Czech plural form for ``n``: 1 → one, 2–4 → few, else many. Pure."""
    n = abs(int(n))
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def entry_key(camera, incident, failed_at) -> str:
    """File stem for one entry: the incident ID, else camera + failure time. Pure.

    Anything outside ``[A-Za-z0-9_.-]`` becomes ``_`` so a camera name can never escape
    the directory or collide with the temp suffix.
    """
    raw = incident or f"{camera}-f{int(float(failed_at) * 1000)}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(raw)).lstrip(".") or "entry"


def score_fields(score):
    """JSON-safe form of a scorer result (float or SubjectScore), or None. Pure."""
    if score is None:
        return None
    try:
        out = {"person": float(getattr(score, "person", score)),
               "animal": float(getattr(score, "animal", 0.0) or 0.0)}
    except (TypeError, ValueError):
        return None
    persons = getattr(score, "persons", None)
    if isinstance(persons, int):
        out["persons"] = persons
    return out


def is_person(entry: Entry, threshold) -> bool:
    """Whether a late entry deserves a phone photo. Pure.

    A scored entry must reach the camera's threshold; an unscored one (the scorer was
    down too) falls back to the camera's own AI: its ``person`` event type.
    """
    if entry.score is not None and threshold is not None:
        try:
            return float(entry.score["person"]) >= float(threshold)
        except (KeyError, TypeError, ValueError):
            pass
    return entry.etype == "person"


def _likely_person(entry: Entry) -> bool:
    if entry.etype == "person":
        return True
    try:
        return entry.score is not None and float(entry.score["person"]) >= EVICT_PERSON_HINT
    except (KeyError, TypeError, ValueError):
        return False


def format_delay(seconds) -> str:
    """``"2 h 5 min"`` / ``"7 min"``: how late a photo arrives. Pure."""
    minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def delayed_caption(entry: Entry, now, lang="en") -> str:
    """The original caption behind a ``⏳ delayed by …`` prefix (``lang`` en/cs). Pure."""
    prefix = "zpožděno o" if lang == "cs" else "delayed by"
    return f"⏳ {prefix} {format_delay(now - entry.event_time)} · {entry.caption}"


def _clock(ts, with_date):
    return time.strftime("%d.%m. %H:%M" if with_date else "%H:%M", time.localtime(ts))


def summary_text(camera, entries, persons, to_phone, now, review_enabled=True,
                 lang="en") -> str:
    """The one message that opens a camera's late batch (HTML-escaped, ``lang`` en/cs). Pure.

    Names the outage window (first and last failed send), how many alerts were lost and
    how many showed a person, and where the rest went, so the photos that follow read as
    old news rather than as something happening now. ``entries`` are the ones this
    summary is the first to report; the rest "stay in the review log" only when the
    review log is configured — otherwise they are, truthfully, not sent at all.
    """
    first = min(e.failed_at for e in entries)
    last = max(e.failed_at for e in entries)
    with_date = (time.localtime(first)[:3] != time.localtime(last)[:3]
                 or time.localtime(first)[:3] != time.localtime(now)[:3])
    n = len(entries)
    window = f"{_clock(first, with_date)}–{_clock(last, with_date)}"
    rest = n - to_phone
    if lang != "cs":
        lines = [f"⏳ Connection outage {window} · {camera} — {n} undelivered "
                 f"{'event' if n == 1 else 'events'}, {persons} with a person."]
        if to_phone:
            lines.append(f"Sending {to_phone} delayed person "
                         f"{'photo' if to_phone == 1 else 'photos'}:")
        if rest:
            where = "only in the review log" if review_enabled else "not sent"
            lines.append(f"+{rest} more {where}.")
        return html.escape("\n".join(lines), quote=False)
    lines = [f"⏳ Výpadek spojení {window}"
             f" · {camera} — nedoručeno {n} {plural(n, 'událost', 'události', 'událostí')},"
             f" z toho {persons} s osobou."]
    if to_phone:
        lines.append(f"Posílám zpožděně {to_phone} "
                     f"{plural(to_phone, 'fotku', 'fotky', 'fotek')} s osobou:")
    if rest:
        where = "jen v review logu" if review_enabled else "neodesláno"
        lines.append(f"+{rest} {plural(rest, 'další', 'další', 'dalších')} {where}.")
    return html.escape("\n".join(lines), quote=False)


def plan_batch(entries, person_flags, max_photos):
    """Split one camera's batch into (phone, review): the first persons up to the cap. Pure.

    ``entries`` in time order; ``person_flags`` parallel to it. Everything not sent to
    the phone — non-persons and the persons past the cap — goes to the review log.
    """
    phone, review = [], []
    for entry, person in zip(entries, person_flags, strict=True):
        if person and len(phone) < max_photos:
            phone.append(entry)
        else:
            review.append(entry)
    return phone, review


def review_meta(entry: Entry, now, reason=None) -> dict:
    """Review-log index fields for an entry the drain keeps off the phone. Pure.

    ``person``/``animal`` are left out for an unscored entry: the review digest reads a
    present ``person`` as a number.
    """
    meta = {"camera": entry.camera, "verdict": "outbox", "etype": entry.etype,
            "failed_at": entry.failed_at, "delay_s": int(now - entry.event_time),
            **incident_mod.index_fields(entry.incident)}
    if entry.score is not None:
        meta["person"] = entry.score.get("person")
        meta["animal"] = entry.score.get("animal", 0.0)
    if reason:
        meta["reason"] = reason
    return meta


class Outbox:
    """The outbox directory plus the in-memory Telegram reachability probe state."""

    def __init__(self, directory, *, max_age=24 * 3600, max_entries=300,
                 max_bytes=64 * 1024 * 1024, max_photos=10,
                 summary=True, min_age=MIN_AGE, lang="en"):
        self.dir = directory
        self.max_age = max_age
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_photos = max_photos
        self.summary = summary
        self.lang = lang
        self.min_age = min_age
        self.last_ok_at: float | None = None
        self.next_probe_at = 0.0
        self._budget_lock = threading.RLock()
        self._borrowed: dict[str, int] = {}
        self._retired: set[str] = set()
        self._locks_guard = threading.Lock()
        self._key_locks: dict[str, threading.RLock] = {}

    def _lock(self, key):
        """One lock per entry: capture/update/remove are read-modify-write on its files."""
        with self._locks_guard:
            return self._key_locks.setdefault(key, threading.RLock())

    # ── storage ──────────────────────────────────────────────────────────────
    def _path(self, key, suffix):
        return os.path.join(self.dir, key + suffix)

    def _image_path(self, key, meta):
        """Only this entry's canonical or versioned JPEG, never an arbitrary sidecar path."""
        name = meta.get("image_file", key + _IMAGE)
        if not isinstance(name, str) or not re.fullmatch(
                re.escape(key) + r"(?:\.[0-9a-f]{8})?\.jpg", name):
            return None
        return os.path.join(self.dir, name)

    def _retire_image(self, path):
        """Do not unlink a JPEG while a drain still holds its Entry snapshot."""
        if self._borrowed.get(path, 0):
            self._retired.add(path)
            return
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("outbox: could not remove old frame %s", os.path.basename(path))

    @contextmanager
    def borrow_entries(self):
        with self._budget_lock:
            entries = self.entries()
            for entry in entries:
                self._borrowed[entry.image] = self._borrowed.get(entry.image, 0) + 1
        try:
            yield entries
        finally:
            with self._budget_lock:
                for entry in entries:
                    path = entry.image
                    self._borrowed[path] -= 1
                    if not self._borrowed[path]:
                        del self._borrowed[path]
                        if path in self._retired:
                            self._retired.remove(path)
                            self._retire_image(path)

    def _write_atomic(self, path, data: bytes):
        # fsync before the rename: after a power cut the name must not point at an empty
        # file, which the next start would have to throw away as corrupt.
        tmp = f"{path}.{uuid.uuid4().hex[:8]}{_TMP}"
        try:
            with open(tmp, "wb", opener=lambda p, f: os.open(p, f, 0o600)) as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _write_meta(self, key, meta):
        self._write_atomic(self._path(key, _META),
                           json.dumps(meta, ensure_ascii=False).encode())

    def _load_meta(self, key):
        """``(meta, corrupt)``: the sidecar, or None with whether it is unusable for good.

        Only a sidecar that reads but does not parse (or is empty) is corrupt; an I/O
        error may pass (a full disk, a permission fixed later) and says nothing about it.
        """
        try:
            with open(self._path(key, _META), encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            return None, False
        try:
            meta = json.loads(raw)
        except ValueError:
            return None, True
        return (meta, False) if isinstance(meta, dict) else (None, True)

    def _read_meta(self, key):
        return self._load_meta(key)[0]

    def capture(self, *, camera, image, caption, failed_at, incident=None, etype=None,
                score=None, send_path=None):
        with self._budget_lock:
            return self._capture_locked(camera=camera, image=image, caption=caption,
                                        failed_at=failed_at, incident=incident, etype=etype,
                                        score=score, send_path=send_path)

    def _capture_locked(self, *, camera, image, caption, failed_at, incident=None, etype=None,
                        score=None, send_path=None):
        """Store one undelivered alert; returns the key, or None when it failed.

        A second failure of the same incident (the SD follow-up retrying every minute)
        replaces the frame and caption — the later path usually picked the better frame —
        but never loses what the first one knew: the first failure time (when the outage
        began for it), a ``person`` event type, a score where the retry has none, and the
        drain's own bookkeeping.
        """
        try:
            with open(image, "rb") as fh:
                frame = fh.read()
        except (OSError, TypeError):
            log.warning("outbox: frame for %s unreadable, alert not kept", camera)
            return None
        key = entry_key(camera, incident, failed_at)
        lock = None
        try:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            lock = self._lock(key)
            lock.acquire()
            previous = self._read_meta(key) or {}
            event_start = None
            if incident:
                event_start = incident_mod.index_fields(incident).get("event_start")
            stored_score = score_fields(score)
            if stored_score is None:
                stored_score = previous.get("score")
            if previous.get("etype") == "person":
                etype = "person"
            meta = {"camera": camera, "caption": caption,
                    "failed_at": previous.get("failed_at", failed_at),
                    "incident": incident, "event_start": event_start, "etype": etype,
                    "score": stored_score, "send_path": send_path,
                    "announced": bool(previous.get("announced", False)),
                    "announce_started_at": previous.get("announce_started_at"),
                    "announce_sent": bool(previous.get("announce_sent", False)),
                    "attempts": int(previous.get("attempts", 0) or 0)}
            old_image = self._image_path(key, previous) if previous else None
            image = self._path(key, _IMAGE)
            if previous or self._borrowed.get(image, 0) or image in self._retired:
                # A borrowed canonical path may survive removal of its sidecar;
                # never overwrite it before the in-flight drain releases it.
                image = self._path(key, "." + uuid.uuid4().hex[:8] + _IMAGE)
                meta["image_file"] = os.path.basename(image)
            if len(frame) + len(json.dumps(meta, ensure_ascii=False).encode()) > self.max_bytes:
                log.warning("outbox: byte budget (%d) cannot hold %s, alert not kept",
                            self.max_bytes, key)
                return None
            try:
                self._write_atomic(image, frame)
                self._write_meta(key, meta)
            except OSError:
                if image != old_image:
                    try:
                        os.unlink(image)
                    except OSError:
                        pass
                raise
            if old_image and old_image != image:
                self._retire_image(old_image)
        except OSError:
            log.warning("outbox: could not store the alert for %s", camera, exc_info=True)
            return None
        finally:
            if lock is not None:
                lock.release()
        if not previous:
            log.info("outbox: kept undelivered %s alert for %s (%s)",
                     etype or "?", camera, incident or key)
        self._enforce_cap()
        return key if os.path.exists(self._path(key, _META)) else None

    def remove(self, key):
        with self._budget_lock:
            self._remove_locked(key)

    def _remove_locked(self, key):
        with self._lock(key):
            meta = self._read_meta(key) or {}
            image = self._image_path(key, meta)
            for path in (self._path(key, _META), self._path(key, _IMAGE), image):
                if path is None:
                    continue
                try:
                    if path == self._path(key, _META):
                        os.remove(path)
                    else:
                        self._retire_image(path)
                except FileNotFoundError:
                    pass
                except OSError:
                    log.debug("outbox: could not remove %s", key, exc_info=True)
        with self._locks_guard:       # the entry is gone: do not keep its lock forever
            self._key_locks.pop(key, None)

    def resolve(self, incident, why="delivered by another path") -> bool:
        """Forget ``incident``: some path delivered it, or decided it must not go out."""
        if not incident:
            return False
        key = entry_key(None, incident, 0)
        if not os.path.exists(self._path(key, _META)):
            return False
        self.remove(key)
        log.info("outbox: %s %s, entry removed", incident, why)
        return True

    def _names(self):
        try:
            return os.listdir(self.dir)
        except OSError:
            return []

    def _keys(self):
        return [n[:-len(_META)] for n in self._names() if n.endswith(_META)]

    def entries(self):
        """Every readable entry, oldest failure first.

        A corrupt sidecar, or one whose frame is gone, can never be sent and is deleted;
        one that merely failed to read this time is skipped and kept.
        """
        out = []
        for key in self._keys():
            meta, corrupt = self._load_meta(key)
            if meta is None:
                if corrupt:
                    log.warning("outbox: dropping corrupt entry %s", key)
                    self.remove(key)
                continue
            image = self._image_path(key, meta)
            if image is None:
                log.warning("outbox: dropping entry %s, invalid image path", key)
                self.remove(key)
                continue
            if not os.path.exists(image):
                log.warning("outbox: dropping entry %s, its frame is gone", key)
                self.remove(key)
                continue
            try:
                out.append(Entry(
                    key=key, camera=str(meta["camera"]), image=image,
                    caption=str(meta.get("caption") or ""),
                    failed_at=float(meta["failed_at"]),
                    incident=meta.get("incident"),
                    event_start=(float(meta["event_start"])
                                 if meta.get("event_start") is not None else None),
                    etype=meta.get("etype"), score=meta.get("score"),
                    send_path=meta.get("send_path"),
                    announced=bool(meta.get("announced", False)),
                    announce_started_at=(float(meta["announce_started_at"])
                                         if meta.get("announce_started_at") is not None
                                         else None),
                    announce_sent=bool(meta.get("announce_sent", False)),
                    attempts=int(meta.get("attempts", 0) or 0)))
            except (KeyError, TypeError, ValueError):
                log.warning("outbox: dropping malformed entry %s", key)
                self.remove(key)
        out.sort(key=lambda e: (e.failed_at, e.key))
        return out

    def sweep(self, now):
        """Remove stale leftovers: temp files and frames without a sidecar (> 1 h old)."""
        with self._budget_lock:
            self._sweep_locked(now)

    def _sweep_locked(self, now):
        names = set(self._names())
        referenced = set()
        unread = []                   # sidecars that exist but did not read this time
        for key in self._keys():
            meta = self._read_meta(key)
            if meta is None:
                unread.append(key + ".")
                continue
            image = self._image_path(key, meta)
            if image is not None:
                referenced.add(os.path.basename(image))
        for name in names:
            orphan = (name.endswith(_IMAGE) and name not in referenced
                      and not name.startswith(tuple(unread)))
            if not (name.endswith(_TMP) or orphan):
                continue
            path = os.path.join(self.dir, name)
            try:
                if now - os.path.getmtime(path) > STALE_AFTER:
                    if self._borrowed.get(path, 0):
                        continue
                    os.remove(path)
                    log.info("outbox: swept stale %s", name)
            except OSError:
                pass

    def update(self, entry: Entry, **changes):
        """Rewrite an entry's sidecar; return whether the change was durable."""
        with self._budget_lock:
            return self._update_locked(entry, **changes)

    def _update_locked(self, entry: Entry, **changes):
        with self._lock(entry.key):
            meta = self._read_meta(entry.key)
            if meta is None:
                return False
            meta.update(changes)
            try:
                current_image = self._image_path(entry.key, meta)
                if current_image is None:
                    return False
                size = os.path.getsize(current_image) + len(json.dumps(meta, ensure_ascii=False).encode())
                if size > self.max_bytes:
                    log.warning("outbox: byte budget cannot hold update of %s", entry.key)
                    return False
                self._write_meta(entry.key, meta)
                person = _likely_person(replace(entry, etype=meta.get("etype"),
                                                score=meta.get("score")))
                self._reserve(size, entry.key, person)
                return True
            except OSError:
                log.debug("outbox: could not update %s", entry.key, exc_info=True)
                return False

    def _usage(self):
        entries = self.entries()
        sizes = {}
        for entry in entries:
            try:
                sizes[entry.key] = (os.path.getsize(entry.image)
                                    + os.path.getsize(self._path(entry.key, _META)))
            except OSError:
                sizes[entry.key] = 0
        return entries, sizes

    def _reserve(self, new_size, key, incoming_person):
        """Trim after an atomic metadata update, protecting the updated entry."""
        entries, sizes = self._usage()
        used = sum(sizes.values()) - sizes.get(key, 0) + new_size
        count = len(entries) + (key not in sizes)
        victims = ([e for e in entries if e.key != key and not _likely_person(e)]
                   + ([e for e in entries if e.key != key and _likely_person(e)]
                      if incoming_person else []))
        for entry in victims:
            if used <= self.max_bytes and count <= self.max_entries:
                break
            reason = "byte budget" if used > self.max_bytes else "entry limit"
            log.warning("outbox: %s exceeded (%d bytes, %d entries), dropping %s",
                        reason, used, count, entry.key)
            self.remove(entry.key)
            used -= sizes[entry.key]
            count -= 1
        return used <= self.max_bytes and count <= self.max_entries

    def _enforce_cap(self):
        """Bound complete entries by count and bytes; discard oldest non-person first."""
        with self._budget_lock:
            self._enforce_cap_locked()

    def _enforce_cap_locked(self):
        entries, sizes = self._usage()
        used = sum(sizes.values())
        count = len(entries)
        for entry in ([e for e in entries if not _likely_person(e)]
                      + [e for e in entries if _likely_person(e)]):
            if count <= self.max_entries and used <= self.max_bytes:
                break
            reason = "byte budget" if used > self.max_bytes else "entry limit"
            log.warning("outbox: %s exceeded (%d bytes, %d entries), dropping %s",
                        reason, used, count, entry.key)
            self.remove(entry.key)
            used -= sizes[entry.key]
            count -= 1

    def has_entries(self) -> bool:
        return bool(self._keys())

    # ── reachability ─────────────────────────────────────────────────────────
    def note_delivered(self, now):
        """A real alert just went out: Telegram is reachable, no probe needed."""
        self.last_ok_at = now

    def telegram_ready(self, now, probe) -> bool:
        """Whether a drain may try Telegram now; ``probe()`` at most once per interval.

        A send or probe that succeeded within :data:`FRESH_OK` is proof enough. Otherwise
        the probe runs, and a failed one is not repeated for :data:`PROBE_INTERVAL`, so a
        long outage costs one cheap request a minute, not one per tick.
        """
        if self.last_ok_at is not None and now - self.last_ok_at < FRESH_OK:
            return True
        if now < self.next_probe_at:
            return False
        try:
            ok = bool(probe())
        except Exception:  # noqa: BLE001 - a probe must never break the tick
            ok = False
        if ok:
            self.last_ok_at = now
        else:
            self.next_probe_at = now + PROBE_INTERVAL
        return ok

    def note_failed(self, now):
        """A drain send failed: back off like a failed probe."""
        self.last_ok_at = None
        self.next_probe_at = now + PROBE_INTERVAL


def drain(box: Outbox, *, now, known_cameras, send_text, send_photo, archive_review,
          score_for, threshold_for, probe, busy=frozenset(), review_enabled=True,
          max_rescores=MAX_RESCORES_PER_TICK, max_sends=MAX_SENDS_PER_TICK,
          budget_s=TICK_BUDGET_S, clock=time.monotonic):
    """Hold each frame until this drain finishes, without blocking concurrent captures."""
    with box.borrow_entries() as entries:
        return _drain(box, entries, now=now, known_cameras=known_cameras,
                      send_text=send_text, send_photo=send_photo,
                      archive_review=archive_review, score_for=score_for,
                      threshold_for=threshold_for, probe=probe, busy=busy,
                      review_enabled=review_enabled, max_rescores=max_rescores,
                      max_sends=max_sends, budget_s=budget_s, clock=clock)

def _drain(box: Outbox, entries, *, now, known_cameras, send_text, send_photo, archive_review,
           score_for, threshold_for, probe, busy, review_enabled,
           max_rescores, max_sends, budget_s, clock):
    """Deliver what the outbox holds, if Telegram is back. Returns a small report dict.

    Collaborators, all injectable:

    - ``send_text(text) -> bool`` / ``send_photo(entry, caption) -> bool`` — Telegram;
    - ``archive_review(entry, meta)`` — the review log (best-effort);
    - ``score_for(camera)`` — ``score(image_path) -> SubjectScore | None`` or None;
    - ``threshold_for(camera, event_ts)`` — the threshold that applied at the event;
    - ``probe() -> bool`` — a cheap Telegram reachability check;
    - ``busy`` — incident IDs an in-memory retry still owns; they wait for it;
    - ``review_enabled`` — whether the review log is configured (summary wording only).

    One call does a bounded amount of work (``max_rescores``, ``max_sends`` photos,
    ``budget_s`` of ``clock``) and the next tick continues. A camera's entries are all
    rescored before its summary, so one outage gets one summary, and a summary counts
    only entries no earlier summary did.

    Entries past ``max_age`` or of a camera no longer configured are dropped with a
    line. An unscored non-person entry of a camera with a scorer waits for the scorer
    (:data:`UNSCORED_WAIT`) rather than being judged by its event type. An entry is
    removed only once its send (or its summary, for the review-log ones) succeeded. A
    failed summary means the link is down and stops the drain; a failed photo stops only
    its camera, and after :data:`MAX_SEND_ATTEMPTS` the entry goes to the review log. The
    drain never touches cooldowns: a late photo must not silence a fresh live alert.
    """
    report = {"sent": 0, "reviewed": 0, "dropped": 0, "failed": 0, "summaries": 0,
              "stopped": False, "budget": False}
    if not entries:
        return report
    box.sweep(now)
    ready = []
    for entry in entries:
        if now - entry.event_time > box.max_age:
            log.warning("outbox: dropping %s, %s old (max_age %ds)", entry.key,
                        format_delay(now - entry.event_time), box.max_age)
            box.remove(entry.key)
            report["dropped"] += 1
        elif entry.camera not in known_cameras:
            log.warning("outbox: dropping %s, camera %s is no longer configured",
                        entry.key, entry.camera)
            box.remove(entry.key)
            report["dropped"] += 1
        elif now - entry.failed_at >= box.min_age and entry.incident not in busy:
            ready.append(entry)
    if not ready or not box.telegram_ready(now, probe):
        return report
    started = clock()
    rescores = sends = 0
    scorer_down = False

    def spent():
        return clock() - started >= budget_s

    by_camera: dict[str, list[Entry]] = {}
    for entry in ready:
        by_camera.setdefault(entry.camera, []).append(entry)
    for camera, batch in by_camera.items():
        if spent() or sends >= max_sends:
            report["budget"] = True
            break
        scorer = score_for(camera)
        unfinished = False
        for entry in batch:
            if entry.score is not None or scorer is None or scorer_down:
                continue
            if rescores >= max_rescores or spent():
                unfinished = True
                break
            rescores += 1
            result = scorer(entry.image)
            if result is None:
                # Still down: the rest go unscored this drain rather than each paying
                # another timeout.
                log.info("outbox: scorer still unreachable, %s stays unscored", entry.key)
                scorer_down = True
                continue
            entry.score = score_fields(result)
            if not box.update(entry, score=entry.score):
                report["stopped"] = True
                return report
        if unfinished:
            report["budget"] = True          # score the rest first: one summary per batch
            continue
        decided = [e for e in batch
                   if e.score is not None or scorer is None or e.etype == "person"
                   or now - e.failed_at >= UNSCORED_WAIT]
        if not decided:
            continue
        flags = [is_person(e, threshold_for(camera, e.event_time)) for e in decided]
        phone, review = plan_batch(decided, flags, box.max_photos)
        for e in decided:
            if (e.announced and not e.announce_sent and e.announce_started_at is not None
                    and now - e.announce_started_at > ANNOUNCE_CONFIRM_WAIT):
                e.announced = False          # the send was never confirmed
        new = [e for e in decided if not e.announced]
        if box.summary and new:
            new_persons = sum(1 for e, f in zip(decided, flags, strict=True)
                              if f and not e.announced)
            to_phone = sum(1 for e in phone if not e.announced)
            # Persist the intent first: a crash between the send and the write would
            # otherwise announce the same batch again (an unconfirmed flag expires after
            # ANNOUNCE_CONFIRM_WAIT). A failed or raising send takes it back.
            for entry in new:
                if not box.update(entry, announced=True, announce_started_at=now,
                                  announce_sent=False):
                    for prior in new:
                        if prior is entry:
                            break
                        box.update(prior, announced=False, announce_started_at=None)
                    report["stopped"] = True
                    return report
            try:
                sent = send_text(summary_text(camera, new, new_persons, to_phone, now,
                                              review_enabled, lang=box.lang))
            except BaseException:
                for entry in new:
                    box.update(entry, announced=False, announce_started_at=None)
                raise
            if not sent:
                for entry in new:
                    box.update(entry, announced=False, announce_started_at=None)
                box.note_failed(now)
                report["stopped"] = True
                return report
            report["summaries"] += 1
            for entry in new:
                entry.announced = True
                entry.announce_sent = True
                if not box.update(entry, announce_sent=True):
                    log.warning("outbox: summary was sent but confirmation for %s was not saved",
                                entry.key)
        for entry in review:
            archive_review(entry, review_meta(entry, now))
            box.remove(entry.key)
            report["reviewed"] += 1
        for entry in phone:
            if sends >= max_sends or spent():
                report["budget"] = True
                break
            sends += 1
            if send_photo(entry, delayed_caption(entry, now, box.lang)):
                box.remove(entry.key)
                report["sent"] += 1
                continue
            entry.attempts += 1
            box.note_failed(now)
            if entry.attempts >= MAX_SEND_ATTEMPTS:
                log.warning("outbox: %s failed %d late sends; moved to the review log",
                            entry.key, entry.attempts)
                archive_review(entry, review_meta(entry, now, reason="send_failed"))
                box.remove(entry.key)
                report["failed"] += 1
                continue
            log.warning("outbox: late send of %s failed; %s retried later",
                        entry.key, camera)
            if not box.update(entry, attempts=entry.attempts):
                log.warning("outbox: late send attempt for %s was not saved", entry.key)
            report["stopped"] = True
            break
    return report
