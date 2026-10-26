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
from collections import deque
from dataclasses import dataclass, field

from . import ptzrpc

log = logging.getLogger(__name__)

_TRACE_LIMIT = 80        # events kept per episode
_TRACE_MOVE = 0.004      # pan change that earns a position entry in the trace
_TRACE_HEARTBEAT = 5.0   # seconds between position entries while the pose is steady
_SLOW_READ = 0.4         # a pose read slower than this is counted in the stats
_STATS_EVERY = 600.0     # seconds between read-statistics lines
_BACKOFF_AFTER = 3       # consecutive failed reads/Stops before the retry rate slows
_BACKOFF_CAP = 2.0       # slowest retry gap: Stop never goes silent for longer than this
_READ_BACKOFF_FLOOR = 0.5  # first slowed gap after repeated non-transport (auth/fault) read errors
_READ_BACKOFF_CAP = 1.0
_OFF_BACKOFF_CAP = 10.0  # slowest tracking-OFF retry gap
_OFF_FAILS_TO_UNVERIFY = 2
_REVERIFY_EVERY = 30.0   # seconds between OFF re-checks while the latch is set


@dataclass(frozen=True)
class GuardSnapshot:
    position: float | None
    healthy: bool
    latched: bool
    tracking_allowed: bool
    disable_pending: bool
    reason: str
    thread_alive: bool = True
    loop_errors: int = 0


@dataclass
class _Episode:
    started: float
    trip_x: float | None
    reasons: list[str] = field(default_factory=list)
    low: float | None = None
    high: float | None = None
    low_at: float = 0.0
    high_at: float = 0.0
    beyond_limit: bool = False
    reads_failed: int = 0
    stops: int = 0
    stops_failed: int = 0
    first_stop_ok: float | None = None
    off: float | None = None
    off_failed: int = 0
    recoveries: int = 0
    # Debug context, never used for decisions: the pre-trip samples and a bounded
    # timeline of observed position changes and guard/camera events.
    pre: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    trace_dropped: int = 0
    stops_traced: int = 0
    last_traced_x: float | None = None
    last_traced_at: float = -math.inf
    cause: str = "none"


def _diag_text(diag):
    """Compact text for a failed RPC's attempt log (``ptzrpc.call`` diagnostics)."""
    if not isinstance(diag, dict):
        return ""
    errors = ",".join(str(e) for e in diag.get("errors", ())) or "none"
    return f" (attempts={diag.get('attempts', 0)} errors={errors} rpc={diag.get('elapsed_s', 0)}s)"


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
                 clock=time.monotonic, wait=None, name="", recover=None,
                 recover_interval=5.0, recover_attempts=6, reverify_interval=_REVERIFY_EVERY):
        values = (right_limit, margin, poll_interval, stale_after, stop_interval,
                  cooldown, retry_interval, stable_tolerance)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("guard values must be finite")
        if min(margin, poll_interval, stale_after, stop_interval, retry_interval, stable_tolerance) <= 0 or cooldown < 0:
            raise ValueError("guard timing and margin must be positive")
        if stable_samples < 2:
            raise ValueError("at least two safe samples are required")
        self.name = name
        self.right_limit, self.left_limit = right_limit, left_limit
        self._episode: _Episode | None = None
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
        self._last_motion_delta = None
        self._safe_samples = 0
        self._healthy = self._latched = self._disable_ok = self._armed = False
        self._reason = "starting"
        self._trip_at = self._last_stop = self._last_disable = -math.inf
        self._recovery_until = -math.inf
        self._recovery_direction = 0
        self._recovery_target = 0.0
        self.reverify_interval = reverify_interval
        self._last_verified = -math.inf
        self._read_failures = self._stop_failures = self._off_failures = 0
        self._loop_errors = self._loop_streak = 0
        self._loop_warned = False
        # Debug-only state: recent reads for the pre-trip context, the cause of the
        # last failed read, the last refused rearm and periodic read statistics.
        self.recover, self.recover_interval = recover, recover_interval
        self.recover_attempts = recover_attempts
        self._recover_thread = None
        self._last_recover = -math.inf
        self._recent: deque = deque(maxlen=14)
        self._last_cause = "none"
        self._rearm_block = None
        self._stats_at = None
        self._stats = {"reads": 0, "failed": 0, "slow": 0, "max": 0.0, "sum": 0.0,
                       "stops": 0, "stops_failed": 0}

    def note(self, kind, detail=""):
        """Add a camera/daemon event (recall permit, plan) to the open episode's trace."""
        with self._lock:
            self._trace(kind, detail)

    def _trace(self, kind, detail=""):
        # Called with the guard lock held. Bounded: a long episode keeps its start.
        episode = self._episode
        if episode is None:
            return
        if len(episode.trace) >= _TRACE_LIMIT:
            episode.trace_dropped += 1
            return
        episode.trace.append((self.clock() - episode.started, kind, detail))

    def _record_read(self, position, read_s, cause=None):
        # Called with the guard lock held after every poll that touched the camera.
        now = self.clock()
        stats = self._stats
        stats["reads"] += 1
        stats["sum"] += read_s
        stats["max"] = max(stats["max"], read_s)
        if cause is not None:
            stats["failed"] += 1
        elif read_s > _SLOW_READ:
            stats["slow"] += 1
        self._recent.append((now, position, round(read_s, 3), cause))
        episode = self._episode
        if episode is not None and cause is None and position is not None:
            moved = (episode.last_traced_x is None
                     or abs(position - episode.last_traced_x) > _TRACE_MOVE)
            if moved or now - episode.last_traced_at >= _TRACE_HEARTBEAT:
                episode.last_traced_x, episode.last_traced_at = position, now
                self._trace("x", f"{position:.4f}")
        if self._stats_at is None:
            self._stats_at = now
        elif now - self._stats_at >= _STATS_EVERY:
            self._stats_at = now
            reads = max(1, stats["reads"])
            log.info("PTZ stop guard stats %s: reads=%d failed=%d slow>%.1fs=%d "
                     "read_mean=%.3fs read_max=%.3fs stops=%d stops_failed=%d",
                     self.name, stats["reads"], stats["failed"], _SLOW_READ, stats["slow"],
                     stats["sum"] / reads, stats["max"], stats["stops"], stats["stops_failed"])
            self._stats.update(reads=0, failed=0, slow=0, max=0.0, sum=0.0,
                               stops=0, stops_failed=0)

    def _safe(self, position):
        return position < self.right_inner and (self.left_inner is None or position > self.left_inner)

    def _is_suspended(self):
        for attempt in (0, 1):
            try:
                return bool(self.suspended())
            except RuntimeError:
                # A concurrently mutated registry (dict changed size) is transient:
                # one retry, so a poll and its Stop are not dropped for a race.
                if attempt:
                    return True
            except Exception:  # noqa: BLE001 - unknown privacy state cannot authorize movement
                return True
        return True

    def _note_suspended(self):
        with self._lock:
            self._healthy = self._armed = False
            self._last_motion_delta = None
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
            alive = self._thread is None or self._thread.is_alive()
            return GuardSnapshot(self._position, healthy, self._latched, allowed, pending,
                                 self._reason, alive, self._loop_errors)

    def _request_disable(self):
        with self._lock:
            pending = self._disable_thread is not None and self._disable_thread.is_alive()
            now = self.clock()
            stale = self._latched and now - self._last_verified >= self.reverify_interval
            gap = min(_OFF_BACKOFF_CAP, self.retry_interval * 2 ** max(0, self._off_failures - 1))
            if (self._disable_ok and not stale) or pending or now - self._last_disable < gap:
                return
            self._last_disable = now
            self._disable_thread = threading.Thread(target=self._disable, daemon=True,
                                                    name="ptz-stopguard-off")
            self._disable_thread.start()

    def _disable(self):
        with self._lock:
            episode = self._episode
        began = self.clock()
        error = "none"
        try:
            ok = self.disable_tracking() is True
        except Exception as exc:  # noqa: BLE001 - camera errors must not kill observation
            ok = False
            error = type(exc).__name__
        with self._lock:
            if episode is not None and episode is self._episode and (not ok or episode.off is None):
                self._trace("off", f"{'ok' if ok else 'FAIL ' + error} {self.clock() - began:.2f}s")
            warn = not ok and not self._disable_warned
            if ok:
                self._disable_ok = True
                self._off_failures = 0
                self._last_verified = self.clock()
            else:
                self._off_failures += 1
                # One transient failed re-verify keeps an earlier verified OFF.
                if not self._disable_ok or self._off_failures >= _OFF_FAILS_TO_UNVERIFY:
                    self._disable_ok = False
            self._disable_warned = not ok
            if episode is not None and episode is self._episode:
                if ok and episode.off is None:
                    episode.off = self.clock() - episode.started
                elif not ok:
                    episode.off_failed += 1
        if warn:
            log.warning("PTZ stop guard could not verify tracking OFF")

    def _maybe_recover(self):
        """Send the camera home once OFF is verified while it stands outside the safe band.

        Without this the lens waits for the next control pass (about a minute) next to
        the limit. The callback is the daemon's ordinary safe-preset recall, so it
        still needs the guard's verified inward-recovery permit. Bounded per episode.
        """
        if self.recover is None or self._is_suspended():
            return
        with self._lock:
            episode = self._episode
            pending = (self._recover_thread is not None and self._recover_thread.is_alive()) or (
                self._disable_thread is not None and self._disable_thread.is_alive())
            if (episode is None or not self._latched or not self._healthy or not self._disable_ok
                    or pending or episode.recoveries >= self.recover_attempts
                    or self._position is None or self._safe(self._position)
                    or self.clock() - self._last_recover < self.recover_interval):
                return
            episode.recoveries += 1
            self._last_recover = self.clock()
            self._recover_thread = threading.Thread(target=self._recover, args=(episode,),
                                                    daemon=True, name="ptz-stopguard-home")
            self._recover_thread.start()

    def _recover(self, episode):
        began = self.clock()
        try:
            ok = self.recover() is True
            error = "none"
        except Exception as exc:  # noqa: BLE001 - a failed recall must not kill observation
            ok, error = False, type(exc).__name__
        with self._lock:
            if episode is self._episode:
                self._trace("home", f"{'ok' if ok else 'deferred ' + error} {self.clock() - began:.2f}s")

    def _trip(self, reason, *, dispatch=True):
        with self._lock:
            new_trip = not self._latched
            self._latched = True
            self._armed = False
            self._reason = reason
            if new_trip:
                self._trip_at = self.clock()
                self._disable_ok = False
                self._safe_samples = 0
                self._recovery_until = -math.inf
                # The first Stop of a new episode is never held back by old failures.
                self._stop_failures = self._off_failures = 0
                self._last_stop = -math.inf
                if reason != "closed":
                    self._episode = _Episode(self._trip_at, self._position)
                    self._episode.pre = [(t - self._trip_at, x, rd, c)
                                         for t, x, rd, c in self._recent]
                    cause = self._last_cause if reason == "pose_unavailable" else reason
                    self._episode.cause = cause
                    self._rearm_block = None
                    age = "none" if self._observed_at is None else f"{self.clock() - self._observed_at:.3f}s"
                    log.info("PTZ stop guard trip %s: reason=%s x=%s pose_age=%s cause=%s",
                             self.name, reason, self._position, age, cause)
            if self._episode is not None:
                if reason not in self._episode.reasons:
                    self._episode.reasons.append(reason)
                self._observe_episode()
        if dispatch:
            self._request_disable()

    def _observe_episode(self):
        # Called with the guard lock held. Peaks are observed positions, not a
        # claim about motor travel during missing or stale reads.
        episode, x = self._episode, self._position
        if episode is None or x is None:
            return
        if episode.low is None or x < episode.low:
            episode.low, episode.low_at = x, self.clock() - episode.started
        if episode.high is None or x > episode.high:
            episode.high, episode.high_at = x, self.clock() - episode.started
        episode.beyond_limit |= x >= self.right_limit or (
            self.left_limit is not None and x <= self.left_limit)

    def _finish_episode(self, end):
        episode = self._episode
        if episode is None:
            return
        self._episode = None
        def pose(x):
            return "none" if x is None else f"{x:.4f}"
        def offset(value):
            return "none" if value is None else f"+{value:.2f}s"
        # Select the largest outward excursion relative to either inner boundary.
        peak, peak_at = episode.high, episode.high_at
        if (self.left_inner is not None and episode.low is not None and episode.high is not None
                and self.left_inner - episode.low > episode.high - self.right_inner):
            peak, peak_at = episode.low, episode.low_at
        level = logging.WARNING if (episode.beyond_limit or episode.first_stop_ok is None
                                    or episode.off is None) else logging.INFO
        log.log(level, "PTZ stop guard episode %s: reason=%s duration=%.2fs trip_x=%s peak_x=%s "
                "beyond_limit=%s reads_failed=%d stops=%d stops_failed=%d first_stop_ok=%s "
                "off=%s off_failed=%d peak_at=%s min_x=%s max_x=%s end=%s",
                self.name, ",".join(episode.reasons), self.clock() - episode.started,
                pose(episode.trip_x), pose(peak), episode.beyond_limit, episode.reads_failed,
                episode.stops, episode.stops_failed, offset(episode.first_stop_ok),
                "unverified" if episode.off is None else "verified" + offset(episode.off),
                episode.off_failed, offset(peak_at), pose(episode.low), pose(episode.high), end)
        self._trace_episode(episode, end, level)

    def _trace_episode(self, episode, end, level):
        # One line per episode: what the pose reads looked like before the trip and
        # what happened to the pose and the camera until the end. Debug context only.
        pre = ";".join(f"{t:+.1f}s:{'ERR ' + c if c else format(x, '.4f')}/{rd:.2f}s"
                       for t, x, rd, c in episode.pre[-10:])
        events = ";".join(f"+{t:.1f}s:{kind}={detail}" if detail else f"+{t:.1f}s:{kind}"
                          for t, kind, detail in episode.trace)
        dropped = f" dropped={episode.trace_dropped}" if episode.trace_dropped else ""
        log.log(level, "PTZ stop guard trace %s: cause=%s end=%s pre=[%s] events=[%s]%s",
                self.name, episode.cause, end, pre, events, dropped)

    @staticmethod
    def _non_transport(exc):
        errors = getattr(exc, "rpc_diag", None)
        errors = errors.get("errors") if isinstance(errors, dict) else None
        return bool(errors) and all(str(e).endswith("/other") for e in errors)

    def _backoff(self, base, failures):
        if failures < _BACKOFF_AFTER:
            return base
        return min(max(base, _BACKOFF_CAP), base * 2 ** (failures - _BACKOFF_AFTER + 1))

    def _stop_gap(self):
        # Called with the lock held. Repeated Stop failures (auth error, offline camera)
        # slow down to a bounded rate; they never stop, and success resets the gap.
        return self._backoff(self.stop_interval, self._stop_failures)

    def _poll_delay(self):
        with self._lock:
            delay = self._backoff(self.poll_interval, self._loop_streak)
            if self._read_failures >= _BACKOFF_AFTER:
                # Only repeated non-transport errors (wrong credentials, SOAP faults) slow
                # the reads; timeouts and connection loss keep the base rate.
                step = self.poll_interval * 2 ** (self._read_failures - _BACKOFF_AFTER + 1)
                delay = max(delay, min(_READ_BACKOFF_CAP, max(_READ_BACKOFF_FLOOR, step)))
            return delay

    def _stop_if_due(self, *, force=False):
        if self._is_suspended():
            self._note_suspended()
            return
        with self._lock:
            if not force and self.clock() < self._recovery_until:
                return
            if not force and self.clock() - self._last_stop < self._stop_gap():
                return
            self._last_stop = self.clock()
            episode = self._episode
            if episode is not None:
                episode.stops += 1
        began = self.clock()
        try:
            self.stop()
            with self._lock:
                self._stats["stops"] += 1
                if episode is not None and episode is self._episode:
                    if episode.first_stop_ok is None:
                        episode.first_stop_ok = self.clock() - episode.started
                    if episode.stops_traced < 3:
                        episode.stops_traced += 1
                        self._trace("stop", f"ok {self.clock() - began:.2f}s")
            self._stop_warned = False
            with self._lock:
                self._stop_failures = 0
        except Exception as exc:  # noqa: BLE001 - never expose exception text/credentials
            diag = getattr(exc, "rpc_diag", None)
            with self._lock:
                self._stop_failures += 1
                self._stats["stops"] += 1
                self._stats["stops_failed"] += 1
                if episode is not None and episode is self._episode:
                    episode.stops_failed += 1
                    self._trace("stop", f"FAIL {ptzrpc.diagnose(exc)} {self.clock() - began:.2f}s"
                                f"{_diag_text(diag)}")
            if not self._stop_warned:
                log.warning("PTZ stop guard Stop failed name=%s cause=%s", self.name,
                            ptzrpc.diagnose(exc))
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
        except Exception as exc:  # noqa: BLE001 - fail closed on unavailable or stale position
            read_s = self.clock() - started
            cause = f"{ptzrpc.diagnose(exc)}{_diag_text(getattr(exc, 'rpc_diag', None))}"
            with self._lock:
                self._healthy = False
                self._last_motion_delta = None
                self._safe_samples = 0
                self._recovery_until = -math.inf
                self._last_cause = cause
                self._read_failures = (self._read_failures + 1
                                       if self._non_transport(exc) else 0)
                self._record_read(None, read_s, cause)
            self._trip("pose_unavailable")
            with self._lock:
                if self._episode is not None:
                    self._episode.reads_failed += 1
                    self._trace("readfail", f"{cause} {read_s:.2f}s")
            self._stop_if_due()
            return
        # Privacy may have changed while a bounded network read was in flight.
        if self._is_suspended():
            self._note_suspended()
            return
        now = self.clock()
        with self._lock:
            self._record_read(position, now - started)
            self._read_failures = 0
            if self._reason == "suspended" and not self._latched:
                self._reason = "starting"
            previous = (self._position if self._healthy and self._observed_at is not None
                        and now - self._observed_at <= self.stale_after else None)
            self._last_motion_delta = position - previous if previous is not None else None
            self._position, self._observed_at, self._healthy = position, now, True
            self._observe_episode()
            safe = self._safe(position)
            self._safe_samples = (self._safe_samples + 1 if safe and previous is not None
                                  and abs(position - previous) <= self.stable_tolerance else int(safe))
            latched = self._latched
            recovering = now < self._recovery_until
            recovery_outward = recovering and previous is not None and (
                (position - previous) * self._recovery_direction < -1e-6)
            inward = previous is not None and (
                position < previous - 1e-6 if position >= self.right_inner else position > previous + 1e-6)
            # The permit covers only a move toward the verified target: once the lens is
            # past it (heading for the opposite boundary) the permit ends and Stop is forced.
            overshoot = recovering and (position - self._recovery_target) * self._recovery_direction > (
                max(self.stable_tolerance * 5, 0.01))
            if overshoot:
                recovery_outward = True
            if recovery_outward or previous is None:
                self._recovery_until = -math.inf
            if previous is None:
                self._armed = False
            if safe and not latched and self._safe_samples >= self.stable_samples:
                self._armed = True
                self._reason = "armed"
            # Latch in the same critical section as the decision, so a control-thread
            # write checked between here and the Stop cannot see tracking_allowed.
            if recovery_outward:
                self._trip("recovery_outward", dispatch=False)
            if not safe:
                self._trip("boundary", dispatch=False)
        if recovery_outward:
            self._stop_if_due(force=True)
        if not safe:
            # Stop before dispatching the slower native API work.
            if not latched or not inward:
                self._stop_if_due()
            self._request_disable()
            self._maybe_recover()
        elif latched:
            # A readable interior pose does not acknowledge a failed Stop. Nor can
            # one Stop prevent native tracking resuming while OFF is still pending.
            with self._lock:
                needs_stop = (not self._disable_ok or (self._episode is not None
                              and self._episode.first_stop_ok is None))
            if needs_stop:
                self._stop_if_due()   # retains the verified inward-recovery permit
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
            # A verified Stop/OFF does not prove the motor has ceased moving.
            # Do not issue a home preset while fresh observations show motion
            # away from that target; keep the latch and normal Stop retries.
            delta = self._last_motion_delta
            if (delta is not None and abs(delta) > 1e-6
                    and delta * (target - self._position) < 0):
                self._trace("recall_refused", "outward_motion")
                return False
            self._recovery_direction = 1 if target > self._position else -1
            self._recovery_target = target
            self._recovery_until = self.clock() + timeout
            return True

    def rearm(self):
        """Explicitly release the latch only after fresh stable interior observations."""
        with self._lock:
            state = self.snapshot()
            blocked = ("unhealthy" if not state.healthy
                       else "unstable" if self._safe_samples < self.stable_samples
                       else "unsafe" if not self._safe(self._position)
                       else "off_pending" if state.disable_pending
                       else "off_unverified" if self._latched and not self._disable_ok
                       else "cooldown" if self._latched and self.clock() - self._trip_at < self.cooldown
                       else None)
            if blocked is not None:
                if self._latched and blocked != self._rearm_block:
                    self._rearm_block = blocked
                    self._trace("rearm_blocked", f"{blocked} x={self._position}")
                return False
            self._trace("rearm")
            self._latched, self._reason, self._armed = False, "armed", True
            self._disable_ok = False
            self._recovery_until = -math.inf
            self._finish_episode("rearm")
            return True

    def _run(self):
        try:
            while not self._exit.is_set():
                try:
                    self.poll_once()
                    with self._lock:
                        self._loop_streak, self._loop_warned = 0, False
                except Exception as exc:  # noqa: BLE001 - an unexpected error must not end observation
                    self._loop_failed(exc)
                if self._wait(self._poll_delay()):
                    break
        finally:
            self._stop_if_due()

    def _loop_failed(self, exc):
        # Fail closed: unknown guard state means no tracking, then keep observing.
        with self._lock:
            self._loop_errors += 1
            self._loop_streak += 1
            self._healthy = False
            self._safe_samples = 0
            self._recovery_until = -math.inf
            warn = not self._loop_warned
            self._loop_warned = True
        if warn:
            log.error("PTZ stop guard loop error %s: %s", self.name, type(exc).__name__,
                      exc_info=exc)
        try:
            self._trip("guard_error")
            self._stop_if_due()
        except Exception:  # noqa: BLE001 - the loop itself must survive
            pass

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
            self._finish_episode("closed")
            return self._disable_ok and not pending
