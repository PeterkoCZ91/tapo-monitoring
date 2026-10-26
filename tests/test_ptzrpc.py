"""Connection-loss replay with a clock and the actual observer transport seam."""
from types import SimpleNamespace as NS

import pytest
from requests.exceptions import ConnectTimeout, ReadTimeout

from tapo_monitor import ptzrpc


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def harness():
    transport = NS(operation_timeout=0.5)
    return NS(zeep_client=NS(transport=transport)), transport, Clock()


@pytest.mark.parametrize("failure", [ConnectTimeout, ReadTimeout])
def test_transient_failure_retries_within_the_original_deadline(failure):
    ptz, transport, clock = harness()
    timeouts = []

    def operation():
        connect, read = transport.operation_timeout
        timeouts.append((connect, read))
        if len(timeouts) == 1:
            clock.now += connect if failure is ConnectTimeout else read
            try:
                raise failure("temporary transport failure")
            except failure as exc:
                raise RuntimeError("ONVIF wrapper") from exc
        assert sum((connect, read)) <= .5 - clock.now
        clock.now += .02
        return "acknowledged"

    assert ptzrpc.call(ptz, operation, budget=.5, clock=clock) == "acknowledged"
    assert len(timeouts) == 2
    assert clock.now < .5
    assert transport.operation_timeout == .5


def test_persistent_failure_is_propagated_after_bounded_attempts():
    ptz, transport, clock = harness()
    calls = []

    def operation():
        connect, read = transport.operation_timeout
        assert connect + read <= .5 - clock.now
        calls.append(clock.now)
        clock.now += read
        raise ReadTimeout("still unavailable")

    with pytest.raises(ReadTimeout):
        ptzrpc.call(ptz, operation, budget=.5, clock=clock)
    assert len(calls) == 4
    assert clock.now <= .5
    assert transport.operation_timeout == .5


def test_response_after_the_deadline_is_rejected_and_not_retried():
    ptz, transport, clock = harness()
    calls = []

    def operation():
        calls.append(True)
        clock.now = .6
        return "late pose"

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        ptzrpc.call(ptz, operation, budget=.5, clock=clock)
    assert calls == [True]
    assert transport.operation_timeout == .5


def test_expired_budget_never_sends_a_second_request():
    ptz, transport, clock = harness()
    calls = []

    def operation():
        calls.append(True)
        clock.now = .6
        raise ConnectTimeout("already late")

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        ptzrpc.call(ptz, operation, budget=.5, clock=clock)
    assert calls == [True]
    assert transport.operation_timeout == .5


def test_camera_refusal_is_not_retried_or_silenced():
    ptz, transport, clock = harness()
    calls = []

    def operation():
        calls.append(True)
        raise ValueError("motor refused")

    with pytest.raises(ValueError, match="motor refused"):
        ptzrpc.call(ptz, operation, budget=.5, clock=clock)
    assert calls == [True]
    assert transport.operation_timeout == .5


def test_delayed_syn_ack_can_use_remaining_budget_after_fast_connect_failures():
    ptz, transport, clock = harness()
    calls = []

    def operation():
        connect, read = transport.operation_timeout
        calls.append((connect, read))
        if len(calls) < 4:
            clock.now += connect
            raise ConnectTimeout("listener not accepting yet")
        # A real delayed handshake arrived after the old 0.5s timeout. The final
        # connection must wait for it instead of abandoning unused time budget.
        assert connect >= .37
        clock.now += .37 + .02
        assert read >= .02
        return "acknowledged"

    assert ptzrpc.call(ptz, operation, clock=clock) == "acknowledged"
    assert len(calls) == 4
    assert clock.now < .8


def test_failure_carries_attempt_diagnostics_for_the_guard_trace():
    ptz, transport, clock = harness()

    def operation():
        clock.now += transport.operation_timeout[1]
        raise ReadTimeout("still unavailable")

    with pytest.raises(ReadTimeout) as caught:
        ptzrpc.call(ptz, operation, budget=.5, clock=clock)
    diag = caught.value.rpc_diag
    assert diag["attempts"] == 4
    assert diag["errors"] == ["read timeout"] * 4
    assert 0 < diag["elapsed_s"] <= .5


def _chained(outer, inner):
    try:
        try:
            raise inner
        except Exception as cause:
            raise outer from cause
    except Exception as exc:
        return exc


@pytest.mark.parametrize("exc, category", [
    (_chained(RuntimeError("onvif"), ConnectTimeout("192.0.2.1")), "connect timeout"),
    (_chained(RuntimeError("onvif"), ReadTimeout("192.0.2.1")), "read timeout"),
    (_chained(RuntimeError("onvif"), ConnectionRefusedError("192.0.2.1")), "connection refused"),
    (RuntimeError("Sender not authorized for 192.0.2.1"), "authentication failure"),
    (RuntimeError("weird 192.0.2.1"), "other"),
])
def test_diagnose_returns_fixed_category(exc, category):
    assert ptzrpc.diagnose(exc) == category


def test_stop_failure_log_carries_category_not_exception_text(caplog):
    from tapo_monitor.stopguard import StopGuard

    def stop():
        raise _chained(RuntimeError("leaked-text 192.0.2.1"), ConnectTimeout("192.0.2.1"))

    clock = Clock()
    poses = iter([0.5, 0.5, 0.86])
    guard = StopGuard(lambda: next(poses), stop, lambda: True, name="cam",
                      right_limit=0.8, margin=0.1, clock=clock)
    for _ in range(3):
        guard.poll_once()
        clock.now += 0.2
    thread = guard._disable_thread
    if thread is not None:
        thread.join(1)
    text = caplog.text
    assert "cause=connect timeout" in text
    assert "RuntimeError" not in text and "leaked-text" not in text and "192.0.2.1" not in text


def timing_op(ptz, transport, clock, latency):
    """Operation answering after `latency` s, or timing out at the transport read limit."""
    seen = []

    def operation():
        connect, read = transport.operation_timeout
        seen.append(connect + read)
        if latency <= connect + read:
            clock.now += latency
            return "ok"
        clock.now += connect + read
        try:
            raise ReadTimeout("slow")
        except ReadTimeout as exc:
            raise RuntimeError("ONVIF wrapper") from exc
    return operation, seen


def failing(ptz, transport, clock):
    return timing_op(ptz, transport, clock, 99)[0]


def test_budget_escalates_after_failure_and_restores_after_three_successes():
    clock = Clock()
    b = ptzrpc.AdaptiveBudget(clock=clock)
    assert b.budget() == 0.8
    b.record(True)
    assert b.budget() == 0.8
    b.record(False)
    assert b.budget() == 1.6
    b.record(True)
    b.record(True)
    assert b.budget() == 1.6
    b.record(False)          # a failure resets the success streak
    for _ in range(2):
        b.record(True)
    assert b.budget() == 1.6
    b.record(True)
    assert b.budget() == 0.8


def test_budget_restores_after_quiet_period():
    clock = Clock()
    b = ptzrpc.AdaptiveBudget(clock=clock)
    b.record(False)
    clock.now = 29.9
    assert b.budget() == 1.6
    clock.now = 30.0
    assert b.budget() == 0.8


def test_budget_is_capped_at_two_seconds():
    b = ptzrpc.AdaptiveBudget(escalated=10.0, clock=Clock())
    b.record(False)
    assert b.budget() == 2.0 == ptzrpc.AdaptiveBudget.CAP


def test_first_failure_uses_base_budget_then_late_stop_ack_succeeds():
    ptz, transport, clock = harness()
    b = ptzrpc.AdaptiveBudget(clock=clock)
    op, seen = timing_op(ptz, transport, clock, 1.2)
    with pytest.raises((RuntimeError, TimeoutError)):
        ptzrpc.call(ptz, op, adaptive=b, clock=clock)
    assert clock.now <= 0.8 + 1e-9          # first failure still used the base budget
    assert b.budget() == 1.6
    start = clock.now
    op, seen = timing_op(ptz, transport, clock, 1.2)
    assert ptzrpc.call(ptz, op, adaptive=b, clock=clock) == "ok"
    assert clock.now - start <= 1.6
    assert transport.operation_timeout == .5


def test_stop_at_1_2_s_fails_with_plain_0_8_budget():
    ptz, transport, clock = harness()
    op, _ = timing_op(ptz, transport, clock, 1.2)
    with pytest.raises((RuntimeError, TimeoutError)):
        ptzrpc.call(ptz, op, budget=0.8, clock=clock)


def test_escalated_failure_stays_within_the_stretched_budget():
    ptz, transport, clock = harness()
    b = ptzrpc.AdaptiveBudget(clock=clock)
    b.record(False)
    with pytest.raises((RuntimeError, TimeoutError)):
        ptzrpc.call(ptz, failing(ptz, transport, clock), adaptive=b, clock=clock)
    assert 1.5 <= clock.now <= 1.6 + 1e-9   # extra time is used, not idle
    assert b.budget() == 1.6


def test_reads_without_adaptive_keep_the_base_budget():
    ptz, transport, clock = harness()
    op, _ = timing_op(ptz, transport, clock, 1.2)
    with pytest.raises((RuntimeError, TimeoutError)):
        ptzrpc.call(ptz, op, clock=clock)
    assert clock.now <= 0.8 + 1e-9
