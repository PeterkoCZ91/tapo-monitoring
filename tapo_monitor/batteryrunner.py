"""Background threads that collect a battery camera's events after it woke up.

For every ``batterywake`` camera one :class:`wakewatch.WakeWatcher` pings the camera (the
only free way to notice it woke) and one worker thread runs :func:`batterycam.collect`
when the watcher signals. Finished jobs wait in a list the daemon drains each tick; the
per-camera cursor moves together with the jobs it produced, so the daemon can persist the
cursor and hand the jobs on without either getting ahead of the other.
"""

from __future__ import annotations

import logging
import queue
import shutil
import threading
import time

from . import batterycam, wakewatch
from .config import resolve_camera_credentials

log = logging.getLogger(__name__)

QUEUE_POLL = 0.5


def default_client_factory(cfg):  # pragma: no cover - network I/O
    """A zero-argument factory of one logged-in client for ``cfg`` (credentials from env)."""
    from . import sdclip

    user, password, cloud = resolve_camera_credentials(cfg)
    return lambda: sdclip.build_client(cfg.host, user, password, cloud)


def remove_job_dir(job) -> None:
    """Delete a job's temp dir (clip and any frames). Never raises."""
    if job is not None and job.dir:
        shutil.rmtree(job.dir, ignore_errors=True)


class BatteryRunner:
    """One watcher and one worker per ``batterywake`` camera."""

    def __init__(self, app, *, cursors=None, client_factory=default_client_factory,
                 collect=batterycam.collect, ping=wakewatch.ping_once,
                 now=time.time, sleep=time.sleep):
        self._cams = {c.name: c for c in app.cameras
                      if "batterywake" in c.detection.sources}
        self._cursors: dict = {n: v for n, v in (cursors or {}).items() if n in self._cams}
        self._client_factory = client_factory
        self._collect = collect
        self._ping = ping
        self._now = now
        # No saved cursor: events older than this start are history, but the first wake
        # (possibly the very press that woke the camera) must not become the baseline.
        for n in self._cams:
            self._cursors.setdefault(n, now())
        self._sleep = sleep
        self._lock = threading.Lock()
        self._jobs: list = []
        self._queues = {n: queue.Queue() for n in self._cams}
        self._pending: set = set()      # queued or running, per camera
        self._stop = threading.Event()
        self._threads: list = []

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self):
        for name, cfg in self._cams.items():
            watcher = wakewatch.WakeWatcher(
                name, cfg.host, cfg.battery_wake.ping_interval, self.on_wake,
                ping=self._ping, sleep=self._sleep, stop_event=self._stop, busy=self.busy)
            worker = threading.Thread(target=self._work, args=(name,), daemon=True,
                                      name=f"batterycam-{name}")
            self._threads += [watcher, worker]
            watcher.start()
            worker.start()

    def stop(self, timeout: float = 2.0):
        """Stop the threads and delete the dirs of jobs nobody drained. Never raises."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout)
        with self._lock:
            jobs, self._jobs = self._jobs, []
        for job in jobs:
            remove_job_dir(job)

    # ── wake handling ────────────────────────────────────────────────────
    def busy(self, name) -> bool:
        with self._lock:
            return name in self._pending

    def on_wake(self, name):
        """Queue one collect for ``name`` unless one is already queued or running."""
        with self._lock:
            if name in self._pending or name not in self._queues:
                return
            self._pending.add(name)
        self._queues[name].put(name)

    def _work(self, name):
        q = self._queues[name]
        while not self._stop.is_set():
            try:
                q.get(timeout=QUEUE_POLL)
            except queue.Empty:
                continue
            try:
                self.run_collect(name)
            except Exception:  # noqa: BLE001 - a bad wake must not end the worker
                log.exception("battery camera %s: collect crashed", name)
            finally:
                with self._lock:
                    self._pending.discard(name)

    def run_collect(self, name):
        """One collect for ``name`` now (the worker's body; public for tests)."""
        cfg = self._cams[name]
        with self._lock:
            cursor = self._cursors.get(name)
        t0 = time.monotonic()
        log.info("batterywake %s: wake, collecting since %s", name, cursor)
        jobs, new_cursor = self._collect(cfg, self._client_factory(cfg), cursor,
                                         now=self._now, sleep=self._sleep)
        log.info("batterywake %s: collect done in %.1fs, %d job(s), cursor %s -> %s",
                 name, time.monotonic() - t0, len(jobs), cursor, new_cursor)
        with self._lock:
            if self._stop.is_set():
                # Stopping: the cursor stays so the next start collects these again.
                stale = jobs
            else:
                stale = []
                self._cursors[name] = new_cursor
                self._jobs.extend(jobs)
        for job in stale:
            remove_job_dir(job)

    # ── daemon side ──────────────────────────────────────────────────────
    def drain(self):
        """``(jobs, cursors)``: every finished job and the cursors as of the same moment."""
        with self._lock:
            jobs, self._jobs = self._jobs, []
            return jobs, dict(self._cursors)

