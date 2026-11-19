"""Collect a battery camera's events and SD clips during the short time it is awake.

A ``batterywake`` camera sleeps between events and every login wakes it, so this module
is only ever called once its own wake-up has been noticed (see ``wakewatch``). One
:func:`collect` call logs in once, waits for the event that woke it to finish, reads its
recording from the card and cuts frames from it. The parsing helpers are pure; the
network and ffmpeg steps are injected so the whole flow is testable without either.

pytapo shapes this relies on (3.4.28):

* ``getEvents(startTime, endTime)`` takes *camera* seconds (true time minus the clock
  correction) and returns events whose ``start_time``/``end_time`` are already shifted
  to true epoch.
* ``getRecordings("YYYYMMDD")`` returns ``[{"search_video_results_N": {"startTime",
  "endTime", "vedio_type"}}]`` in *camera* seconds, which is also what ``Downloader``
  expects.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from . import detection, sdworker

log = logging.getLogger(__name__)

RING_BIT = 512  # events_1 bit 9 = code 10, the doorbell press
_RING_STATES = {17: "missed", 18: "answered"}
POLL_SECONDS = 2.0
LOOKBACK = 600
# pytapo's Downloader refuses a window ending less than 60 s ago. An event that has
# ended has a finished recording, and waiting out the guard would keep the camera
# awake past ``event_wait``, so the guard is lowered to this on our downloader.
DOWNLOAD_FRESH_GUARD = 3


def is_ring(event) -> bool:
    """True when the event carries the doorbell press bit. Pure."""
    try:
        return bool(int(event.get("events_1") or 0) & RING_BIT)
    except (AttributeError, TypeError, ValueError):
        return False


def ring_state(event) -> str | None:
    """``missed`` / ``answered`` for a ring event (by ``alarm_type``), else None. Pure."""
    if not is_ring(event):
        return None
    try:
        return _RING_STATES.get(int(event.get("alarm_type")))
    except (TypeError, ValueError):
        return None


def is_new(start: float, cursor: float, seen: Iterable[float], tol: float = 3.0) -> bool:
    """Whether an event starting at ``start`` has not been handled yet. Pure.

    The camera shifts an event's start by a second or so between listings, so an
    event within ``tol`` seconds of the cursor or of an already seen start is the
    same event.
    """
    if start - cursor <= tol:
        return False
    return all(abs(start - s) > tol for s in seen)


def parse_recordings(raw) -> list[dict]:
    """Flatten pytapo ``getRecordings`` into ``[{"start_time", "end_time"}]``. Pure."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        seg = item if "startTime" in item else next(
            (v for v in item.values() if isinstance(v, dict) and "startTime" in v), None)
        if not seg:
            continue
        try:
            out.append({"start_time": int(seg["startTime"]), "end_time": int(seg["endTime"])})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda r: r["start_time"])


def match_recording(start: float, recordings: list[dict], tol: float = 5.0) -> dict | None:
    """The recording starting closest to ``start`` within ``tol`` seconds, else None."""
    best = None
    for rec in recordings:
        gap = abs(rec["start_time"] - start)
        if gap <= tol and (best is None or gap < abs(best["start_time"] - start)):
            best = rec
    return best


@dataclass
class Job:
    """One finished event ready to be scored and sent."""
    camera: str
    event: dict
    frames: list[str]
    clip: str | None
    dir: str | None = field(default=None)  # temp dir holding frames and clip


def download_recording(client, start, end, out_path):  # pragma: no cover - I/O
    """Download the SD window [start, end] (camera seconds) to ``out_path``, or None.

    Mirrors the pattern of ``sdclip._download_segment`` (session pre-warm, fresh event
    loop) but with pytapo's fast "download" method that the doorbell serves.
    """
    import sys

    from pytapo.media_stream.downloader import Downloader

    from . import sdclip

    out_dir, name = os.path.split(out_path)
    try:
        tc = client.getTimeCorrection() or 0
        downloader = Downloader(client, start, end, tc, out_dir + "/", overwriteFiles=True,
                                window_size=50, fileName=name, progressInterval=0)
        downloader.FRESH_RECORDING_TIME_SECONDS = DOWNLOAD_FRESH_GUARD
        client.getUserID()  # warm before the loop starts, see sdclip._download_segment

        async def run():
            async for _status in downloader.download():
                pass

        sdclip._run_in_fresh_loop(lambda: run(), cleanup=lambda: sdclip._safe_unlink(out_path))
    except Exception as exc:  # noqa: BLE001 - the caller treats a miss as "no clip"
        print(f"battery clip download failed: {type(exc).__name__}", file=sys.stderr)
        sdclip._safe_unlink(out_path)
        return None
    ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
    return out_path if ok else None


def download_snapshot(client, start, out_path):  # pragma: no cover - I/O
    """The camera's own first-frame JPEG of the event at ``start`` (camera seconds), or None.

    One tiny media-port request (about 0.1 s, 640x360); ``start`` must equal the event's
    start as the camera lists it. Never raises.
    """
    import sys

    from pytapo.media_stream.snapshot import getRecordingSnapshot

    from . import sdclip

    try:
        client.getUserID()  # warm before the loop starts, see sdclip._download_segment
        data = sdclip._run_in_fresh_loop(
            lambda: getRecordingSnapshot(client, int(start), timeout=10), timeout=20)
        if not data:
            return None
        with open(out_path, "wb") as fh:
            fh.write(data)
        return out_path
    except Exception as exc:  # noqa: BLE001 - the caller treats a miss as "no frame"
        print(f"battery snapshot failed: {type(exc).__name__}", file=sys.stderr)
        sdclip._safe_unlink(out_path)
        return None


def extract_frames(clip, out_dir, duration, rotate=0):  # pragma: no cover - ffmpeg
    """Up to six event-spanning JPEGs from ``clip`` (see snapshot.frames_from_clip)."""
    from . import snapshot

    return snapshot.frames_from_clip(clip, duration=duration, out_dir=out_dir, rotate=rotate)


def spread(frames: list[str], n: int) -> list[str]:
    """``n`` evenly spaced items of ``frames`` (all when fewer). Pure."""
    if n <= 0 or len(frames) <= n:
        return list(frames)
    if n == 1:
        return [frames[len(frames) // 2]]
    idx = {round(i * (len(frames) - 1) / (n - 1)) for i in range(n)}
    return [frames[i] for i in sorted(idx)]


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def collect(cfg, client_factory, cursor, *, now=time.time, sleep=time.sleep,
            download=download_recording, extract=None, job_dir=sdworker.job_dir,
            poll=POLL_SECONDS, snapshot=download_snapshot):
    """One wake: events and clips since ``cursor`` -> ``(jobs, new_cursor)``.

    ``cursor`` is the start of the last event handled; None (first run) returns
    ``([], now())`` without logging in, so events already on the card never storm.
    ``client_factory()`` makes one logged-in client; when it raises, nothing more is
    tried (the camera fell asleep between the ping and the login) and the cursor stays.
    Events still running when ``battery_wake.event_wait`` runs out, and every later
    one, are left for the next wake: the cursor stops before them. No exception escapes.
    ``extract(clip, dir, duration) -> [jpg]`` defaults to ffmpeg via snapshot.
    ``snapshot(client, start, out_path) -> path | None`` is the fallback single frame
    when the clip yields none.
    """
    if cursor is None:
        return [], now()
    bw = cfg.battery_wake
    if extract is None:
        def extract(clip, out_dir, duration):
            return extract_frames(clip, out_dir, duration, rotate=getattr(cfg, "rotate", 0))
    try:
        client = client_factory()
    except Exception as exc:  # noqa: BLE001 - asleep again; do not retry, that wakes it
        log.info("battery camera %s: login failed after wake (%s), giving up",
                 cfg.name, type(exc).__name__)
        return [], cursor
    jobs: list[Job] = []
    try:
        tc = int(client.getTimeCorrection() or 0)
        log.info("battery camera %s: logged in, camera clock offset %ds", cfg.name, tc)
        deadline = now() + bw.event_wait
        events: list[dict] = []
        while True:
            t = now()
            listed = client.getEvents(int(t - LOOKBACK - tc), int(t + 60 - tc)) or []
            events = sorted(
                (detection.normalize_event(e) for e in listed
                 if isinstance(e, dict) and _num(e.get("start_time")) is not None),
                key=lambda e: float(e["start_time"]))
            events = [e for e in events if is_new(float(e["start_time"]), cursor, [])]
            if events and all(_ended(e, now()) for e in events):
                break
            if now() >= deadline:
                break
            sleep(poll)
        recordings: dict[str, list[dict]] = {}
        for event in events:
            if not _ended(event, now()):
                break  # still running: it and anything after wait for the next wake
            start = float(event["start_time"])
            date = datetime.fromtimestamp(start).strftime("%Y%m%d")
            if date not in recordings:
                try:
                    recordings[date] = parse_recordings(client.getRecordings(date))
                except Exception as exc:  # noqa: BLE001 - alert without a clip
                    log.warning("battery camera %s: recording list failed (%s)",
                                cfg.name, type(exc).__name__)
                    recordings[date] = []
            rec = match_recording(start - tc, recordings[date])
            log.info("battery camera %s: event start=%d end=%s alarm_type=%s events_1=%s "
                     "ring=%s recording=%s", cfg.name, int(start), event.get("end_time"),
                     event.get("alarm_type"), event.get("events_1"), is_ring(event),
                     "matched" if rec is not None else "none")
            jobs.append(_job(cfg, client, event, rec, download, extract, job_dir,
                              snapshot, tc))
            cursor = max(cursor, start)
    except Exception as exc:  # noqa: BLE001 - keep what is done, never raise
        log.warning("battery camera %s: collect stopped (%s: %s)",
                    cfg.name, type(exc).__name__, exc)
    return jobs, cursor


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ended(event, t) -> bool:
    end = _num(event.get("end_time"))
    return end is not None and end <= t


def _job(cfg, client, event, rec, download, extract, job_dir, snapshot=None, tc=0) -> Job:
    directory = job_dir()
    clip = None
    frames: list[str] = []
    if rec is not None:
        try:
            path = os.path.join(directory, f"battery_{rec['start_time']}.mp4")
            clip = download(client, rec["start_time"], rec["end_time"], path)
            if clip:
                frames = spread(list(extract(clip, directory,
                                             rec["end_time"] - rec["start_time"]) or []),
                                cfg.battery_wake.frames)
        except Exception as exc:  # noqa: BLE001 - the event is still worth an alert
            log.warning("battery camera %s: clip step failed (%s)", cfg.name, type(exc).__name__)
    if not frames and snapshot is not None:
        start = rec["start_time"] if rec is not None else int(float(event["start_time"]) - tc)
        try:
            shot = snapshot(client, start, os.path.join(directory, f"battery_{start}_snap.jpg"))
            if shot:
                frames = [shot]
        except Exception as exc:  # noqa: BLE001 - no frame is today's behaviour
            log.warning("battery camera %s: snapshot failed (%s)", cfg.name, type(exc).__name__)
    log.info("battery camera %s: clip=%s (%s bytes) frames=%d source=%s", cfg.name,
             "yes" if clip else "no", os.path.getsize(clip) if clip and os.path.exists(clip) else 0,
             len(frames), "clip" if clip and frames else ("snapshot" if frames else "none"))
    kept = set(frames)
    for name in os.listdir(directory):
        full = os.path.join(directory, name)
        if full != clip and full not in kept and full.endswith(".jpg"):
            _unlink(full)
    if rec is None and not frames:
        try:
            os.rmdir(directory)
        except OSError:
            pass
        return Job(cfg.name, event, [], None)
    return Job(cfg.name, event, frames, clip, directory)
