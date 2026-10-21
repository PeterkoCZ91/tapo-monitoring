"""Authentication handling in camera.py, handoff lease state, connect backoff."""

import threading
import time

import pytest

from tapo_monitor import camera
from tapo_monitor.handoff import HandoffManager


class _Code(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def test_connect_stops_on_authentication_error():
    calls = []

    def factory():
        calls.append(1)
        raise Exception("Invalid authentication data")

    sleeps = []
    client, err = camera.connect(factory, sleep=sleeps.append)
    assert client is None and "Invalid authentication" in str(err)
    assert len(calls) == 1 and sleeps == []


def test_connect_still_retries_other_errors():
    calls = []

    def factory():
        calls.append(1)
        raise OSError("timeout")

    camera.connect(factory, sleep=lambda _s: None)
    assert len(calls) == 3


def test_suspension_seconds_available_from_constructor_error():
    err = Exception("Temporary Suspension: Try again in 600 seconds")
    assert camera.is_authentication_error(err)
    assert camera.authentication_retry_after(err) == 600


def test_missing_params_40211_is_not_authentication():
    err = Exception('Error: MISSING_NECESSARY_PARAMS, Response: {"error_code": -40211}')
    assert not camera.is_authentication_error(err)
    assert not camera.is_authentication_error(_Code("x", code=-40211))


@pytest.mark.parametrize("code", [-40404, -40408, -40418])
def test_blocked_and_tpap_auth_codes(code):
    assert camera.is_authentication_error(_Code("x", code=code))
    assert camera.is_authentication_error(Exception(f'Response: {{"error_code": {code}}}'))


def test_unrelated_code_not_authentication():
    assert not camera.is_authentication_error(Exception('{"error_code": -404040}'))


def test_observer_reports_nested_retry_once():
    class Client:
        def performRequest(self, data, *a, **k):
            if k.get("retry"):
                raise Exception("Invalid authentication data")
            return self.performRequest(data, retry=True)

    client = Client()
    failures, successes = [], []
    camera.observe_client_requests(
        client, on_auth_failure=lambda c, e: failures.append(e),
        on_success=lambda c: successes.append(1))
    with pytest.raises(Exception, match="authentication"):
        client.performRequest({})
    assert len(failures) == 1 and successes == []


def test_whitelamp_toggle_is_serialized():
    state = {"on": False}
    toggles = []

    class Client:
        host = "10.0.0.1"

        def getWhitelampStatus(self):
            snapshot = state["on"]
            time.sleep(0.05)
            return {"status": 1 if snapshot else 0}

        def reverseWhitelampStatus(self):
            toggles.append(1)
            state["on"] = not state["on"]

    client = Client()
    threads = [threading.Thread(target=camera.trigger_whitelamp, args=(client,))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(toggles) == 1 and state["on"] is True


def test_handoff_begin_keeps_original_previous_policy():
    mgr = HandoffManager()
    kw = dict(group="g", source_camera="a", target_camera="b", preset="1", duration=60)
    mgr.begin(now=0, previous_policy="orig", **kw)
    lease = mgr.begin(now=10, previous_policy="temp", **kw)
    assert lease.previous_policy == "orig"
    assert mgr.active("g", 20).previous_policy == "orig"


def test_constructor_suspension_extends_connect_backoff(monkeypatch):
    from tapo_monitor import config as cfg
    from tapo_monitor import daemon

    cam = cfg.load_config_from_dict(
        {"cameras": [{"name": "c", "host": "203.0.113.10"}]}).cameras[0]
    monkeypatch.setattr(camera, "ping_reachable", lambda host: True)
    monkeypatch.setattr(camera, "tapo_factory", lambda *a: (lambda: None))
    monkeypatch.setattr(
        camera, "connect",
        lambda f: (None, Exception("Temporary Suspension: Try again in 900 seconds")))
    monkeypatch.setattr(daemon, "resolve_camera_credentials",
                        lambda c: ("u", "p", None))
    state = daemon.MonitorState()
    client, err = daemon._connect_camera({}, state, now=100)(cam)
    assert client is None
    assert state.connect_backoff_until["c"] == 1000


def test_login_credentials_40209_is_authentication():
    assert camera.is_authentication_error(_Code("x", code=-40209))
    assert camera.is_authentication_error(Exception('{"error_code": -40209}'))


def test_observer_depth_is_per_thread():
    gate = threading.Event()
    entered = threading.Event()
    failures = []

    class Client:
        def performRequest(self, data, *a, **k):
            if data == "slow":
                entered.set()
                gate.wait(5)
            raise Exception("Invalid authentication data")

    client = Client()
    camera.observe_client_requests(
        client, on_auth_failure=lambda c, e: failures.append(1), on_success=lambda c: None)

    def slow():
        with pytest.raises(Exception, match="authentication"):
            client.performRequest("slow")

    t = threading.Thread(target=slow)
    t.start()
    entered.wait(5)
    with pytest.raises(Exception, match="authentication"):
        client.performRequest("fast")
    # the first thread is still inside its call: a shared depth counter would be at 2 here
    # and swallow this report
    assert t.is_alive() and not gate.is_set()
    assert failures == [1]
    gate.set()
    t.join(5)
    assert not t.is_alive()
    assert failures == [1, 1]


def test_nested_request_failure_is_reported_once():
    failures = []

    class Client:
        def performRequest(self, data, *a, **k):
            if data == "outer":
                return self.performRequest("inner")
            raise Exception("Invalid authentication data")

    client = Client()
    camera.observe_client_requests(
        client, on_auth_failure=lambda c, e: failures.append(1), on_success=lambda c: None)
    with pytest.raises(Exception, match="authentication"):
        client.performRequest("outer")
    assert failures == [1]


def test_whitelamp_lock_timeout_skips_toggle(monkeypatch):
    class Client:
        host = "10.0.0.9"

        def getWhitelampStatus(self):
            raise AssertionError("must not be read")

    client = Client()
    monkeypatch.setattr(camera, "LAMP_LOCK_TIMEOUT", 0.05)
    lock = camera._lamp_lock(client)
    with lock:
        assert camera.trigger_whitelamp(client) is False


def test_whitelamp_lock_prefers_camera_name_over_host():
    class A:
        host = "10.0.0.1"
        _tapo_camera_name = "cam"

    class B:
        host = "10.0.0.2"
        _tapo_camera_name = "cam"

    assert camera._lamp_lock(A()) is camera._lamp_lock(B())


def test_per_lens_single_channel_answer_keyed_by_other_channel_is_unknown():
    assert camera._per_lens({"1": {"enabled": "on"}}, [0]) == {}
    assert camera._per_lens({"enabled": "on"}, [0]) == {0: {"enabled": "on"}}
