import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import config, scene_probe


def _conf(*names):
    return config.load_config_from_dict(
        {"cameras": [{"name": n, "host": f"10.0.0.{i}"} for i, n in enumerate(names, 1)]})


# ── select_cameras ───────────────────────────────────────────────────────────

def test_select_cameras_returns_named_in_requested_order():
    conf = _conf("yard", "front")
    picked = scene_probe.select_cameras(conf, ["front", "yard"])
    assert [c.name for c in picked] == ["front", "yard"]


def test_select_cameras_empty_names_returns_all():
    conf = _conf("yard", "front")
    assert [c.name for c in scene_probe.select_cameras(conf, [])] == ["yard", "front"]


def test_select_cameras_unknown_name_raises_naming_it():
    conf = _conf("front")
    try:
        scene_probe.select_cameras(conf, ["ghost"])
    except ValueError as e:
        assert "ghost" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown camera")


# ── summarize ────────────────────────────────────────────────────────────────

def test_summarize_extracts_person_animal_tile_and_top_non_person_classes():
    result = {"person": 0.82, "animal": 0.10,
              "classes": {"person": 0.82, "dog": 0.12, "cat": 0.05},
              "tile_person": 0.91, "box": [1, 2, 3, 4]}
    s = scene_probe.summarize(result)
    assert s["person"] == 0.82
    assert s["animal"] == 0.10
    assert s["tile_person"] == 0.91
    assert s["box"] == [1, 2, 3, 4]
    # person is reported on its own; top_classes shows what else scored, sorted desc.
    assert s["top_classes"] == [["dog", 0.12], ["cat", 0.05]]


def test_summarize_tolerates_missing_optional_keys():
    s = scene_probe.summarize({"person": 0.3})
    assert s["person"] == 0.3
    assert s["animal"] == 0.0
    assert s["tile_person"] is None
    assert s["top_classes"] == []
    assert s["box"] is None


# ── format_line / probe_filename ─────────────────────────────────────────────

def test_format_line_shows_scores_and_camera():
    s = scene_probe.summarize({"person": 0.82, "animal": 0.10,
                               "classes": {"dog": 0.12}, "tile_person": 0.91})
    line = scene_probe.format_line("front", s)
    assert "front" in line
    assert "0.82" in line and "0.10" in line and "0.91" in line
    assert "dog" in line


def test_probe_filename_encodes_scores_and_sanitizes_name():
    s = {"person": 0.82, "animal": 0.10}
    name = scene_probe.probe_filename("../ev il", s, now=1785200000.0)
    assert name.startswith("ev_il_p0.82_a0.10_")
    assert name.endswith(".jpg")
    assert "/" not in name


# ── run (orchestration with injected I/O) ────────────────────────────────────

def test_run_archives_frame_with_scores_and_returns_record(tmp_path):
    conf = _conf("front")
    src = tmp_path / "grab.jpg"
    src.write_bytes(b"\xff\xd8FRAME")
    result = {"person": 0.82, "animal": 0.10,
              "classes": {"person": 0.82, "dog": 0.12}, "tile_person": 0.91, "box": [1, 2, 3, 4]}
    archive = tmp_path / "probe"

    recs = scene_probe.run(conf, ["front"],
                           capture=lambda cfg: str(src),
                           score=lambda cfg, path: result,
                           archive_dir=str(archive), now=1785200000.0)

    assert len(recs) == 1 and recs[0]["ok"] is True
    assert recs[0]["person"] == 0.82 and recs[0]["tile_person"] == 0.91
    saved = list(archive.glob("*.jpg"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"\xff\xd8FRAME"
    assert saved[0].name.startswith("front_p0.82_a0.10_")
    rec = json.loads((archive / "index.jsonl").read_text().strip())
    assert rec["camera"] == "front" and rec["tile_person"] == 0.91


def test_run_records_grab_failure_without_crashing(tmp_path):
    conf = _conf("front")
    archive = tmp_path / "probe"
    recs = scene_probe.run(conf, ["front"],
                           capture=lambda cfg: None,
                           score=lambda cfg, path: {"person": 1.0},
                           archive_dir=str(archive), now=1.0)
    assert recs[0]["ok"] is False and recs[0]["error"] == "grab_failed"
    assert not archive.exists() or not list(archive.glob("*.jpg"))


def test_real_capture_rotates_like_production_frames(monkeypatch):
    # The probe is the scorer calibration tool: on a mis-mounted (rotated) camera it must
    # grab the same upright frame the detection pipeline scores, not an upside-down one.
    conf = config.load_config_from_dict(
        {"cameras": [{"name": "front", "host": "203.0.113.10", "rotate": 180}]})
    calls = []
    monkeypatch.setattr(scene_probe.snapshot, "capture_rtsp",
                        lambda url, **kw: calls.append(kw) or "/tmp/probe.jpg")

    assert scene_probe._real_capture(conf.cameras[0]) == "/tmp/probe.jpg"
    assert [kw.get("rotate") for kw in calls] == [180]


def test_real_capture_retry_also_rotates(monkeypatch):
    conf = config.load_config_from_dict(
        {"cameras": [{"name": "front", "host": "203.0.113.10", "rotate": 90}]})
    calls = []
    monkeypatch.setattr(scene_probe.snapshot, "capture_rtsp",
                        lambda url, **kw: calls.append(kw) or None)

    assert scene_probe._real_capture(conf.cameras[0]) is None
    assert [kw.get("rotate") for kw in calls] == [90, 90]   # retry keeps the rotation


def test_real_score_passes_pseudonymous_source_id(monkeypatch, tmp_path):
    conf = config.load_config_from_dict(
        {"cameras": [{"name": "front", "host": "203.0.113.10",
                       "scorer": {"url": "http://scorer/score"}}]})
    seen = []
    monkeypatch.setattr(
        scene_probe.scorer, "score_image",
        lambda url, path, **kwargs: seen.append(kwargs["source_id"]) or {"person": 0.1},
    )

    result = scene_probe._real_score(1)(conf.cameras[0], str(tmp_path / "frame.jpg"))

    assert result["person"] == 0.1
    assert seen == [scene_probe.scorer.source_id_for_camera("front")]


def test_run_records_scorer_unavailable(tmp_path):
    conf = _conf("front")
    src = tmp_path / "g.jpg"
    src.write_bytes(b"x")
    recs = scene_probe.run(conf, ["front"],
                           capture=lambda cfg: str(src),
                           score=lambda cfg, path: None,
                           archive_dir=str(tmp_path / "p"), now=1.0)
    assert recs[0]["ok"] is False and recs[0]["error"] == "scorer_unavailable"
