"""Opt-in virtual PTZ endstop, independent of slow alert processing.

This is a measured safety margin, not a firmware or physical travel limit. Callbacks
must have bounded network timeouts. ``disable_tracking`` must return True only after
reading back OFF, and must serialize access to the daemon's existing camera session.
Every other motor owner must honor ``tracking_allowed`` at the actual write.
"""

import logging
import math
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuardSnapshot:
    position: float | None
    healthy: bool
    latched: bool
    tracking_allowed: bool
    disable_pending: bool
    reason: str


class StopGuard:
    """Observe in one thread; native tracking OFF runs in at most one other thread.

    ``poll_once`` is the same observation step used by the worker, exposed for trace
    replay. Do not call it concurrently with the worker. A trip stays latched until
    explicit ``rearm`` after a verified disable, cooldown and stable safe samples.
    Inward recovery is allowed; outward or stationary unsafe positions repeat Stop.
    ``suspended`` suppresses reads and motor commands (including shutdown Stop),
    and requires fresh startup verification on resume. Callback errors suspend too.
    ``close`` requests OFF and joins within a total timeout, but cannot cancel network
    calls. Callers must confirm OFF before surrendering control or replacing a client.
    """

    def __init__(self, read_position, stop, disable_tracking, *, right_limit, margin,
                 left_limit=None, poll_interval=0.15, stale_after=1.0,
                 stop_interval=0.15, cooldown=2.0, stable_samples=2,
                 retry_interval=1.0, stable_tolerance=0.002, suspended=None,
                 clock=time.monotonic, wait=None):
        values = (right_limit, margin, poll_interval, stale_after, stop_interval,
                  cooldown, retry_interval, stable_tolerance)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("guard values must be finite")
        if min(margin, poll_interval, stale_after, stop_interval, retry_interval, stable_tolerance) <= 0 or cooldown < 0:
            raise ValueError("guard timing and margin must be positive")
        if stable_samples < 2:
            raise ValueError("at least two safe samples are required")
        self.right_inner = right_limit - margin
        self.left_inner = None if left_limit is None else left_limit + margin
        if self.left_inner is not None and (
                not math.isfinite(self.left_inner) or self.left_inner >= self.right_inner):
            raise ValueError("guard margins leave no safe range")
        self.read_position, self.stop, self.disable_tracking = read_position, stop, disable_tracking
        self.poll_interval, self.stale_after = poll_interval, stale_after
        self.stop_interval, self.cooldown = stop_interval, cooldown
        self.stable_samples, self.retry_interval = stable_samples, retry_interval
        self.stable_tolerance = stable_tolerance
        self.clock = clock
        self.suspended = suspended or (lambda: False)
        self._lock = threading.RLock()
        self._exit = threading.Event()
        self._wait = wait or self._exit.wait
        self._thread = self._disable_thread = None
        self._closed = False
        self._disable_warned = self._stop_warned = False
        self._position = self._observed_at = None
        self._safe_samples = 0
        self._healthy = self._latched = self._disable_ok = self._armed = False
        self._reason = "starting"
        self._trip_at = self._last_stop = self._last_disable = -math.inf
        self._recovery_until = -math.inf
        self._recovery_direction = 0

    def _safe(self, position):
        return position < self.right_inner and (self.left_inner is None or position > self.left_inner)

    def _is_suspended(self):
        try:
            return bool(self.suspended())
        except Exception:  # noqa: BLE001 - unknown privacy state cannot authorize movement
            return True

    def _note_suspended(self):
        with self._lock:
            self._healthy = self._armed = False
            self._safe_samples = 0
            self._observed_at = None
            self._reason = "suspended"
            self._recovery_until = -math.inf

    def snapshot(self):
        if self._is_suspended():
            self._note_suspended()
        with self._lock:
            healthy = (self._healthy and not self._closed and self._observed_at is not None
                       and self.clock() - self._observed_at <= self.stale_after)
            allowed = healthy and not self._latched and self._armed
            pending = self._disable_thread is not None and self._disable_thread.is_alive()
            return GuardSnapshot(self._position, healthy, self._latched, allowed, pending, self._reason)

    def _request_disable(self):
        with self._lock:
            pending = self._disable_thread is not None and self._disable_thread.is_alive()
            if self._disable_ok or pending or self.clock() - self._last_disable < self.retry_interval:
                return
            self._last_disable = self.clock()
            self._disable_thread = threading.Thread(target=self._disable, daemon=True,
                                                    name="ptz-stopguard-off")
            self._disable_thread.start()

    def _disable(self):
        try:
            ok = self.disable_tracking() is True
        except Exception:  # noqa: BLE001 - camera errors must not kill observation
            ok = False
        with self._lock:
            warn = not ok and not self._disable_warned
            self._disable_ok = ok
            self._disable_warned = not ok
        if warn:
            log.warning("PTZ stop guard could not verify tracking OFF")

    def _trip(self, reason, *, dispatch=True):
        with self._lock:
            if not self._latched:
                self._trip_at = self.clock()
                self._disable_ok = False
                self._safe_samples = 0
                self._recovery_until = -math.inf
            self._latched = True
            self._armed = False
            self._reason = reason
        if dispatch:
            self._request_disable()

    def _stop_if_due(self, *, force=False):
        if self._is_suspended():
            self._note_suspended()
            return
        with self._lock:
            if not force and self.clock() < self._recovery_until:
                return
            if not force and self.clock() - self._last_stop < self.stop_interval:
                return
            self._last_stop = self.clock()
        try:
            self.stop()
            self._stop_warned = False
        except Exception:  # noqa: BLE001 - retry on later observations; never expose credentials
            if not self._stop_warned:
                log.warning("PTZ stop guard Stop failed")
            self._stop_warned = True

    def poll_once(self):
        """Observe, latch and stop before waiting for the slower tracking OFF call."""
        with self._lock:
            if self._closed:
                return
        if self._is_suspended():
            self._note_suspended()
            with self._lock:
                latched = self._latched
            if latched:
                self._request_disable()
            return
        started = self.clock()
        try:
            position = float(self.read_position())
            if not math.isfinite(position) or self.clock() - started > self.stale_after:
                raise ValueError("unusable pose")
        except Exception:  # noqa: BLE001 - fail closed on unavailable or stale position
            with self._lock:
                self._healthy = False
                self._safe_samples = 0
                self._recovery_until = -math.inf
            self._trip("pose_unavailable")
            self._stop_if_due()
            return
        # Privacy may have changed while a bounded network read was in flight.
        if self._is_suspended():
            self._note_suspended()
            return
        now = self.clock()
        with self._lock:
            if self._reason == "suspended" and not self._latched:
                self._reason = "starting"
            previous = self._position if self._observed_at is not None and now - self._observed_at <= self.stale_after else None
            self._position, self._observed_at, self._healthy = position, now, True
            safe = self._safe(position)
            self._safe_samples = (self._safe_samples + 1 if safe and previous is not None
                                  and abs(position - previous) <= self.stable_tolerance else int(safe))
            latched = self._latched
            recovering = now < self._recovery_until
            recovery_outward = recovering and previous is not None and (
                (position - previous) * self._recovery_direction < -1e-6)
            if recovery_outward or previous is None:
                self._recovery_until = -math.inf
            if previous is None:
                self._armed = False
            if safe and not latched and self._safe_samples >= self.stable_samples:
                self._armed = True
                self._reason = "armed"
        if recovery_outward:
            self._trip("recovery_outward", dispatch=False)
            self._stop_if_due(force=True)
        if not safe:
            # Latch before Stop so concurrent control cannot re-enable tracking.
            self._trip("boundary", dispatch=False)
            # Stop before dispatching the slower native API work.
            inward = previous is not None and (
                position < previous - 1e-6 if position >= self.right_inner else position > previous + 1e-6)
            if not latched or not inward:
                self._stop_if_due()
            self._request_disable()
        elif latched:
            self._request_disable()

    def prepare_recovery(self, target, timeout=2.0):
        """Permit up to two seconds for a verified-OFF safe-home move to accelerate.

        Call immediately before the actual preset command. The latch stays set; a
        fresh observation away from the target cancels this permit and forces Stop.
        Unknown pose, privacy, failed OFF or an expired permit never relax the guard.
        """
        if not math.isfinite(target) or not math.isfinite(timeout) or not 0 < timeout <= 2.0:
            return False
        with self._lock:
            state = self.snapshot()
            if (not state.latched or not state.healthy or state.disable_pending
                    or not self._disable_ok or not self._safe(target)):
                return False
            self._recovery_direction = 1 if target > self._position else -1
            self._recovery_until = self.clock() + timeout
            return True

    def rearm(self):
        """Explicitly release the latch only after fresh stable interior observations."""
        with self._lock:
            state = self.snapshot()
            if (not state.healthy or self._safe_samples < self.stable_samples
                    or not self._safe(self._position) or state.disable_pending
                    or (self._latched and (not self._disable_ok or self.clock() - self._trip_at < self.cooldown))):
                return False
            self._latched, self._reason, self._armed = False, "armed", True
            self._disable_ok = False
            self._recovery_until = -math.inf
            return True

    def _run(self):
        try:
            while not self._exit.is_set():
                self.poll_once()
                if self._wait(self.poll_interval):
                    break
        finally:
            self._stop_if_due()

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("guard is closed")
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="ptz-stopguard")
                self._thread.start()
        return self

    def close(self, timeout=2.0):
        """Latch, request OFF and bound joins; return whether OFF was verified.

        False means an unconfirmed stop, including an API call still in flight.
        No synchronous fallback may extend the caller's shutdown timeout.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._closed = True
            self._recovery_until = -math.inf
            self._exit.set()
        self._trip("closed")
        for worker in (self._thread, self._disable_thread):
            if worker is not None and worker is not threading.current_thread():
                worker.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            pending = self._disable_thread is not None and self._disable_thread.is_alive()
            return self._disable_ok and not pending
