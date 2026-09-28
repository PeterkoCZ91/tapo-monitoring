"""Opt-in persistent outbox for alerts Telegram never took ("zpožděno" delivery).

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
import time
from dataclasses import dataclass

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


def delayed_caption(entry: Entry, now) -> str:
    """The original caption behind a ``⏳ zpožděno o …`` prefix. Pure."""
    return f"⏳ zpožděno o {format_delay(now - entry.event_time)} · {entry.caption}"


def _clock(ts, with_date):
    return time.strftime("%d.%m. %H:%M" if with_date else "%H:%M", time.localtime(ts))


def summary_text(camera, entries, persons, to_phone, now, review_enabled=True) -> str:
    """The one message that opens a camera's late batch (HTML-escaped, Czech). Pure.

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
    lines = [f"⏳ Výpadek spojení {_clock(first, with_date)}–{_clock(last, with_date)}"
             f" · {camera} — nedoručeno {n} {plural(n, 'událost', 'události', 'událostí')},"
             f" z toho {persons} s osobou."]
    if to_phone:
        lines.append(f"Posílám zpožděně {to_phone} "
                     f"{plural(to_phone, 'fotku', 'fotky', 'fotek')} s osobou:")
    rest = n - to_phone
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

    def __init__(self, directory, *, max_age=24 * 3600, max_entries=300, max_photos=10,
                 summary=True, min_age=MIN_AGE):
        self.dir = directory
        self.max_age = max_age
        self.max_entries = max_entries
        self.max_photos = max_photos
        self.summary = summary
        self.min_age = min_age
        self.last_ok_at: float | None = None
        self.next_probe_at = 0.0

    # ── storage ──────────────────────────────────────────────────────────────
    def _path(self, key, suffix):
        return os.path.join(self.dir, key + suffix)

    def _write_atomic(self, path, data: bytes):
        # fsync before the rename: after a power cut the name must not point at an empty
        # file, which the next start would have to throw away as corrupt.
        tmp = path + _TMP
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

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
        try:
            os.makedirs(self.dir, exist_ok=True)
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
                    "attempts": int(previous.get("attempts", 0) or 0)}
            self._write_atomic(self._path(key, _IMAGE), frame)
            self._write_meta(key, meta)
        except OSError:
            log.warning("outbox: could not store the alert for %s", camera, exc_info=True)
            return None
        if not previous:
            log.info("outbox: kept undelivered %s alert for %s (%s)",
                     etype or "?", camera, incident or key)
        self._enforce_cap()
        return key

    def remove(self, key):
        for suffix in (_META, _IMAGE):
            try:
                os.remove(self._path(key, suffix))
            except FileNotFoundError:
                pass
            except OSError:
                log.debug("outbox: could not remove %s%s", key, suffix, exc_info=True)

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
            image = self._path(key, _IMAGE)
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
                    attempts=int(meta.get("attempts", 0) or 0)))
            except (KeyError, TypeError, ValueError):
                log.warning("outbox: dropping malformed entry %s", key)
                self.remove(key)
        out.sort(key=lambda e: (e.failed_at, e.key))
        return out

    def sweep(self, now):
        """Remove stale leftovers: temp files and frames without a sidecar (> 1 h old)."""
        names = set(self._names())
        for name in names:
            orphan = (name.endswith(_IMAGE)
                      and name[:-len(_IMAGE)] + _META not in names)
            if not (name.endswith(_TMP) or orphan):
                continue
            path = os.path.join(self.dir, name)
            try:
                if now - os.path.getmtime(path) > STALE_AFTER:
                    os.remove(path)
                    log.info("outbox: swept stale %s", name)
            except OSError:
                pass

    def update(self, entry: Entry, **changes):
        """Rewrite an entry's sidecar with ``changes`` (score, announced, attempts)."""
        meta = self._read_meta(entry.key)
        if meta is None:
            return
        meta.update(changes)
        try:
            self._write_meta(entry.key, meta)
        except OSError:
            log.debug("outbox: could not update %s", entry.key, exc_info=True)

    def _enforce_cap(self):
        """Drop beyond ``max_entries``: the oldest non-person entries first, then persons."""
        entries = self.entries()
        excess = len(entries) - self.max_entries
        if excess <= 0:
            return
        victims = ([e for e in entries if not _likely_person(e)]
                   + [e for e in entries if _likely_person(e)])[:excess]
        for entry in victims:
            log.warning("outbox: full (%d), dropping %s", self.max_entries, entry.key)
            self.remove(entry.key)

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
    entries = box.entries()
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
            box.update(entry, score=entry.score)
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
        new = [e for e in decided if not e.announced]
        if box.summary and new:
            new_persons = sum(1 for e, f in zip(decided, flags, strict=True)
                              if f and not e.announced)
            to_phone = sum(1 for e in phone if not e.announced)
            if not send_text(summary_text(camera, new, new_persons, to_phone, now,
                                          review_enabled)):
                box.note_failed(now)
                report["stopped"] = True
                return report
            report["summaries"] += 1
            for entry in new:
                entry.announced = True
                box.update(entry, announced=True)
        for entry in review:
            archive_review(entry, review_meta(entry, now))
            box.remove(entry.key)
            report["reviewed"] += 1
        for entry in phone:
            if sends >= max_sends or spent():
                report["budget"] = True
                break
            sends += 1
            if send_photo(entry, delayed_caption(entry, now)):
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
            box.update(entry, attempts=entry.attempts)
            report["stopped"] = True
            break
    return report
