"""Fixes found in review: vehicle-absence identity, repair accounting, crop temp files,
parked_lenses races, unreadable clock and soak-report robustness."""

import importlib.util
import json
import os
import sys
import threading
import types
from pathlib import Path

import pytest

from tapo_monitor import capabilities, daemon, drift, twin
from tapo_monitor import config as cfg

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "soak"
spec = importlib.util.spec_from_file_location("soak_report", ROOT / "tools" / "soak_report.py")
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)

GOOD_FW = "1.3.2 Build 260820 Rel.65461n"
REFUSED = Exception('{"error_code": -40101}')


def _info(firmware):
    return {"device_info": {"basic_info": {
        "device_model": "C216", "hw_version": "1.0", "sw_version": firmware}}}


def _cam(**overrides):
    base = {"name": "c", "host": "203.0.113.10"}
    base.update(overrides)
    return cfg.load_config_from_dict({"cameras": [base]}).cameras[0]


# 1. identity source ----------------------------------------------------------------

def test_absent_vehicle_uses_fresh_identity_not_login_basicinfo():
    class Client:
        basicInfo = _info("1.3.3")      # stale/flat login identity: unverified

        def getBasicInfo(self):
            return _info(GOOD_FW)

        def getVehicleDetection(self):
            raise REFUSED

    assert capabilities.absent_vehicle_detection(Client()) is True


def test_absent_vehicle_ignores_stale_verified_login_identity():
    class Client:
        basicInfo = _info(GOOD_FW)      # firmware was upgraded after login

        def getBasicInfo(self):
            return _info("1.3.3")

        def getVehicleDetection(self):
            raise AssertionError("an unverified camera must not be probed")

    assert capabilities.absent_vehicle_detection(Client()) is False


def test_absent_vehicle_repairs_when_fresh_identity_is_unreadable():
    class Client:
        basicInfo = _info(GOOD_FW)

        def getBasicInfo(self):
            raise TimeoutError()

        def getVehicleDetection(self):
            raise AssertionError("unverified")

    assert capabilities.absent_vehicle_detection(Client()) is False


# 2. motion sensitivity failure is counted --------------------------------------------

def test_apply_plan_counts_refused_motion_sensitivity(monkeypatch):
    from tapo_monitor import tracking
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)

    class Cam:
        def executeFunction(self, *a, **k):
            pass

        def setMotionDetection(self, sensitivity=False):
            raise Exception("refused")

        def setPersonDetection(self, enabled, sensitivity=False):
            pass

        def setVehicleDetection(self, enabled, sensitivity=False):
            pass

        def setAutoTrackTarget(self, enabled):
            pass

        def getAutoTrackTarget(self):
            return {"enabled": "off"}

    failures = {}
    plan = daemon.plan_camera(_cam(), night=False, rain_active=False)
    daemon.apply_plan(Cam(), plan, repair_failures=failures)
    assert failures.get("motion_sensitivity") == 1


# 3. crop temp files ----------------------------------------------------------------

def _crop_cam():
    return _cam(crop_to_subject=True, scorer={"url": "http://x/score", "tiles": 2})


_RESULT = {"person": 0.9, "box": [100, 100, 200, 300], "w": 1000, "h": 800}


def test_failed_crop_leaves_no_partial_file(tmp_path):
    src = tmp_path / "frame.jpg"
    src.write_bytes(b"jpeg")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def broken(image, out_path, rect):
        open(out_path, "w").write("half")
        raise RuntimeError("ffmpeg died")

    out = daemon.crop_for_subject(_crop_cam(), str(src), str(out_dir),
                                  score_result=_RESULT, run_ffmpeg=broken)
    assert out == str(src)
    assert list(out_dir.iterdir()) == []


def test_empty_crop_output_is_removed(tmp_path):
    src = tmp_path / "frame.jpg"
    src.write_bytes(b"jpeg")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = daemon.crop_for_subject(_crop_cam(), str(src), str(out_dir),
                                  score_result=_RESULT, run_ffmpeg=lambda *a: None)
    assert out == str(src)
    assert list(out_dir.iterdir()) == []


def test_temp_names_are_unique_within_one_instant(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon._time, "time", lambda: 1234.5)
    paths = {daemon._unique_jpg(str(tmp_path), "crop_") for _ in range(5)}
    assert len(paths) == 5


def test_reduced_failure_removes_partial_and_no_literal_tmp(monkeypatch, tmp_path):
    src = tmp_path / "native.jpg"
    src.write_bytes(b"jpeg")
    monkeypatch.setattr(daemon.snapshot, "image_width", lambda p: 3840)
    monkeypatch.setattr(daemon.tempfile, "tempdir", str(tmp_path / "scratch"))
    (tmp_path / "scratch").mkdir()

    def broken(s, o):
        open(o, "w").write("half")
        raise RuntimeError("boom")

    assert daemon._reduced(str(src), None, broken) is None
    assert list((tmp_path / "scratch").iterdir()) == []
    seen = []
    daemon._reduced(str(src), None, lambda s, o: seen.append(o) or open(o, "w").write("x"))
    assert seen and os.path.dirname(seen[0]) == str(tmp_path / "scratch")
    os.unlink(seen[0])


# 4. parked_lenses under concurrent mutation --------------------------------------------

def test_parked_lenses_survives_concurrent_mutation():
    state = types.SimpleNamespace(
        twin_fleet={f"c{i}": {"privacy": "x"} for i in range(3000)},
        privacy_seen={f"c{i}": bool(i % 2) for i in range(3000)})
    stop = threading.Event()

    def churn():
        i = 0
        while not stop.is_set():
            i += 1
            state.privacy_seen[f"n{i % 50}"] = True
            state.privacy_seen.pop(f"n{(i + 25) % 50}", None)
            state.twin_fleet[f"n{i % 50}"] = {}
            state.twin_fleet.pop(f"n{(i + 25) % 50}", None)

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    thread = threading.Thread(target=churn)
    thread.start()
    try:
        for _ in range(300):
            daemon.parked_lenses(state)
    finally:
        stop.set()
        thread.join()
        sys.setswitchinterval(old)


# 5. unreadable clock --------------------------------------------------------------------

@pytest.mark.parametrize("value", [False, {"seconds": False}, {"offset": False}])
def test_unreadable_clock_is_unknown_not_synchronized(value):
    assert twin._clock_synced(value) is drift.UNKNOWN


def test_zero_offset_is_still_synchronized():
    assert twin._clock_synced(0) is True
    assert twin._clock_synced({"seconds": 0.0}) is True


# 6. soak report -----------------------------------------------------------------------

def _analyze(*lines):
    text = (FIXTURES / "host-a.raw").read_text().replace(
        "@@journal", "@@journal\n" + "\n".join(lines))
    return soak.analyze(text, 24)


def test_reordered_episode_fields_are_still_counted():
    rec = _analyze("INFO PTZ stop guard episode cam: stops_failed=2 off_failed=1 stops=5 "
                   "reads_failed=3 beyond_limit=False first_stop_ok=+0.1s off=verified+0.2s")
    assert (rec["stopguard_reads_failed"], rec["stopguard_stop_attempts"],
            rec["stopguard_stop_failures"], rec["stopguard_off_failures"]) == (3, 5, 2, 1)
    assert rec["stopguard_unparsed"] == 0


def test_episode_line_with_missing_counts_is_flagged_not_zeroed():
    rec = _analyze("INFO PTZ stop guard episode cam: reads_failed=3 stops=5 "
                   "beyond_limit=False first_stop_ok=+0.1s off=verified+0.2s")
    assert rec["stopguard_unparsed"] == 1
    assert rec["stopguard_episodes"] == 0
    assert any("could not be read" in f for f in rec["findings"])
    assert soak.host_status(rec) == "ATTENTION"


def test_failing_stop_is_one_finding():
    rec = _analyze(
        "WARNING PTZ stop guard Stop failed name=cam error=X",
        "INFO PTZ stop guard episode cam: reason=r duration=1s beyond_limit=False "
        "reads_failed=0 stops=2 stops_failed=2 first_stop_ok=none off=verified+1s off_failed=0")
    stop_findings = [f for f in rec["findings"] if "Stop" in f]
    assert len(stop_findings) == 1
    assert "Stopguard problem" in stop_findings[0] and "2 Stop RPC" in stop_findings[0]


def test_baseline_write_is_atomic(monkeypatch, tmp_path):
    baseline = tmp_path / "base.json"
    old = json.dumps({"version": soak.BASELINE_VERSION, "created": "x", "hosts": {}})
    baseline.write_text(old)
    raw = FIXTURES / "host-a.raw"
    original = os.replace

    def failing_replace(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(soak.os, "replace", failing_replace)
    with pytest.raises(OSError):
        soak.main([f"host-a={raw}", "--baseline", str(baseline), "--update-baseline"])
    assert baseline.read_text() == old          # never truncated in place
    assert [p.name for p in tmp_path.iterdir()] == ["base.json"]
    monkeypatch.setattr(soak.os, "replace", original)
