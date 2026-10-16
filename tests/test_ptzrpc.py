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
