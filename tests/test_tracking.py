import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

import pytest

from tapo_monitor import daemon, tracking

# ── decide_tracking ──────────────────────────────────────────────────────────

def test_static_never_tracks():
    assert tracking.decide_tracking("static", night=True, rain_active=False, strategy="none") == (False, False)

def test_tracking_camera_day_off():
    assert tracking.decide_tracking("tracking", night=False, rain_active=False, strategy="none") == (False, False)

def test_tracking_camera_night_on():
    assert tracking.decide_tracking("tracking", night=True, rain_active=False, strategy="none") == (True, False)

def test_night_rain_disable_strategy_parks():
    assert tracking.decide_tracking("tracking", night=True, rain_active=True, strategy="disable_tracking") == (False, True)

def test_night_rain_lower_sensitivity_keeps_tracking():
    # lowering sensitivity does not stop tracking
    assert tracking.decide_tracking("tracking", night=True, rain_active=True, strategy="lower_sensitivity") == (True, False)

def test_storm_park_parks_even_under_lower_sensitivity():
    # storm_park parks the PTZ in the rain regardless of the sensitivity strategy, so a
    # tracking camera stops swinging after raindrops/branches even while it still lowers
    # motion sensitivity.
    assert tracking.decide_tracking(
        "tracking", night=True, rain_active=True, strategy="lower_sensitivity",
        storm_park=True) == (False, True)

def test_storm_park_no_effect_when_dry():
    assert tracking.decide_tracking(
        "tracking", night=True, rain_active=False, strategy="lower_sensitivity",
        storm_park=True) == (True, False)


# ── decide_motion_sensitivity ────────────────────────────────────────────────

def test_sensitivity_lowered_in_rain():
    assert tracking.decide_motion_sensitivity(True, 60, 20, strategy="lower_sensitivity") == 20

def test_sensitivity_normal_when_dry():
    assert tracking.decide_motion_sensitivity(False, 60, 20, strategy="lower_sensitivity") == 60

def test_sensitivity_unchanged_under_other_strategy():
    assert tracking.decide_motion_sensitivity(True, 60, 20, strategy="disable_tracking") == 60


# ── smarttrack_payload ───────────────────────────────────────────────────────

def test_smarttrack_payload_people_only():
    info = tracking.smarttrack_payload(["people"])["smart_track"]["smart_track_info"]
    assert info["people_enabled"] == "on"
    assert info["vehicle_enabled"] == "off"
    assert info["pet_enabled"] == "off"

def test_smarttrack_payload_multiple():
    info = tracking.smarttrack_payload(["people", "vehicle"])["smart_track"]["smart_track_info"]
    assert info["people_enabled"] == "on"
    assert info["vehicle_enabled"] == "on"


# ── ensure_autotrack (firmware-safe, with fake camera) ───────────────────────

class _FakeCam:
    def __init__(self, accept=True, flips_after=0):
        self.state = False
        self.accept = accept
        self.set_calls = 0
        self.flips_after = flips_after  # succeed only from this set-call onward
    def setAutoTrackTarget(self, enabled):
        self.set_calls += 1
        if self.accept and self.set_calls >= self.flips_after:
            self.state = enabled
    def getAutoTrackTarget(self):
        return {"enabled": "on" if self.state else "off"}


def _no_sleep(_):
    pass


def test_ensure_autotrack_succeeds_first_try():
    cam = _FakeCam(accept=True, flips_after=1)
    assert tracking.ensure_autotrack(cam, True, sleep=_no_sleep) is True
    assert cam.state is True

def test_ensure_autotrack_succeeds_after_retry():
    cam = _FakeCam(accept=True, flips_after=2)  # first set ignored, retry works
    assert tracking.ensure_autotrack(cam, True, sleep=_no_sleep) is True

def test_ensure_autotrack_fails_when_camera_never_accepts():
    cam = _FakeCam(accept=False)
    assert tracking.ensure_autotrack(cam, True, sleep=_no_sleep) is False


# ── back_time (firmware dwell after a track) ─────────────────────────────────

class _TrackCfgCam:
    """Camera that records executeFunction payloads and serves a target_track readback."""

    def __init__(self, accept=True, back_time="30"):
        self.state = False
        self.back_time = back_time
        self.accept = accept
        self.calls = []

    def setAutoTrackTarget(self, enabled):
        self.calls.append(("setAutoTrackTarget", {"enabled": enabled}))
        self.state = enabled

    def executeFunction(self, method, params):
        self.calls.append((method, params))
        if not self.accept:
            raise RuntimeError("refused")
        info = params["target_track"]["target_track_info"]
        if "enabled" in info:
            self.state = info["enabled"] == "on"
        if "back_time" in info:
            self.back_time = info["back_time"]

    def getAutoTrackTarget(self):
        return {"enabled": "on" if self.state else "off", "back_time": self.back_time}


def test_back_time_rides_along_in_the_autotrack_call():
    # The dwell must be written by the SAME request that asserts the master switch.
    # setSmartTrackConfig silently clears auto-track, so apply_smarttrack -> assert is a
    # gap nothing may enter; a separate setTargetTrackConfig for back_time would sit
    # exactly there and re-open the ordering bug this module exists to prevent.
    cam = _TrackCfgCam()

    assert tracking.set_autotrack(cam, True, back_time=180) is True

    assert len(cam.calls) == 1
    method, params = cam.calls[0]
    assert method == "setTargetTrackConfig"
    assert params["target_track"]["target_track_info"] == {"enabled": "on", "back_time": "180"}


def test_back_time_absent_leaves_the_call_shape_untouched():
    # Cameras without a configured dwell keep the exact path they have always taken.
    cam = _TrackCfgCam()

    assert tracking.set_autotrack(cam, True) is True

    assert cam.calls == [("setAutoTrackTarget", {"enabled": True})]


def test_a_refused_back_time_still_turns_tracking_on():
    # The dwell is a comfort; tracking is not. A firmware that rejects the combined
    # payload must still end the pass with auto-track asserted.
    cam = _TrackCfgCam(accept=False)

    assert tracking.set_autotrack(cam, True, back_time=180) is True

    assert cam.calls[0][0] == "setTargetTrackConfig"
    assert cam.calls[-1] == ("setAutoTrackTarget", {"enabled": True})
    assert cam.state is True


def test_verify_autotrack_reports_a_back_time_the_camera_did_not_take(caplog):
    # A camera that quietly keeps back_time=30 looks identical to one that took 180 —
    # and the whole change is worthless in that state. Say so rather than assume.
    cam = _TrackCfgCam(back_time="30")
    cam.state = True

    with caplog.at_level("WARNING"):
        assert tracking.verify_autotrack(cam, True, back_time=180) is True

    assert "back_time" in caplog.text


def test_verify_autotrack_is_quiet_when_the_dwell_landed():
    cam = _TrackCfgCam(back_time="180")
    cam.state = True

    assert tracking.verify_autotrack(cam, True, back_time=180) is True


def test_ensure_autotrack_passes_the_dwell_through():
    cam = _TrackCfgCam()

    assert tracking.ensure_autotrack(cam, True, sleep=_no_sleep, back_time=180) is True

    assert cam.back_time == "180"


class _NsCam(_TrackCfgCam):
    """Camera that only knows the auto_track_target namespace (C560WS fw 1.1.10)."""

    def executeFunction(self, method, params):
        self.calls.append((method, params))
        if method != "setAutoTrackTarget":
            raise RuntimeError("refused")
        info = params["auto_track_target"]
        self.state = info["enabled"] == "on"
        if "back_time" in info:
            self.back_time = info["back_time"]


def test_back_time_falls_back_to_auto_track_target_namespace():
    cam = _NsCam()
    assert tracking.set_autotrack(cam, True, back_time=180) is True
    assert cam.calls[-1][0] == "setAutoTrackTarget"
    assert cam.back_time == "180" and cam.state is True


def test_back_time_already_set_skips_the_combined_call(caplog):
    cam = _TrackCfgCam(accept=False, back_time="180")
    with caplog.at_level("WARNING"):
        assert tracking.set_autotrack(cam, True, back_time=180) is True
    assert cam.calls == [("setAutoTrackTarget", {"enabled": True})]
    assert "refused" not in caplog.text


def test_refused_back_time_warns_only_once_per_camera(caplog):
    tracking._BACK_TIME_WARNED.clear()
    cam = _TrackCfgCam(accept=False)
    with caplog.at_level("WARNING"):
        tracking.set_autotrack(cam, True, back_time=180)
        tracking.set_autotrack(cam, True, back_time=180)
    assert caplog.text.count("refused the combined") == 1


class _GuardCamera:
    """Firmware model: a SmartTrack write resets the auto-track master switch."""
    def __init__(self):
        self._rotation_stop_control = SimpleNamespace(allowed=lambda: True)
        self.values = {
            'motion': {'digital_sensitivity': '60'}, 'person': {'enabled': 'on'},
            'vehicle': {'enabled': 'off'}, 'lamp': {'wtl_force_time': '60'},
            'night': 'on', 'smart': tracking.smarttrack_payload(('people',)),
            'auto': {'enabled': 'on'},
        }
        self.writes = []
        self.broken = None

    def read(self, key):
        if key == self.broken:
            raise ConnectionError('unreadable setting')
        return self.values[key]

    def getMotionDetection(self):
        return self.read('motion')

    def getPersonDetection(self):
        return self.read('person')

    def getVehicleDetection(self):
        return self.read('vehicle')

    def getWhitelampConfig(self):
        return self.read('lamp')

    def getDayNightMode(self):
        return self.read('night')

    def getSmartTrackConfig(self):
        return self.read('smart')

    def getAutoTrackTarget(self):
        return self.read('auto')

    def setMotionDetection(self, **kwargs):
        self.writes.append('motion')
        self.values['motion'] = {'digital_sensitivity': str(kwargs['sensitivity'])}

    def setPersonDetection(self, enabled, **kwargs):
        self.writes.append('person')
        self.values['person'] = {'enabled': 'on' if enabled else 'off', **kwargs}

    def setVehicleDetection(self, enabled):
        self.writes.append('vehicle')
        self.values['vehicle'] = {'enabled': 'on' if enabled else 'off'}

    def setWhitelampConfig(self, **kwargs):
        self.writes.append('lamp')
        self.values['lamp'] = {'wtl_force_time': str(kwargs['forceTime'])}

    def setDayNightMode(self, mode):
        self.writes.append('night')
        self.values['night'] = mode

    def executeFunction(self, method, params):
        assert method == 'setSmartTrackConfig'
        self.writes.append('smart')
        self.values['smart'] = params
        self.values['auto'] = {'enabled': 'off'}

    def setAutoTrackTarget(self, enabled):
        self.writes.append('auto')
        self.values['auto'] = {'enabled': 'on' if enabled else 'off'}


def _guard_plan():
    return daemon.CameraPlan(True, False, 60, ('people',), None,
                             night_vision='on', whitelamp_force_time=60)


def test_stable_endstop_camera_is_read_without_rewriting_firmware(monkeypatch):
    cam = _GuardCamera()
    monkeypatch.setattr(tracking._time, 'sleep', lambda _: None)
    assert daemon.apply_plan(cam, _guard_plan())
    assert cam.writes == []
    assert cam.values['auto']['enabled'] == 'on'


@pytest.mark.parametrize('setting', ['motion', 'person', 'vehicle', 'lamp', 'night', 'smart', 'auto'])
def test_endstop_camera_repairs_real_drift_and_reasserts_auto_last(monkeypatch, setting):
    cam = _GuardCamera()
    cam.values[setting] = {} if isinstance(cam.values[setting], dict) else 'off'
    monkeypatch.setattr(tracking._time, 'sleep', lambda _: None)
    assert daemon.apply_plan(cam, _guard_plan())
    assert cam.writes == ([setting, 'auto'] if setting == 'smart' else [setting])
    assert cam.values['auto']['enabled'] == 'on'


@pytest.mark.parametrize('setting', ['motion', 'person', 'vehicle', 'lamp', 'night', 'smart'])
def test_unreadable_settings_keep_the_original_repair_path(monkeypatch, setting):
    cam = _GuardCamera()
    cam.broken = setting
    monkeypatch.setattr(tracking._time, 'sleep', lambda _: None)
    assert daemon.apply_plan(cam, _guard_plan())
    assert setting in cam.writes


def test_a_partial_detection_read_cannot_skip_the_requested_sensitivity(monkeypatch):
    cam = _GuardCamera()
    plan = _guard_plan()
    plan.person_sensitivity = 40
    monkeypatch.setattr(tracking._time, 'sleep', lambda _: None)
    assert daemon.apply_plan(cam, plan)
    assert cam.writes == ['person']


def test_guard_trip_during_the_read_does_not_leave_tracking_enabled():
    cam = _GuardCamera()
    allowed = [True]
    cam._rotation_stop_control.allowed = lambda: allowed[0]
    def reading():
        allowed[0] = False
        return cam.values['auto']
    cam.getAutoTrackTarget = reading
    assert tracking.ensure_autotrack(cam, True, sleep=lambda _: None)
    assert cam.writes == ['auto']
    assert cam.values['auto']['enabled'] == 'off'


def test_cameras_without_an_endstop_keep_their_existing_write_policy(monkeypatch):
    cam = _GuardCamera()
    cam._rotation_stop_control = None
    monkeypatch.setattr(tracking._time, 'sleep', lambda _: None)
    assert daemon.apply_plan(cam, _guard_plan())
    assert cam.writes == ['motion', 'night', 'person', 'vehicle', 'lamp', 'smart', 'auto']
