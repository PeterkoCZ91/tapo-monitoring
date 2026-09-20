import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import camera


def _no_sleep(_):
    pass


# ── ping_reachable ───────────────────────────────────────────────────────────

def test_ping_reachable_uses_a_bounded_shell_free_probe():
    calls = []
    class Result:
        returncode = 0
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()

    assert camera.ping_reachable("203.0.113.10", run=run) is True
    argv, kwargs = calls[0]
    assert argv == ["ping", "-n", "-c", "3", "-i", "0.3", "-W", "1", "203.0.113.10"]
    assert kwargs["timeout"] == 4
    assert kwargs["check"] is False


def test_ping_reachable_survives_a_single_lost_packet():
    # Measured on the production cameras: 2-4 % of echoes to the camera are lost while the
    # default gateway loses none from the same radio. A one-packet probe on a 60 s control
    # pass turned each lost packet into a warning AND a 60 s hole, because a camera that
    # fails the probe is dropped from the client map until the next pass.
    calls = []
    class Result:
        returncode = 0
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()

    camera.ping_reachable("203.0.113.10", run=run)
    argv, kwargs = calls[0]
    assert int(argv[argv.index("-c") + 1]) > 1
    # ping exits 0 when any echo is answered, so the deadline has to cover them all
    assert kwargs["timeout"] > int(argv[argv.index("-W") + 1])


def test_ping_reachable_returns_false_on_nonzero_or_command_error():
    class Failed:
        returncode = 1
    assert camera.ping_reachable("203.0.113.10", run=lambda *a, **k: Failed()) is False
    def missing(*args, **kwargs):
        raise OSError("ping missing")
    assert camera.ping_reachable("203.0.113.10", run=missing) is False


# ── connect (retry / lockout-aware) ──────────────────────────────────────────

def test_connect_succeeds_first_try():
    cam, err = camera.connect(lambda: "CAM", sleep=_no_sleep)
    assert cam == "CAM"
    assert err is None


def test_connect_succeeds_after_transient_failures():
    calls = {"n": 0}
    def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Invalid authentication data")
        return "CAM"
    cam, err = camera.connect(factory, retries=3, sleep=_no_sleep)
    assert cam == "CAM"
    assert err is None
    assert calls["n"] == 3


def test_connect_exhausts_and_returns_error():
    def factory():
        raise RuntimeError("down")
    cam, err = camera.connect(factory, retries=3, sleep=_no_sleep)
    assert cam is None
    assert isinstance(err, RuntimeError)


def test_reboot_calls_api_and_swallows_failure():
    class Client:
        def __init__(self, fail=False):
            self.fail = fail
            self.calls = 0
        def reboot(self):
            self.calls += 1
            if self.fail:
                raise RuntimeError("busy")
    ok = Client()
    assert camera.reboot(ok) is True
    assert ok.calls == 1
    assert camera.reboot(Client(fail=True)) is False


# ── whitelamp_on ──────────────────────────────────────────────────────────────

def test_whitelamp_on_true_when_status_is_1():
    class Client:
        def getWhitelampStatus(self):
            return {"status": 1, "rest_time": 240}
    assert camera.whitelamp_on(Client()) is True

def test_whitelamp_on_false_when_status_is_0():
    class Client:
        def getWhitelampStatus(self):
            return {"status": 0, "rest_time": 0}
    assert camera.whitelamp_on(Client()) is False

def test_whitelamp_on_none_when_unsupported():
    class Client:
        def getWhitelampStatus(self):
            raise Exception("UNSUPPORTED_METHOD")
    assert camera.whitelamp_on(Client()) is None

def test_whitelamp_on_none_on_malformed_response():
    class Client:
        def getWhitelampStatus(self):
            return "not a dict"
    assert camera.whitelamp_on(Client()) is None


# ── trigger_whitelamp ─────────────────────────────────────────────────────────

def test_trigger_whitelamp_status_0_calls_reverse():
    class Client:
        def __init__(self):
            self.reversed = False
        def getWhitelampStatus(self):
            return {"status": 0}
        def reverseWhitelampStatus(self):
            self.reversed = True
    client = Client()
    assert camera.trigger_whitelamp(client) is True
    assert client.reversed is True


def test_trigger_whitelamp_status_0_calls_set_force_state():
    class Client:
        def __init__(self):
            self.forced = None
        def getWhitelampStatus(self):
            return {"status": 0}
        def setForceWhitelampState(self, state):
            self.forced = state
    client = Client()
    assert camera.trigger_whitelamp(client) is True
    assert client.forced is True


def test_trigger_whitelamp_status_already_1_does_nothing():
    class Client:
        def __init__(self):
            self.reversed = False
        def getWhitelampStatus(self):
            return {"status": 1}
        def reverseWhitelampStatus(self):
            self.reversed = True
    client = Client()
    assert camera.trigger_whitelamp(client) is True
    assert client.reversed is False


def test_trigger_whitelamp_on_exception_returns_false_never_raises(monkeypatch):
    monkeypatch.setattr(camera._time, "sleep", lambda s: None)

    class Client:
        def getWhitelampStatus(self):
            raise RuntimeError("API failure")
    assert camera.trigger_whitelamp(Client()) is False


def test_trigger_whitelamp_unreadable_status_never_toggles_blind():
    class Client:
        reversed = False
        def getWhitelampStatus(self):
            return None
        def reverseWhitelampStatus(self):
            self.reversed = True
    client = Client()
    assert camera.trigger_whitelamp(client) is False
    assert client.reversed is False


def test_trigger_whitelamp_reverse_error_but_lamp_landed_is_success(monkeypatch):
    monkeypatch.setattr(camera._time, "sleep", lambda s: None)

    class Client:
        calls = 0
        def getWhitelampStatus(self):
            self.calls += 1
            return {"status": 0 if self.calls == 1 else 1}
        def reverseWhitelampStatus(self):
            raise RuntimeError("-40214")
    assert camera.trigger_whitelamp(Client()) is True


def test_trigger_whitelamp_retries_transient_status_read(monkeypatch):
    monkeypatch.setattr(camera._time, "sleep", lambda s: None)

    class Client:
        calls = 0
        reversed = False
        def getWhitelampStatus(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("-40214")
            return {"status": 0}
        def reverseWhitelampStatus(self):
            self.reversed = True
    client = Client()
    assert camera.trigger_whitelamp(client) is True
    assert client.reversed is True


# ── new_events / newest_start ────────────────────────────────────────────────

def test_new_events_filters_and_sorts():
    events = [
        {"start_time": 50}, {"start_time": 200}, {"start_time": 150},
    ]
    out = camera.new_events(events, last_seen=100)
    assert [e["start_time"] for e in out] == [150, 200]


def test_new_events_empty():
    assert camera.new_events([], last_seen=0) == []


def test_newest_start():
    assert camera.newest_start([{"start_time": 5}, {"start_time": 9}, {"start_time": 7}]) == 9


def test_newest_start_empty():
    assert camera.newest_start([]) is None


def test_trigger_whitelamp_with_force_time():
    configured_time = []
    class Client:
        def getWhitelampStatus(self):
            return {"status": 0}
        def reverseWhitelampStatus(self):
            pass
        def setWhitelampConfig(self, forceTime=None):
            configured_time.append(forceTime)

    client = Client()
    assert camera.trigger_whitelamp(client, force_time=30) is True
    assert configured_time == [30]


def test_set_lens_distortion_correction():
    calls = []
    class Client:
        def setLensDistortionCorrection(self, enabled):
            calls.append(enabled)

    client = Client()
    assert camera.set_lens_distortion_correction(client, True) is True
    assert calls == [True]


def test_set_tamper_detection():
    calls = []
    class Client:
        def setTamperDetection(self, enabled, sensitivity):
            calls.append((enabled, sensitivity))

    client = Client()
    assert camera.set_tamper_detection(client, True, "high") is True
    assert calls == [(True, "high")]


def test_set_osd_safe():
    executed = []
    class Client:
        def executeFunction(self, method, payload):
            executed.append((method, payload))

    client = Client()
    assert camera.set_osd_safe(client, label="FRONT", date_enabled=True, week_enabled=True) is True
    assert len(executed) == 1
    method, payload = executed[0]
    assert method == "setOsd"
    assert payload["OSD"]["label_info_1"]["text"] == "FRONT"
    assert payload["OSD"]["date"]["enabled"] == "on"
    assert payload["OSD"]["week"]["enabled"] == "on"
