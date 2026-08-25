import os

import pytest

from tapo_monitor import config, reliability


def test_normalize_repairs_is_deterministic_and_rejects_unknown():
    assert reliability.normalize_repairs(["smarttrack", "smarttrack", "person_detection"]) == (
        "smarttrack", "person_detection"
    )
    with pytest.raises(ValueError, match="allowed_repairs"):
        reliability.normalize_repairs(["firmware"])


def test_latency_snapshot_aggregates_only_safe_numbers():
    values = {}
    reliability.observe_latency(values, "scorer", 0.25)
    reliability.observe_latency(values, "scorer", 0.75)
    reliability.observe_latency(values, "scorer", -1)
    assert reliability.latency_snapshot(values) == {
        "scorer": {"count": 2, "total_s": 1.0, "max_s": 0.75, "avg_s": 0.5}
    }


def test_recorder_health_reports_continuity(tmp_path):
    camera_dir = tmp_path / "camera"
    camera_dir.mkdir()
    paths = [camera_dir / "a.mkv", camera_dir / "b.mkv"]
    for path in paths:
        path.write_bytes(b"")
        os.utime(path, (1000, 1000))

    starts = {str(paths[0]): 0, str(paths[1]): 900}
    result = reliability.recorder_health(
        str(tmp_path), "camera", now=1000,
        lister=lambda _directory: paths,
        parse_start=lambda path: starts[str(path)],
    )
    assert result["status"] == "ok"
    assert result["segments"] == 2
    assert result["max_gap_s"] == 900.0


def test_recorder_health_reports_stale_and_gap(tmp_path):
    camera_dir = tmp_path / "camera"
    camera_dir.mkdir()
    path = camera_dir / "a.mkv"
    path.write_bytes(b"")
    os.utime(path, (0, 0))
    assert reliability.recorder_health(
        str(tmp_path), "camera", now=1000,
        lister=lambda _directory: [path], parse_start=lambda _path: 0,
    )["reason"] == "stale_output"

    second = camera_dir / "b.mkv"
    second.write_bytes(b"")
    os.utime(second, (1000, 1000))
    assert reliability.recorder_health(
        str(tmp_path), "camera", now=1000,
        lister=lambda _directory: [path, second],
        parse_start=lambda item: 0 if item == path else 3600,
    )["reason"] == "recording_gap"


def test_recorder_health_ignores_gap_outside_recent_continuity_window(tmp_path):
    camera_dir = tmp_path / "camera"
    camera_dir.mkdir()
    paths = [camera_dir / name for name in ("old-a.mkv", "old-b.mkv", "recent-a.mkv", "recent-b.mkv")]
    for path in paths:
        path.write_bytes(b"")
        os.utime(path, (10_000, 10_000))

    starts = dict(zip(paths, (0, 3600, 7200, 8100), strict=True))
    result = reliability.recorder_health(
        str(tmp_path), "camera", now=9000, continuity_window=3600,
        lister=lambda _directory: paths,
        parse_start=lambda path: starts[path],
    )

    assert result["status"] == "ok"
    assert result["reason"] == "continuous"
    assert result["max_gap_s"] == 900.0
    assert result["historical_max_gap_s"] == 3600.0


def test_recorder_health_is_unknown_without_root():
    assert reliability.recorder_health(None, "camera", now=1000)["status"] == "unknown"

def test_reliability_config_round_trip():
    app = config.load_config_from_dict({
        "reliability": {
            "enabled": True,
            "auto_fix": False,
            "allowed_repairs": ["smarttrack"],
            "recorder_max_age": 120,
        },
        "cameras": [{"name": "front", "host": "192.0.2.50"}],
    })
    assert app.reliability.enabled is True
    assert app.reliability.auto_fix is False
    assert app.reliability.allowed_repairs == ("smarttrack",)
    assert app.reliability.recorder_max_age == 120
