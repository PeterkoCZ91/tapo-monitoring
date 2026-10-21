"""Regression tests for the stop guard hardening (latch, recovery permit, backoff)."""

import pytest

from tapo_monitor.stopguard import StopGuard


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds=0.2):
        self.now += seconds


def build(positions, *, stop=None, **kwargs):
    clock = Clock()
    trace = []
    poses = iter(positions)

    def read():
        value = next(poses)
        if isinstance(value, Exception):
            raise value
        return value

    def default_stop():
        trace.append("stop")

    guard = StopGuard(read, stop or default_stop, lambda: trace.append("off") or True,
                      right_limit=0.8, margin=0.1, clock=clock, **kwargs)
    return guard, clock, trace


def await_off(guard):
    thread = guard._disable_thread
    if thread is not None:
        thread.join(1)
        assert not thread.is_alive()


def test_latch_is_set_inside_the_decision_critical_section():
    guard, clock, _ = build([0.5, 0.5, 0.75])
    held = []
    real = guard._trip

    def spy(reason, **kw):
        held.append((reason, guard._lock._is_owned()))
        return real(reason, **kw)

    guard._trip = spy
    for _ in range(3):
        guard.poll_once()
        clock.advance()
    assert ("boundary", True) in held
    await_off(guard)


def _latched_with_permit(target, poses, **kwargs):
    guard, clock, trace = build(poses, **kwargs)
    guard.poll_once()
    await_off(guard)
    clock.advance()
    guard.poll_once()          # stationary unsafe pose, OFF verified
    await_off(guard)
    clock.advance()
    assert guard.prepare_recovery(target)
    return guard, clock, trace


def test_recovery_permit_ends_when_lens_passes_target_toward_opposite_boundary():
    guard, clock, trace = _latched_with_permit(-0.6, [0.85, 0.85, -0.5, -0.9],
                                               left_limit=-0.8)
    trace.clear()
    guard.poll_once()          # moving inward but short of target: permit holds
    assert "stop" not in trace
    clock.advance()
    guard.poll_once()          # past target and into the opposite unsafe zone
    assert "stop" in trace
    assert guard._recovery_until == float("-inf")


def test_recovery_permit_ends_when_safe_lens_passes_target():
    guard, clock, trace = _latched_with_permit(0.3, [0.85, 0.85, 0.5, 0.1], left_limit=None)
    trace.clear()
    guard.poll_once()
    assert "stop" not in trace
    clock.advance()
    guard.poll_once()          # safe, but beyond the target: Stop is forced
    assert "stop" in trace


def test_stationary_lens_keeps_permit_until_the_move_starts():
    guard, clock, trace = _latched_with_permit(
        0.3, [0.85, 0.85, 0.85, 0.85, 0.1], left_limit=None)
    assert guard._recovery_until > clock.now           # permit granted at t0
    trace.clear()
    for _ in range(2):                                 # still parked in the unsafe zone
        guard.poll_once()
        clock.advance()
        assert "stop" not in trace
        assert guard._recovery_until > clock.now       # permit untouched while stationary
    guard.poll_once()                                  # the move starts, straight past target
    assert "stop" in trace
    assert guard._recovery_until == float("-inf")      # revoked only now


class AuthError(Exception):
    rpc_diag = {"attempts": 1, "errors": ["Fault/other"], "elapsed_s": 0.01}


class TimeoutErr(Exception):
    rpc_diag = {"attempts": 4, "errors": ["Timeout/read"] * 4, "elapsed_s": 0.8}


def test_transport_read_failures_never_slow_observation():
    guard, clock, _ = build([TimeoutErr()] * 12)
    for _ in range(12):
        guard.poll_once()
        assert guard._poll_delay() == guard.poll_interval
    assert not guard.snapshot().tracking_allowed


def test_auth_read_failures_back_off_bounded_and_not_below_half_second():
    guard, clock, _ = build([AuthError()] * 12)
    delays = []
    for _ in range(12):
        guard.poll_once()
        delays.append(guard._poll_delay())
    assert delays[0] == delays[1] == guard.poll_interval
    assert delays == sorted(delays)
    assert 0.5 <= delays[-1] <= 1.0


def test_read_recovery_resets_backoff():
    guard, clock, _ = build([AuthError()] * 6 + [0.5])
    for _ in range(7):
        guard.poll_once()
        clock.advance()
    assert guard._poll_delay() == guard.poll_interval


def test_failing_stop_for_unsafe_lens_slows_but_never_goes_silent():
    calls = []

    def failing_stop():
        calls.append(clock.now)
        raise RuntimeError("auth")

    guard, clock, _ = build([0.9] * 60, stop=failing_stop)
    for _ in range(50):          # 10 simulated seconds
        guard.poll_once()
        clock.advance()
    assert 5 <= len(calls) <= 15  # 50 without the backoff
    assert max(b - a for a, b in zip(calls, calls[1:], strict=False)) <= 2.2 + 1e-9
    await_off(guard)


def test_successful_stop_resets_stop_backoff():
    outcome = [RuntimeError("x")] * 5
    calls = []

    def flaky():
        calls.append(clock.now)
        if outcome:
            outcome.pop()
            raise RuntimeError("x")

    guard, clock, _ = build([0.9] * 80, stop=flaky)
    for _ in range(80):
        guard.poll_once()
        clock.advance(0.1)
    gaps = [b - a for a, b in zip(calls, calls[1:], strict=False)]
    assert guard._stop_failures == 0
    assert max(gaps[:5]) >= 1.0
    assert gaps[-1] < 0.3
    await_off(guard)


def test_new_episode_first_stop_ignores_old_failure_backoff():
    calls = []

    def failing():
        calls.append(clock.now)
        raise RuntimeError("x")

    guard, clock, _ = build([0.9] * 8 + [0.5, 0.5, 0.9], stop=failing)
    for _ in range(8):
        guard.poll_once()
        clock.advance(0.5)
    await_off(guard)
    assert guard._stop_failures >= 5
    for _ in range(2):
        guard.poll_once()
        clock.advance(0.5)
    clock.advance(0.1)
    assert guard.rearm()
    guard._last_stop = clock.now - 0.01
    before = len(calls)
    guard.poll_once()
    assert len(calls) == before + 1


# default stable_tolerance 0.002 -> margin max(0.002 * 5, 0.01) = 0.01 beyond the target
@pytest.mark.parametrize("pose", [0.295, 0.2915])
def test_overshoot_margin_tolerates_normal_settling(pose):
    guard, clock, trace = _latched_with_permit(0.3, [0.85, 0.85, pose], left_limit=None)
    trace.clear()
    guard.poll_once()
    assert "stop" not in trace
    assert guard._recovery_until > clock.now           # permit survives settling


@pytest.mark.parametrize("pose", [0.2885, 0.25])
def test_overshoot_beyond_the_margin_forces_stop(pose):
    guard, clock, trace = _latched_with_permit(0.3, [0.85, 0.85, pose], left_limit=None)
    trace.clear()
    guard.poll_once()
    assert "stop" in trace
    assert guard._recovery_until == float("-inf")


def test_loop_survives_unexpected_poll_exception_and_reports_it():
    waits = []

    def wait(delay):
        waits.append(delay)
        return len(waits) >= 4

    guard, clock, trace = build([0.5] * 10, wait=wait)
    real = guard.poll_once
    state = {"n": 0}

    def flaky_poll():
        state["n"] += 1
        if state["n"] == 1:
            raise KeyError("boom")
        real()

    guard.poll_once = flaky_poll
    guard._run()
    assert state["n"] == 4
    snap = guard.snapshot()
    assert snap.loop_errors == 1
    assert not snap.tracking_allowed
    await_off(guard)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_snapshot_reports_dead_guard_thread():
    guard, _, _ = build([0.5])
    assert guard.snapshot().thread_alive

    def die():
        raise SystemExit  # not an Exception: the supervisor cannot absorb it

    guard.poll_once = die
    guard.start()
    guard._thread.join(1)
    snap = guard.snapshot()
    assert not snap.thread_alive
    assert not snap.tracking_allowed


def test_transient_runtime_error_in_suspended_does_not_drop_the_poll():
    state = {"n": 0}

    def suspended():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("dictionary changed size during iteration")
        return False

    guard, clock, _ = build([0.5, 0.5], suspended=suspended)
    guard.poll_once()
    assert guard.snapshot().position == 0.5


def test_persistent_runtime_error_in_suspended_still_fails_closed():
    def suspended():
        raise RuntimeError("dictionary changed size during iteration")

    guard, clock, _ = build([0.5, 0.5], suspended=suspended)
    guard.poll_once()
    assert guard.snapshot().position is None
    assert not guard.snapshot().tracking_allowed


def test_off_is_reverified_periodically_while_latched():
    guard, clock, trace = build([0.9] * 20)
    guard.poll_once()
    await_off(guard)
    for _ in range(5):
        clock.advance()
        guard.poll_once()
        await_off(guard)
    assert trace.count("off") == 1
    clock.advance(31)
    guard.poll_once()
    await_off(guard)
    assert trace.count("off") == 2


def test_single_failed_reverify_keeps_verified_off_second_clears_it():
    results = iter([True, False, False])
    guard, clock, trace = build([0.9] * 20)
    guard.disable_tracking = lambda: next(results, False)
    guard.poll_once()
    await_off(guard)
    assert guard._disable_ok
    clock.advance(31)
    guard.poll_once()
    await_off(guard)
    assert guard._disable_ok            # one transient failure
    clock.advance(1.1)
    guard.poll_once()
    await_off(guard)
    assert not guard._disable_ok


def test_off_retry_backs_off_to_ten_seconds():
    attempts = []
    guard, clock, _ = build([0.9] * 200)
    guard.disable_tracking = lambda: attempts.append(clock.now) or False
    for _ in range(200):
        guard.poll_once()
        await_off(guard)
        clock.advance(0.5)
    gaps = [b - a for a, b in zip(attempts, attempts[1:], strict=False)]
    assert len(attempts) < 25            # every second would be ~100
    assert max(gaps) <= 10.5
    assert gaps[-1] >= 8
