"""Replay boundary races without camera connections or wall-clock sleeps."""

import threading
import time

import pytest

from tapo_monitor.stopguard import StopGuard


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds=0.2):
        self.now += seconds


def guard_for(positions, **kwargs):
    clock = Clock()
    trace = []
    poses = iter(positions)
    guard = StopGuard(lambda: next(poses), lambda: trace.append("stop"),
                      lambda: trace.append("off") or True,
                      right_limit=0.8, margin=0.1, clock=clock, **kwargs)
    return guard, clock, trace


def await_off(guard):
    # This is waiting for an injected in-process callback, never network I/O.
    thread = guard._disable_thread
    if thread is not None:
        thread.join(1)
        assert not thread.is_alive()


def test_startup_requires_two_stable_fresh_samples_and_left_is_unlimited():
    guard, clock, trace = guard_for([-0.95, -0.95, -0.95])
    assert not guard.snapshot().tracking_allowed
    guard.poll_once()
    assert not guard.snapshot().tracking_allowed
    clock.advance()
    guard.poll_once()
    assert guard.snapshot().tracking_allowed
    assert not trace
    clock.advance(1.1)
    assert not guard.snapshot().tracking_allowed
    guard.poll_once()
    assert not guard.snapshot().tracking_allowed  # stale gap breaks stability
    guard.close()


def test_latch_precedes_stop_and_off_cannot_block_observer():
    callback_entered, release = threading.Event(), threading.Event()
    positions = iter([0.5, 0.5, 0.71, 0.72])
    clock = Clock()
    stops = []
    guard = None

    def stop():
        assert guard.snapshot().latched
        stops.append(guard.snapshot().tracking_allowed)

    def off():
        callback_entered.set()
        release.wait(2)
        return True

    guard = StopGuard(lambda: next(positions), stop, off, right_limit=0.8, margin=0.1, clock=clock)
    try:
        for _ in range(3):
            guard.poll_once()
            clock.advance()
        assert callback_entered.wait(1)
        assert guard.snapshot().latched
        assert guard.snapshot().disable_pending
        guard.poll_once()  # continues while native OFF is blocked
        assert stops == [False, False]
        assert guard._disable_thread is not None
    finally:
        release.set()
        guard.close()


def test_inward_recovery_not_stopped_and_rearm_needs_stability_cooldown_and_off():
    guard, clock, trace = guard_for([0.71, 0.70, 0.6, 0.6, 0.6])
    guard.poll_once()
    await_off(guard)
    assert trace == ["stop", "off"]
    clock.advance()
    guard.poll_once()  # still outside inner range, but moving inward
    assert trace.count("stop") == 1
    clock.advance()
    guard.poll_once()
    assert not guard.rearm()
    clock.advance()
    guard.poll_once()
    assert not guard.rearm()  # stable, but cooldown still active
    clock.advance(2)
    guard.poll_once()
    assert not guard.rearm()  # long gap needs a second fresh sample
    guard.read_position = lambda: 0.6
    clock.advance()
    guard.poll_once()
    assert guard.rearm()
    assert guard.snapshot().tracking_allowed
    guard.close()


def test_stationary_unsafe_pose_repeats_stop_at_bounded_rate():
    guard, clock, trace = guard_for([0.75] * 4)
    guard.poll_once()
    await_off(guard)
    guard.poll_once()
    assert trace.count("stop") == 1
    clock.advance()
    guard.poll_once()
    assert trace.count("stop") == 2
    guard.close()


@pytest.mark.parametrize("value", [None, float("nan"), float("inf")])
def test_bad_pose_latches_and_requests_stop_and_off(value):
    guard, _, trace = guard_for([value])
    guard.poll_once()
    await_off(guard)
    assert guard.snapshot().latched
    assert not guard.snapshot().healthy
    assert sorted(trace) == ["off", "stop"]
    assert not guard.rearm()
    guard.close()


def test_read_failure_disables_tracking_without_leaking_exception(caplog):
    def fail():
        raise OSError("sensitive camera endpoint")

    guard, _, trace = guard_for([])
    guard.read_position = fail
    guard.poll_once()
    await_off(guard)
    assert not guard.snapshot().tracking_allowed
    assert guard.snapshot().reason == "pose_unavailable"
    assert "sensitive" not in caplog.text
    assert sorted(trace) == ["off", "stop"]
    guard.close()


def test_slow_read_is_not_accepted_as_fresh():
    guard, clock, _ = guard_for([])

    def slow():
        clock.advance(1.1)
        return 0.5

    guard.read_position = slow
    guard.poll_once()
    assert not guard.snapshot().healthy
    assert guard.snapshot().latched
    guard.close()


def test_failed_off_is_retried_without_parallel_jobs_or_rearm():
    guard, clock, trace = guard_for([0.75, 0.5, 0.5, 0.5])
    calls = []
    guard.disable_tracking = lambda: calls.append(1) or False
    guard.poll_once()
    await_off(guard)
    for _ in range(2):
        clock.advance()
        guard.poll_once()
    assert len(calls) == 1
    assert not guard.rearm()
    clock.advance(2)
    guard.poll_once()
    await_off(guard)
    assert len(calls) == 2
    assert not guard.rearm()
    assert trace.count("stop") == 4  # keep stopping while OFF remains unverified
    guard.close()


def test_optional_left_boundary_stops_outward_but_allows_inward():
    guard, clock, trace = guard_for([0.05, 0.08, 0.08], left_limit=0.0)
    guard.poll_once()
    await_off(guard)
    clock.advance()
    guard.poll_once()
    assert trace.count("stop") == 1
    clock.advance()
    guard.poll_once()
    assert trace.count("stop") == 2
    guard.close()


def test_worker_close_has_bounded_join_even_when_off_callback_stalls():
    entered, release = threading.Event(), threading.Event()

    def off():
        entered.set()
        release.wait(2)
        return True

    guard = StopGuard(lambda: 0.75, lambda: None, off, right_limit=0.8, margin=0.1,
                      poll_interval=0.01).start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        assert guard.close(timeout=0.05) is False
        assert time.monotonic() - started < 0.5
        assert not guard.snapshot().tracking_allowed
        with pytest.raises(RuntimeError):
            guard.start()
    finally:
        release.set()
        guard.close()


@pytest.mark.parametrize("kwargs", [{"margin": 0}, {"margin": float("nan")},
                                    {"left_limit": 0.65}, {"stable_samples": 1},
                                    {"poll_interval": 0}])
def test_invalid_configuration_rejected(kwargs):
    defaults = dict(right_limit=0.8, margin=0.1)
    defaults.update(kwargs)
    with pytest.raises(ValueError):
        StopGuard(lambda: 0, lambda: None, lambda: True, **defaults)


def test_safe_movement_does_not_disarm_an_established_tracker():
    guard, clock, trace = guard_for([0.3, 0.3, 0.4, 0.5, 0.6])
    guard.poll_once()
    clock.advance()
    guard.poll_once()
    assert guard.snapshot().tracking_allowed
    for _ in range(3):
        clock.advance()
        guard.poll_once()
        assert guard.snapshot().tracking_allowed
    assert not trace
    guard.close()


def test_privacy_suspension_prevents_reads_and_stops_then_requires_fresh_resume():
    suspended = [False]
    guard, clock, trace = guard_for([0.4, 0.4, 0.4, 0.4], suspended=lambda: suspended[0])
    for _ in range(2):
        guard.poll_once()
        clock.advance()
    assert guard.snapshot().tracking_allowed
    original_read = guard.read_position
    reads = []
    guard.read_position = lambda: reads.append(1) or 0.9
    suspended[0] = True
    guard.poll_once()
    assert not reads
    assert not trace  # no motor Stop or pointless repeated native disable
    state = guard.snapshot()
    assert not state.healthy and not state.tracking_allowed
    assert state.reason == "suspended"
    suspended[0] = False
    guard.read_position = original_read
    guard.poll_once()
    assert not guard.snapshot().tracking_allowed
    clock.advance()
    guard.poll_once()
    assert guard.snapshot().tracking_allowed
    suspended[0] = True
    guard.close()
    assert "stop" not in trace


def test_suspension_during_read_does_not_issue_motor_stop():
    suspended = [False]
    guard, _, trace = guard_for([], suspended=lambda: suspended[0])

    def read():
        suspended[0] = True
        return 0.9

    guard.read_position = read
    guard.poll_once()
    assert not trace
    assert guard.snapshot().reason == "suspended"
    guard.close()
    assert "stop" not in trace


def test_suspended_callback_error_suppresses_motor_and_pose_reads():
    def unknown_privacy():
        raise RuntimeError("private endpoint")

    guard, _, trace = guard_for([], suspended=unknown_privacy)
    guard.poll_once()  # would exhaust the pose iterator if it were called
    assert not trace
    assert guard.snapshot().reason == "suspended"
    guard.close()
    assert "stop" not in trace


def test_existing_trip_stays_latched_across_suspension():
    suspended = [False]
    guard, clock, trace = guard_for([0.9, 0.4, 0.4], suspended=lambda: suspended[0])
    guard.poll_once()
    await_off(guard)
    suspended[0] = True
    clock.advance(3)
    guard.poll_once()
    assert guard.snapshot().latched
    assert not guard.rearm()
    suspended[0] = False
    for _ in range(2):
        clock.advance()
        guard.poll_once()
    assert guard.snapshot().latched
    assert guard.rearm()
    assert trace.count("stop") == 1
    guard.close()


def test_worker_finally_does_not_stop_suspended_lens():
    waited = threading.Event()
    trace = []

    def wait(_):
        waited.set()
        return True

    guard = StopGuard(lambda: trace.append("read") or 0.9,
                      lambda: trace.append("stop"), lambda: True,
                      right_limit=0.8, margin=0.1, suspended=lambda: True, wait=wait).start()
    assert waited.wait(1)
    guard.close()
    assert not trace


def test_recovery_permit_allows_acceleration_then_inward_motion():
    guard, clock, trace = guard_for([0.75, 0.75, 0.73, 0.65])
    guard.poll_once()
    await_off(guard)
    assert guard.prepare_recovery(0.5)
    for _ in range(3):
        clock.advance()
        guard.poll_once()
        assert guard.snapshot().latched
        assert not guard.snapshot().tracking_allowed
    assert trace.count("stop") == 1
    guard.close()


def test_recall_refused_while_fresh_samples_move_away_from_the_target():
    guard, clock, trace = guard_for([0.75, 0.75, 0.78])
    guard.poll_once()
    await_off(guard)
    clock.advance()
    guard.poll_once()
    clock.advance()
    guard.poll_once()  # 0.75 -> 0.78: still drifting outward after the verified OFF
    assert not guard.prepare_recovery(0.5)
    guard.close()


def test_recall_allowed_once_the_motion_stops():
    guard, clock, trace = guard_for([0.75, 0.75, 0.75])
    guard.poll_once()
    await_off(guard)
    clock.advance()
    guard.poll_once()
    clock.advance()
    guard.poll_once()
    assert guard.prepare_recovery(0.5)
    guard.close()


def test_recovery_permit_outward_motion_forces_stop_and_cancels_permit():
    guard, clock, trace = guard_for([0.75, 0.76, 0.76])
    guard.poll_once()
    await_off(guard)
    assert guard.prepare_recovery(0.5)
    clock.advance(0.01)  # safety cancellation bypasses normal Stop rate limit
    guard.poll_once()
    assert trace.count("stop") == 2
    clock.advance()
    guard.poll_once()
    assert trace.count("stop") == 3
    guard.close()


def test_expired_recovery_permit_resumes_stop_at_stationary_unsafe_pose():
    guard, clock, trace = guard_for([0.75, 0.75, 0.75])
    guard.poll_once()
    await_off(guard)
    assert guard.prepare_recovery(0.5, timeout=0.3)
    clock.advance()
    guard.poll_once()
    assert trace.count("stop") == 1
    clock.advance()
    guard.poll_once()
    assert trace.count("stop") == 2
    guard.close()


def test_recovery_permit_requires_verified_off_and_safe_target():
    guard, _, _ = guard_for([0.75])
    guard.disable_tracking = lambda: False
    guard.poll_once()
    await_off(guard)
    assert not guard.prepare_recovery(0.5)
    guard.close()
    guard, _, _ = guard_for([0.75])
    guard.poll_once()
    await_off(guard)
    assert not guard.prepare_recovery(0.75)
    assert not guard.prepare_recovery(0.5, timeout=3)
    guard.suspended = lambda: True
    assert not guard.prepare_recovery(0.5)
    guard.close()


def test_close_reports_only_verified_completed_native_off():
    guard, _, trace = guard_for([])
    assert guard.close() is True
    assert trace == ["off"]
    guard, _, _ = guard_for([])
    guard.disable_tracking = lambda: False
    assert guard.close() is False


def episode_lines(caplog):
    return [r for r in caplog.records if "stop guard episode" in r.getMessage()]


def test_episode_summary_counts_every_stop_failure_despite_deduplicated_warning(caplog):
    clock = Clock()
    poses = iter([0.5, 0.5, 0.75, 0.83, 0.86, 0.6, 0.6, 0.6])
    stops = iter([OSError, OSError, None, None])

    def stop():
        exc = next(stops)
        if exc is not None:
            raise exc("lost")

    guard = StopGuard(lambda: next(poses), stop, lambda: True, name="cam",
                      right_limit=0.8, margin=0.1, cooldown=0.5, clock=clock)
    caplog.set_level("INFO", logger="tapo_monitor.stopguard")
    for _ in range(8):
        guard.poll_once()
        await_off(guard)
        clock.advance()
    warnings = [r.getMessage() for r in caplog.records if "Stop failed" in r.getMessage()]
    assert len(warnings) == 1
    assert "cause=other" in warnings[0] and "lost" not in warnings[0]
    assert guard.rearm()
    [line] = episode_lines(caplog)
    message = line.getMessage()
    assert line.levelname == "WARNING"  # observed beyond the configured limit itself
    assert "cam" in message and "reason=boundary" in message
    assert "trip_x=0.7500" in message and "peak_x=0.8600" in message
    assert "beyond_limit=True" in message
    assert "stops=3 stops_failed=2" in message
    assert "first_stop_ok=+0.40s" in message
    assert "off=verified+" in message
    guard.close()


def test_pose_loss_episode_without_motion_is_info_and_counts_reads(caplog):
    clock = Clock()
    reads = iter([0.5, 0.5, None, None, 0.5, 0.5, 0.5])

    def read():
        value = next(reads)
        if value is None:
            raise TimeoutError("late")
        return value

    guard = StopGuard(read, lambda: None, lambda: True, right_limit=0.8, margin=0.1,
                      cooldown=0.5, clock=clock)
    caplog.set_level("INFO", logger="tapo_monitor.stopguard")
    for _ in range(7):
        guard.poll_once()
        await_off(guard)
        clock.advance()
    assert guard.rearm()
    [line] = episode_lines(caplog)
    message = line.getMessage()
    assert line.levelname == "INFO"
    assert "reason=pose_unavailable" in message and "reads_failed=2" in message
    assert "trip_x=0.5000" in message and "peak_x=0.5000" in message
    assert "beyond_limit=False" in message and "stops_failed=0" in message
    guard.close()


def test_episode_without_any_confirmed_stop_is_a_warning(caplog):
    clock = Clock()
    poses = iter([0.5, 0.5, 0.75])

    def stop():
        raise OSError("lost")

    guard = StopGuard(lambda: next(poses), stop, lambda: False, right_limit=0.8,
                      margin=0.1, clock=clock)
    caplog.set_level("INFO", logger="tapo_monitor.stopguard")
    for _ in range(3):
        guard.poll_once()
        await_off(guard)
        clock.advance()
    guard.close()
    [line] = episode_lines(caplog)
    assert line.levelname == "WARNING"
    assert "first_stop_ok=none" in line.getMessage()
    assert "off=unverified" in line.getMessage()
    assert "end=closed" in line.getMessage()


def test_left_limit_peak_and_episodes_reset_after_rearm(caplog):
    guard, clock, _ = guard_for([-0.55, -0.65, 0.4, 0.4, 0.75, 0.4, 0.4],
                                left_limit=-0.6, cooldown=0)
    caplog.set_level("INFO", logger="tapo_monitor.stopguard")
    for _ in range(4):
        guard.poll_once()
        await_off(guard)
        clock.advance()
    assert guard.rearm()
    for _ in range(3):
        guard.poll_once()
        await_off(guard)
        clock.advance()
    assert guard.rearm()
    first, second = episode_lines(caplog)
    assert "peak_x=-0.6500" in first.getMessage()
    assert "beyond_limit=True" in first.getMessage()
    assert "peak_at=+0.20s" in first.getMessage()
    assert "beyond_limit=False" in second.getMessage()
    assert "stops=2 stops_failed=0" in second.getMessage()  # OFF retry was still pending
    guard.close()
    assert len(episode_lines(caplog)) == 2  # shutdown alone is not a trip


def test_missing_startup_pose_and_unverified_off_are_not_reported_healthy(caplog):
    guard, _, _ = guard_for([])
    guard.disable_tracking = lambda: False
    caplog.set_level("INFO", logger="tapo_monitor.stopguard")
    guard.poll_once()
    await_off(guard)
    guard.close()
    [line] = episode_lines(caplog)
    assert line.levelname == "WARNING"
    assert "trip_x=none peak_x=none" in line.getMessage()
    assert "reads_failed=1" in line.getMessage()
    assert "off=unverified off_failed=1" in line.getMessage()


def test_close_with_off_in_flight_logs_once_and_keeps_unverified_result(caplog):
    entered, release = threading.Event(), threading.Event()
    guard, _, _ = guard_for([0.85])

    def off():
        entered.set()
        assert release.wait(2)
        return True

    guard.disable_tracking = off
    caplog.set_level("INFO", logger="tapo_monitor.stopguard")
    try:
        guard.poll_once()
        assert entered.wait(1)
        assert guard.close(timeout=0) is False
        [line] = episode_lines(caplog)
        assert "off=unverified" in line.getMessage()
    finally:
        release.set()
        await_off(guard)
    guard.close()
    assert len(episode_lines(caplog)) == 1


def test_failed_stop_is_retried_when_pose_recovers_inside_the_limit():
    guard, clock, trace = guard_for([0.5, 0.5, float('nan'), 0.5, 0.5])
    attempts = []
    def stop():
        attempts.append(clock())
        if len(attempts) == 1:
            raise ConnectionError('temporary transport failure')
    guard.stop = stop
    try:
        for _ in range(3):
            guard.poll_once()
            clock.advance()
        await_off(guard)
        assert guard.snapshot().latched and len(attempts) == 1
        guard.poll_once()                       # recovered read must not abandon Stop
        assert len(attempts) == 2
        assert guard._episode.first_stop_ok is not None
        clock.advance()
        guard.poll_once()
        assert len(attempts) == 2               # verified OFF + ACK: no needless Stop
        assert not guard.snapshot().tracking_allowed
    finally:
        guard.close()


def test_tracking_off_in_flight_keeps_stopping_even_inside_the_boundary():
    guard, clock, trace = guard_for([0.71, 0.6, 0.6])
    release, entered = threading.Event(), threading.Event()
    def off():
        entered.set()
        release.wait(2)
        return True
    guard.disable_tracking = off
    try:
        guard.poll_once()
        assert entered.wait(1)
        clock.advance()
        guard.poll_once()
        assert trace.count('stop') == 2
        release.set()
        await_off(guard)
        clock.advance()
        guard.poll_once()
        assert trace.count('stop') == 2
    finally:
        release.set()
        guard.close()


def test_retry_after_failed_stop_still_respects_verified_inward_recovery():
    guard, clock, trace = guard_for([0.71, 0.6, 0.5])
    attempts = []
    def stop():
        attempts.append(clock())
        if len(attempts) == 1:
            raise ConnectionError('temporary transport failure')
    guard.stop = stop
    try:
        guard.poll_once()
        await_off(guard)
        assert guard.prepare_recovery(0.4)
        clock.advance()
        guard.poll_once()
        assert len(attempts) == 1
        clock.advance(2.1)
        guard.poll_once()
        assert len(attempts) == 2
    finally:
        guard.close()


def test_pose_loss_trace_names_cause_and_pre_trip_reads(caplog):
    caplog.set_level("INFO", logger="tapo_monitor.stopguard")
    clock = Clock()
    reads = iter([0.5, 0.5, None])

    def read():
        value = next(reads)
        clock.advance(0.2)
        if value is None:
            error = TimeoutError("slow")
            error.rpc_diag = {"attempts": 3, "errors": ["read timeout"] * 3, "elapsed_s": 0.8}
            raise error
        return value

    guard = StopGuard(read, lambda: None, lambda: True, right_limit=0.8, margin=0.1,
                      clock=clock, name="cam")
    for _ in range(3):
        guard.poll_once()
        clock.advance()
    await_off(guard)
    clock.advance(3)
    for _ in range(2):  # fail-closed pose never rearms; a fresh pair of reads does
        guard.poll_once()
    reads = iter([0.5, 0.5])
    guard._latched = True
    guard.poll_once()
    guard.poll_once()
    await_off(guard)
    assert guard.rearm()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "reason=pose_unavailable" in text and "cause=read timeout (attempts=3" in text
    trace = next(r.getMessage() for r in caplog.records if "stop guard trace" in r.getMessage())
    assert "cause=read timeout" in trace and "readfail=read timeout" in trace
    assert "pre=[" in trace and "0.5000/0.20s" in trace and "stop=ok" in trace
    assert "off=ok" in trace and "rearm" in trace
    guard.close()


def test_trace_is_bounded_and_records_blocked_rearm_once(caplog):
    caplog.set_level("INFO", logger="tapo_monitor.stopguard")
    guard, clock, _ = guard_for([0.5, 0.5] + [0.75] * 400)
    for _ in range(402):
        guard.poll_once()
        clock.advance(0.2)
        guard.rearm()
        guard.rearm()
    await_off(guard)
    episode = guard._episode
    assert episode is not None and len(episode.trace) <= 80
    assert [k for _, k, _ in episode.trace].count("rearm_blocked") <= 3
    guard.close()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "stop guard trace" in text and "cause=boundary" in text


def test_read_statistics_line_is_periodic(caplog):
    caplog.set_level("INFO", logger="tapo_monitor.stopguard")
    guard, clock, _ = guard_for([0.5] * 8)
    for _ in range(8):
        guard.poll_once()
        clock.advance(200)
    stats = [r.getMessage() for r in caplog.records if "stop guard stats" in r.getMessage()]
    assert stats and "reads=" in stats[0] and "failed=0" in stats[0]
    guard.close()


def recovery_guard(positions, **kwargs):
    clock = Clock()
    poses = iter(positions)
    homes = []
    guard = StopGuard(lambda: next(poses), lambda: None, lambda: True, right_limit=0.8,
                      margin=0.1, clock=clock, recover=lambda: homes.append(clock.now) or True,
                      **kwargs)
    return guard, clock, homes


def await_home(guard):
    thread = guard._recover_thread
    if thread is not None:
        thread.join(1)
        assert not thread.is_alive()


def test_verified_off_beyond_boundary_sends_home_without_waiting_for_control_pass(caplog):
    caplog.set_level("INFO", logger="tapo_monitor.stopguard")
    guard, clock, homes = recovery_guard([0.5, 0.5] + [0.85] * 8)
    for _ in range(3):
        guard.poll_once()
        clock.advance()
    await_off(guard)
    guard.poll_once()                       # OFF is verified now; still outside the band
    await_home(guard)
    assert len(homes) == 1
    clock.advance(1)
    guard.poll_once()                       # inside the retry interval: no second recall
    await_home(guard)
    assert len(homes) == 1
    clock.advance(5)
    guard.poll_once()
    await_home(guard)
    assert len(homes) == 2
    guard.close()
    assert any("home=ok" in r.getMessage() for r in caplog.records)


def test_pose_loss_at_a_safe_position_never_triggers_a_recall():
    guard, clock, homes = recovery_guard([0.5, 0.5, None, 0.5, 0.5])

    def read():
        value = next(guard_poses)
        if value is None:
            raise TimeoutError("no pose")
        return value

    guard_poses = iter([0.5, 0.5, None, 0.5, 0.5, 0.5])
    guard.read_position = read
    for _ in range(6):
        guard.poll_once()
        clock.advance(6)
        await_off(guard)
    await_home(guard)
    assert homes == []
    guard.close()


def test_recall_attempts_are_bounded_per_episode():
    guard, clock, homes = recovery_guard([0.5, 0.5] + [0.85] * 40, recover_attempts=2)
    for _ in range(42):
        guard.poll_once()
        await_off(guard)
        clock.advance(6)
        await_home(guard)
    assert len(homes) == 2
    guard.close()
