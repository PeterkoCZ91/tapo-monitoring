"""Per-lens repair of tamper, day/night, night-vision mode and white lamp (dual-lens)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tapo_monitor import camera as cam_mod
from tapo_monitor import daemon

LENSES = {"channels": (1, 2),
          "motion": {c: {"digital_sensitivity": "50"} for c in (1, 2)},
          "person": {c: {"enabled": "on"} for c in (1, 2)},
          "vehicle": {c: {"enabled": "off"} for c in (1, 2)}}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    from tapo_monitor import tracking
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)


class LensCam:
    """Fake dual-lens client: per-lens readings and a record of every write."""

    def __init__(self, tamper=None, day_night=None, nvm=None, lamp=None, fail_reads=()):
        self.tamper = tamper or {}
        self.day_night = day_night or {}
        self.nvm = nvm or {}
        self.lamp = lamp or {}
        self.fail_reads = set(fail_reads)
        self.writes = []
        self.reads = []
        self.calls = []

    def _read(self, kind, data, chn_id):
        self.reads.append((kind, chn_id))
        if kind in self.fail_reads:
            raise RuntimeError("refused")
        return {str(c): data[c] for c in chn_id if c in data}

    def getTamperDetection(self, chn_id=None):
        return self._read("tamper", self.tamper, chn_id)

    def getWhitelampConfig(self, chn_id=None):
        return self._read("lamp", self.lamp, chn_id)

    def getNightVisionModeConfig(self, chn_id=None):
        return self._read("nvm", {c: {"night_vision_mode": v} for c, v in self.nvm.items()},
                          chn_id)

    def executeFunction(self, method, params):
        if method != "getLightFrequencyInfo":
            return {}
        self.calls.append((method, params))
        # The string form ignores chn_id: only the list form may be used.
        assert params["image"]["name"] == ["common"]
        return {"image": {"common_chn": {str(c): {"inf_type": v}
                                         for c, v in self.day_night.items()}}}

    def setTamperDetection(self, enabled, sensitivity=False, chn_id=None):
        self.writes.append(("tamper", enabled, sensitivity, chn_id))

    def setDayNightMode(self, mode, chn_id=None):
        self.writes.append(("day_night", mode, chn_id))

    def setNightVisionModeConfig(self, mode, chn_id=None):
        self.writes.append(("nvm", mode, chn_id))

    def setWhitelampConfig(self, forceTime=False, intensityLevel=False, chn_id=None):
        self.writes.append(("lamp", forceTime, intensityLevel, chn_id))


def _plan(**kw):
    base = dict(autotrack_on=False, rain_parked=False, motion_sensitivity=50,
                smarttrack=(), preset=None)
    base.update(kw)
    return daemon.CameraPlan(**base)


def _apply(cam, plan, failures=None):
    daemon.apply_plan(cam, plan, repair_failures=failures, camera="c", lenses=LENSES)


def test_tamper_writes_only_the_drifted_lens():
    cam = LensCam(tamper={1: {"enabled": "on", "sensitivity": "high"},
                          2: {"enabled": "off", "sensitivity": "high"}})
    _apply(cam, _plan(tamper_detection=True, tamper_sensitivity="high"))
    assert [w for w in cam.writes if w[0] == "tamper"] == [("tamper", True, "high", [2])]


def test_tamper_matching_both_lenses_writes_nothing_and_normal_is_medium():
    cam = LensCam(tamper={c: {"enabled": "on", "sensitivity": "medium"} for c in (1, 2)})
    _apply(cam, _plan(tamper_detection=True))
    assert cam.writes == []


def test_tamper_unreadable_writes_every_lens_and_counts_refusal():
    cam = LensCam(fail_reads={"tamper"})
    failures = {}
    attempts = []

    def refuse(enabled, sensitivity=False, chn_id=None):
        attempts.append((enabled, sensitivity, chn_id))
        raise RuntimeError("no")
    cam.setTamperDetection = refuse
    _apply(cam, _plan(tamper_detection=True), failures)
    # one blind write that names both lenses, exactly once, refused and counted once
    assert attempts == [(True, "normal", [1, 2])]
    assert failures == {"tamper_detection": 1}
    # the unread lenses are not hammered again within the hold-off
    _apply(cam, _plan(tamper_detection=True), failures)
    assert attempts == [(True, "normal", [1, 2])]
    assert failures == {"tamper_detection": 1}


def test_tamper_unreadable_accepted_write_counts_no_refusal():
    cam = LensCam(fail_reads={"tamper"})
    failures = {}
    _apply(cam, _plan(tamper_detection=True), failures)
    assert [w for w in cam.writes if w[0] == "tamper"] == [("tamper", True, "normal", [1, 2])]
    assert failures == {}


def test_day_night_reads_list_form_and_writes_drifted_lens():
    cam = LensCam(day_night={1: "on", 2: "off"})
    _apply(cam, _plan(night_vision="on"))
    assert [w for w in cam.writes if w[0] == "day_night"] == [("day_night", "on", [2])]
    assert cam.calls and cam.calls[0][1]["image"]["chn_id"] == [1, 2]


def test_day_night_matching_writes_nothing():
    cam = LensCam(day_night={1: "auto", 2: "auto"})
    _apply(cam, _plan(night_vision="auto"))
    assert cam.writes == []


def test_night_vision_mode_per_lens():
    cam = LensCam(nvm={1: "md_night_vision", 2: "inf_night_vision"})
    _apply(cam, _plan(night_vision_mode="md_night_vision"))
    assert [w for w in cam.writes if w[0] == "nvm"] == [("nvm", "md_night_vision", [2])]


def test_whitelamp_intensity_needs_chn_id_and_skips_matching_lens():
    cam = LensCam(lamp={1: {"wtl_intensity_level": "5", "wtl_force_time": "300"},
                        2: {"wtl_intensity_level": "3", "wtl_force_time": "300"}})
    _apply(cam, _plan(whitelamp_intensity=5))
    assert [w for w in cam.writes if w[0] == "lamp"] == [("lamp", False, 5, [2])]


def test_unmanaged_settings_are_not_touched():
    cam = LensCam()
    _apply(cam, _plan())
    assert cam.writes == [] and cam.reads == [] and cam.calls == []


def test_read_lens_setting_never_raises_on_bad_shapes():
    class Bad:
        def getTamperDetection(self, chn_id=None):
            return "garbage"
    assert cam_mod.read_lens_setting(Bad(), "tamper", [1, 2]) == {}


def test_unreadable_lens_is_written_once_per_ten_minutes(monkeypatch):
    cam = LensCam(fail_reads={"tamper"})
    writes = []
    clock = [1000.0]
    monkeypatch.setattr(daemon._time, "monotonic", lambda: clock[0])
    lenses = {"channels": (1, 2)}
    for _ in range(3):
        daemon._repair_lens_setting(cam, lenses, "tamper_detection", lambda v: True,
                                    lambda chn: writes.append(list(chn)), {}, "c")
    assert len(writes) == 1
    clock[0] += 601
    daemon._repair_lens_setting(cam, lenses, "tamper_detection", lambda v: True,
                                lambda chn: writes.append(list(chn)), {}, "c")
    assert len(writes) == 2


def test_absent_vehicle_identity_is_cached_per_client():
    from tapo_monitor import capabilities

    class C:
        calls = 0

        def getBasicInfo(self):
            C.calls += 1
            return {"device_info": {"basic_info": {"device_model": "X"}}}

        def getVehicleDetection(self):
            return {}
    c = C()
    for _ in range(4):
        capabilities.absent_vehicle_detection(c)
    assert C.calls == 1
