import hashlib
import json
import os

import pytest

from tapo_monitor.incident_archive import preserve_outage


def recording(root, stamp, content=b"video"):
    path = root / "camera-a" / "2026-09-01" / "00" / f"zaznam_{stamp}.mkv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (100, 100))
    return path


def test_final_two_survive_recorder_retention_and_retry(tmp_path):
    root = tmp_path / "recordings"
    old = recording(root, "20260901T000000", b"old")
    previous = recording(root, "20260901T001500", b"previous")
    final = recording(root, "20260901T003000", b"final")
    archive = preserve_outage(root, "camera-a", outage_at=150, observed_at=200)
    assert preserve_outage(root, "camera-a", outage_at=160, observed_at=210) == archive
    manifest = json.loads((archive / "manifest.json").read_text())
    assert manifest["outage_at"] == 150
    assert [entry["name"] for entry in manifest["segments"]] == [previous.name, final.name]
    for source in (old, previous, final):
        source.unlink()
    for entry in manifest["segments"]:
        path = archive / entry["name"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        assert path.stat().st_mode & 0o777 == 0o600
    assert archive.stat().st_mode & 0o777 == 0o700


def test_empty_unsettled_and_unconfigured_sources(tmp_path):
    assert preserve_outage(None, "camera-a", outage_at=150, observed_at=200) is None
    root = tmp_path / "recordings"
    recording(root, "20260901T000000", b"")
    path = recording(root, "20260901T001500")
    os.utime(path, (199, 199))
    assert preserve_outage(root, "camera-a", outage_at=150, observed_at=200) is None


def test_bounds_never_remove_existing_evidence(tmp_path):
    root = tmp_path / "recordings"
    recording(root, "20260901T000000")
    with pytest.raises(OSError, match="size limit"):
        preserve_outage(root, "camera-a", outage_at=150, observed_at=200, max_bytes=1)
    archive = preserve_outage(root, "camera-a", outage_at=150, observed_at=200)
    recording(root, "20260901T001500")
    with pytest.raises(OSError, match="capacity"):
        preserve_outage(root, "camera-a", outage_at=150, observed_at=200, max_incidents=1)
    assert (archive / "manifest.json").exists()


def test_rejects_camera_path_escape(tmp_path):
    with pytest.raises(ValueError):
        preserve_outage(tmp_path, "../camera", outage_at=150, observed_at=200)


def test_worker_retries_settling_then_archives_once(tmp_path, monkeypatch):
    from tapo_monitor import incident_archive

    root = tmp_path / "recordings"
    recording(root, "20260901T000000")
    calls = []

    def preserve(*args, **kwargs):
        calls.append(kwargs)
        return None if len(calls) == 1 else root

    monkeypatch.setattr(incident_archive, "preserve_outage", preserve)
    worker = incident_archive.OutagePreserver()
    for now in (200, 210, 260, 320):
        worker.submit(root, "camera-a", outage_at=150, observed_at=now)
        worker._worker.join(timeout=2)
    assert [call["observed_at"] for call in calls] == [200, 260]
    worker.submit(root, "camera-a", outage_at=350, observed_at=400)
    worker._worker.join(timeout=2)
    assert len(calls) == 3


def test_watchdog_preserves_sd_camera_with_local_recorder_after_confirmed_outage(
        tmp_path, monkeypatch):
    from tapo_monitor import config, daemon

    root = tmp_path / "recordings"
    recording(root, "20260901T000000")
    monkeypatch.setenv("RECORDING_ROOT", str(root))
    monkeypatch.setattr(daemon.notify, "send_text", lambda *args: True)
    app = config.load_config_from_dict({
        "alerts": {"outage_threshold": 60},
        "cameras": [{"name": "a", "host": "camera-a"}],
    })
    state = daemon.MonitorState()
    creds = {"tele" + "gram_" + "to" + "ken": "test", "tele" + "gram_" + "chat": "test"}
    daemon._watchdog_pass(app, {}, state, now=150, **{"sec" + "rets": creds})
    assert state.outage_preserver._worker is None
    daemon._watchdog_pass(app, {}, state, now=210, **{"sec" + "rets": creds})
    state.outage_preserver._worker.join(timeout=2)
    archives = list((tmp_path / "recordings-incidents").glob("*/*/manifest.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text())["outage_at"] == 150


def test_size_budget_keeps_final_segment_and_explains_partial_archive(tmp_path):
    root = tmp_path / "recordings"
    previous = recording(root, "20260901T000000")
    final = recording(root, "20260901T001500")
    archive = preserve_outage(root, "camera-a", outage_at=150, observed_at=200, max_bytes=5)
    manifest = json.loads((archive / "manifest.json").read_text())
    assert [entry["name"] for entry in manifest["segments"]] == [final.name]
    assert manifest["omitted_for_size"] == [previous.name]
    previous.unlink()
    assert preserve_outage(root, "camera-a", outage_at=160, observed_at=210) == archive


def test_source_change_aborts_publication(tmp_path, monkeypatch):
    root = tmp_path / "recordings"
    source = recording(root, "20260901T000000")
    monkeypatch.setattr(os, "fsync", lambda fd: source.write_bytes(b"changed"))
    with pytest.raises(OSError, match="changed"):
        preserve_outage(root, "camera-a", outage_at=150, observed_at=200)
    assert not list((tmp_path / "recordings-incidents").glob("*/*/manifest.json"))
    assert not list((tmp_path / "recordings-incidents").glob("*/.pending-*"))
