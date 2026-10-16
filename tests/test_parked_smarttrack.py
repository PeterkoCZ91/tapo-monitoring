"""Parked views must disable both native tracking switches without muting detection."""
import pytest

from tapo_monitor import config, daemon, tracking


@pytest.mark.parametrize("role,rain", [("tracking", False), ("static", False),
                                      ("tracking", True)])
def test_parked_control_clears_smarttrack_and_restores_it_at_night(monkeypatch, role, rain):
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)

    class Camera:
        def __init__(self):
            self.smart = {}
            self.auto = False
            self.person = False
            self.order = []

        def setMotionDetection(self, **kwargs):
            pass

        def setPersonDetection(self, enabled, **kwargs):
            self.person = enabled

        def setVehicleDetection(self, enabled):
            pass

        def executeFunction(self, name, payload):
            assert name == "setSmartTrackConfig"
            self.smart = payload["smart_track"]["smart_track_info"]
            self.order.append("smart")

        def setAutoTrackTarget(self, enabled):
            self.auto = enabled
            self.order.append("auto")

        def getAutoTrackTarget(self):
            return {"enabled": "on" if self.auto else "off"}

    cfg = config.load_config_from_dict({"cameras": [{
        "name": "test-camera", "host": "192.168.1.20", "role": role,
        "tracking": {"day_preset": None, "night_preset": None, "smarttrack": ["people"]},
        "weather": {"strategy": "disable_tracking"},
    }]}).cameras[0]
    cam = Camera()
    # Seed the state left by a previous tracking night, including the separate filter.
    cam.smart = tracking.smarttrack_payload(["people"])["smart_track"]["smart_track_info"]
    cam.auto = True
    parked = daemon.plan_camera(cfg, night=rain or role == "static", rain_active=rain)
    assert not parked.autotrack_on
    assert daemon.apply_plan(cam, parked)
    assert set(cam.smart.values()) == {"off"}
    assert cam.auto is False and cam.person is True
    assert cam.order[-2:] == ["smart", "auto"]
    if role == "tracking":
        assert daemon.apply_plan(cam, daemon.plan_camera(cfg, night=True, rain_active=False))
        assert cam.smart["people_enabled"] == "on"
        assert cam.auto is True and cam.person is True
        assert cam.order[-2:] == ["smart", "auto"]
