"""Background runner for the slow half of an SD/recording follow-up.

Reading a camera card blocks for a median 70 s (up to 150 s), a local recording for
~15 s. Run on the daemon's loop, that is time in which no camera on the host is polled,
no sampler frame grabbed and no pan guard run. The fetch, scoring and frame selection
therefore run here, one thread per camera; everything that decides or has a side effect
(alert gate, Telegram, queues, persistence) stays on the loop, which submits a job and
collects its result on a later tick.

A job is a plain callable closed over copied values; it must never touch the daemon's
state. Its outcome comes back through :meth:`SdWorker.poll`, an exception included, so a
failing job can never end the loop. The bookkeeping (``submit``/``poll``/``pending``/
``busy``) is only ever called from the loop's thread.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Every follow-up gets its own temp dir under this prefix, with the owning daemon's pid
# in the name, so a start can clear what a killed predecessor's jobs left behind.
JOB_DIR_PREFIX = "sdjob_"
# A job dir from before the pid was in the name is only cleared once it is this old.
LEGACY_JOB_DIR_AGE = 3600


@dataclass
class Finished:
    """One job's outcome: ``value`` is what the callable returned, ``error`` what it raised."""
    key: Any
    camera: str
    context: Any
    value: Any = None
    error: BaseException | None = None


class SdWorker:
    """One background thread per camera; jobs of one camera run one after another.

    Two downloads from the same camera card never overlap, as they never did on the loop;
    different cameras run in parallel. Threads are daemonic (a ThreadPoolExecutor's are
    joined at interpreter exit), so a stop never waits for a download in progress: its
    entry is still on the persisted queue and runs again after the restart.
    """

    def __init__(self):
        self._queues: dict[str, queue.Queue] = {}
        self._threads: list[threading.Thread] = []
        self._done: queue.Queue = queue.Queue()
        self._jobs: dict[Any, tuple[str, Any]] = {}      # key -> (camera, context)

    def submit(self, camera: str, key, fn: Callable[[], Any], context=None) -> None:
        """Queue ``fn`` on ``camera``'s thread; ``key`` must not be pending already."""
        if key in self._jobs:
            raise ValueError(f"job {key!r} is already pending")
        self._jobs[key] = (camera, context)
        self._enqueue(camera, key, fn)

    def _enqueue(self, camera, key, fn):
        jobs = self._queues.get(camera)
        if jobs is None:
            jobs = self._queues[camera] = queue.Queue()
            thread = threading.Thread(target=self._run, args=(jobs,),
                                      name=f"sd-followup-{camera}", daemon=True)
            self._threads.append(thread)
            thread.start()
        jobs.put((key, fn))

    def _run(self, jobs):
        while True:
            item = jobs.get()
            if item is None:
                return
            key, fn = item
            self._done.put(run_job(key, fn))

    def poll(self) -> list[Finished]:
        """Jobs finished since the last call, each with its context. Never blocks."""
        out: list[Finished] = []
        while True:
            try:
                key, value, error = self._done.get_nowait()
            except queue.Empty:
                return out
            camera, context = self._jobs.pop(key)
            out.append(Finished(key, camera, context, value, error))

    def pending(self, key) -> bool:
        """True from ``submit`` until ``poll`` has handed the job's result back."""
        return key in self._jobs

    def busy(self, camera: str) -> bool:
        """True while a job of ``camera`` is queued, running or not yet collected."""
        return any(cam == camera for cam, _ in self._jobs.values())

    def in_flight(self) -> int:
        """Jobs submitted and not yet collected."""
        return len(self._jobs)

    def shutdown(self, wait: bool = False) -> None:
        """Let the threads end after their current job; ``wait`` joins them."""
        for jobs in self._queues.values():
            jobs.put(None)
        if wait:
            for thread in self._threads:
                thread.join()


class InlineSdWorker(SdWorker):
    """Runs each job at submit, on the caller's thread: the result is ready for the next
    ``poll``. What the tests and one-shot callers use; semantics as before the worker."""

    def _enqueue(self, camera, key, fn):
        self._done.put(run_job(key, fn))

    def busy(self, camera: str) -> bool:
        return False          # a job is finished the moment it was submitted


def run_job(key, fn):
    """``(key, value, error)`` for one job; an exception is returned, never raised."""
    try:
        return key, fn(), None
    except Exception as exc:  # noqa: BLE001 - a failed follow-up must not end the loop
        log.exception("SD follow-up job failed: %s", exc)
        return key, None, exc


def job_dir(tmp=None) -> str:
    """A fresh temp dir for one job, named after this process."""
    return tempfile.mkdtemp(prefix=f"{JOB_DIR_PREFIX}{os.getpid()}_", dir=tmp)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True           # exists, owned by someone else
    return True


def clean_orphan_job_dirs(tmp=None, now=None, logger=None) -> int:
    """Remove job dirs whose daemon is gone. Returns how many were removed. Never raises.

    A job still running when the daemon stops (a stop does not wait for a download) or a
    daemon killed outright leaves its segment and frames behind; on a small tmpfs those
    add up. Dirs of a live process, this one included, are left alone.
    """
    tmp = tmp or tempfile.gettempdir()
    now = time.time() if now is None else now
    removed = 0
    try:
        names = os.listdir(tmp)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(JOB_DIR_PREFIX):
            continue
        path = os.path.join(tmp, name)
        owner, sep, _ = name[len(JOB_DIR_PREFIX):].partition("_")
        try:
            if sep and owner.isdigit():
                if _pid_alive(int(owner)):
                    continue
            elif now - os.path.getmtime(path) < LEGACY_JOB_DIR_AGE:
                continue
            if not os.path.isdir(path) or os.path.islink(path):
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    if removed and logger is not None:
        logger.info("removed %d SD job dir(s) left by a stopped daemon", removed)
    return removed
