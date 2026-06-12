import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import tracking


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
