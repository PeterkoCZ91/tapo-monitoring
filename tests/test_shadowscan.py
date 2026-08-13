import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import shadowscan


def _local(y, mo, d, h=0, mi=0, s=0):
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


def test_resolve_date_defaults_to_yesterday():
    now = _local(2026, 8, 13, 3, 0)
    assert shadowscan.resolve_date(None, now=now) == "2026-08-12"
    assert shadowscan.resolve_date("yesterday", now=now) == "2026-08-12"
    assert shadowscan.resolve_date("2026-08-01", now=now) == "2026-08-01"


def test_resolve_date_rejects_garbage():
    import pytest
    with pytest.raises(ValueError):
        shadowscan.resolve_date("last tuesday")


def test_segments_for_date_sorted_and_parsed(tmp_path):
    host = "192.0.2.10"
    hour_dir = tmp_path / host / "2026-08-12" / "07"
    hour_dir.mkdir(parents=True)
    (hour_dir / "zaznam_20260812T071500.mkv").write_bytes(b"x")
    (hour_dir / "zaznam_20260812T070000.mkv").write_bytes(b"x")
    (hour_dir / "notes.txt").write_bytes(b"x")
    segs = shadowscan.segments_for_date(str(tmp_path), host, "2026-08-12")
    assert [os.path.basename(p) for p, _ in segs] == [
        "zaznam_20260812T070000.mkv", "zaznam_20260812T071500.mkv"]
    assert segs[0][1] == _local(2026, 8, 12, 7, 0)


def test_segments_for_date_empty_tree(tmp_path):
    assert shadowscan.segments_for_date(str(tmp_path), "192.0.2.10", "2026-08-12") == []


SHOWINFO = (
    "[Parsed_showinfo_1 @ 0x1] n:   0 pts:  12800 pts_time:5.12    pos: 1 fmt:yuvj420p\n"
    "[Parsed_showinfo_1 @ 0x1] n:   1 pts:  64000 pts_time:25.6    pos: 2 fmt:yuvj420p\n"
    "irrelevant line\n"
)


def test_parse_showinfo_times():
    assert shadowscan.parse_showinfo_times(SHOWINFO) == [5.12, 25.6]
    assert shadowscan.parse_showinfo_times("") == []


def test_extract_candidates_scene_frames_and_uniform(tmp_path):
    seg_start = _local(2026, 8, 12, 7, 0)
    calls = []

    def fake_runner(args):
        calls.append(args)
        target = args[-1]        # the implementation passes the output path/pattern last
        if "%02d" in target:
            for k in (1, 2):
                with open(target % k, "wb") as f:
                    f.write(b"\xff\xd8scene")
            return SHOWINFO
        with open(target, "wb") as f:
            f.write(b"\xff\xd8mid")
        return ""

    got = shadowscan.extract_candidates(
        "/rec/zaznam_20260812T070000.mkv", seg_start, str(tmp_path), "seg0",
        runner=fake_runner)
    stamps = [ts for _, ts in got]
    assert seg_start + 5.12 in stamps and seg_start + 25.6 in stamps
    assert seg_start + 450 in stamps          # uniform mid-segment frame
    assert len(got) == 3
    assert all(os.path.exists(p) for p, _ in got)


def test_extract_candidates_survives_ffmpeg_failure(tmp_path):
    def boom(args):
        raise RuntimeError("ffmpeg exploded")
    got = shadowscan.extract_candidates(
        "/rec/zaznam_20260812T070000.mkv", 0.0, str(tmp_path), "seg0", runner=boom)
    assert got == []


def test_score_candidates_hits_budget_and_rate(tmp_path):
    naps = []
    results = {"a.jpg": {"person": 0.8, "animal": 0.0, "box": [1, 2, 3, 4]},
               "b.jpg": {"person": 0.1, "animal": 0.0, "box": None},
               "c.jpg": {"person": 0.9, "animal": 0.0, "box": None}}

    def fake_score(url, path, timeout=10, tiles=1):
        assert tiles == 1
        return results[os.path.basename(path)]

    out = shadowscan.score_candidates(
        [("a.jpg", 10.0), ("b.jpg", 20.0), ("c.jpg", 30.0)],
        "http://127.0.0.1:1/score", 0.55,
        budget=2, score=fake_score, sleep=naps.append)
    assert [h["ts"] for h in out["hits"]] == [10.0]   # c.jpg fell past the budget
    assert out["scored"] == 2 and out["trimmed"] is True and out["aborted"] is False
    assert naps == [shadowscan.DEFAULT_RATE]          # gap between request 1 and 2 only


def test_score_candidates_aborts_after_consecutive_failures():
    def dead(url, path, timeout=10, tiles=1):
        return None
    out = shadowscan.score_candidates(
        [(f"{k}.jpg", float(k)) for k in range(5)], "http://x/score", 0.5,
        budget=100, score=dead, sleep=lambda s: None)
    assert out["aborted"] is True
    assert out["scored"] == 0
    assert len(out["hits"]) == 0


def test_cluster_hits_merges_within_gap_and_keeps_peak_frame():
    hits = [
        {"ts": 100.0, "path": "a.jpg", "person": 0.6, "box": None},
        {"ts": 220.0, "path": "b.jpg", "person": 0.9, "box": None},   # 120s later: same
        {"ts": 1000.0, "path": "c.jpg", "person": 0.7, "box": None},  # far: new one
    ]
    obs = shadowscan.cluster_hits(hits)
    assert len(obs) == 2
    assert obs[0] == {"start": 100.0, "end": 220.0, "peak": 0.9, "frame": "b.jpg"}
    assert obs[1] == {"start": 1000.0, "end": 1000.0, "peak": 0.7, "frame": "c.jpg"}


def test_cluster_hits_empty():
    assert shadowscan.cluster_hits([]) == []


class _FakeLedger:
    def __init__(self):
        self.shadow = []
        self.camera_events = []

    def record_shadow_event(self, **kw):
        self.shadow.append(kw)
        return len(self.shadow)

    def camera_events_between(self, camera, start, end):
        return [t for (c, t) in self.camera_events if c == camera and start <= t <= end]


def _app_with_recorder(tmp_path, monkeypatch, host="192.0.2.10"):
    from tapo_monitor import config as cfg
    root = tmp_path / "rec"
    hour = root / host / "2026-08-12" / "07"
    hour.mkdir(parents=True)
    (hour / "zaznam_20260812T070000.mkv").write_bytes(b"mkv")
    monkeypatch.setenv("RECORDING_ROOT", str(root))
    app = cfg.load_config_from_dict({"cameras": [{
        "name": "front", "host": host,
        "scorer": {"url": "http://127.0.0.1:1/score", "threshold": 0.55}}]})
    return app, root


def test_run_scan_records_matches_and_archives_misses(tmp_path, monkeypatch):
    app, _root = _app_with_recorder(tmp_path, monkeypatch)
    review = tmp_path / "review"
    monkeypatch.setenv("TAPO_REVIEW_LOG_DIR", str(review))
    seg_start = time.mktime((2026, 8, 12, 7, 0, 0, 0, 0, -1))

    def fake_runner(args):
        target = args[-1]
        if "%02d" in target:
            with open(target % 1, "wb") as f:
                f.write(b"\xff\xd8hit")
            return "[showinfo] pts_time:5.0\n"
        with open(target, "wb") as f:
            f.write(b"\xff\xd8mid")
        return ""

    def fake_score(url, path, timeout=10, tiles=1):
        if path.endswith("_sc_01.jpg"):
            return {"person": 0.81, "animal": 0.0, "box": None}
        return {"person": 0.02, "animal": 0.0, "box": None}

    fake = _FakeLedger()          # no camera events -> the hit is a miss candidate
    summary = shadowscan.run_scan(
        app, "2026-08-12", out_dir=str(tmp_path / "work"),
        ledger_factory=lambda: fake, score=fake_score, runner=fake_runner,
        now=seg_start + 90000)
    assert summary["cameras"]["front"]["observations"] == 1
    assert summary["cameras"]["front"]["shadow_only"] == 1
    assert summary["cameras"]["front"]["matched"] == 0
    assert fake.shadow[0]["camera"] == "front"
    assert fake.shadow[0]["adapter"] == "local_scorer"
    saved = list(review.glob("front_shadow_p0.81_*.jpg"))
    assert len(saved) == 1                      # evidence frame archived
    assert (review / shadowscan.SUMMARY_NAME).exists()
    work_leftovers = list((tmp_path / "work").glob("*.jpg"))
    assert work_leftovers == []                 # candidates cleaned up


def test_run_scan_matched_observation_writes_no_evidence(tmp_path, monkeypatch):
    app, _root = _app_with_recorder(tmp_path, monkeypatch)
    review = tmp_path / "review"
    monkeypatch.setenv("TAPO_REVIEW_LOG_DIR", str(review))
    seg_start = time.mktime((2026, 8, 12, 7, 0, 0, 0, 0, -1))

    def fake_runner(args):
        target = args[-1]
        if "%02d" in target:
            with open(target % 1, "wb") as f:
                f.write(b"\xff\xd8hit")
            return "[showinfo] pts_time:5.0\n"
        with open(target, "wb") as f:
            f.write(b"\xff\xd8mid")
        return ""

    def fake_score(url, path, timeout=10, tiles=1):
        # Only the scene-change frame is a hit; the uniform mid-segment frame stays
        # below threshold so this fixture yields exactly one observation to match.
        if path.endswith("_sc_01.jpg"):
            return {"person": 0.81, "animal": 0.0, "box": None}
        return {"person": 0.02, "animal": 0.0, "box": None}

    fake = _FakeLedger()
    fake.camera_events = [("front", seg_start + 60.0)]   # camera saw it too
    summary = shadowscan.run_scan(
        app, "2026-08-12", out_dir=str(tmp_path / "work"),
        ledger_factory=lambda: fake, score=fake_score,
        runner=fake_runner, now=seg_start + 90000)
    assert summary["cameras"]["front"]["matched"] == 1
    assert summary["cameras"]["front"]["shadow_only"] == 0
    assert not list(review.glob("front_shadow_*.jpg"))


def test_run_scan_missing_root_is_calm(tmp_path, monkeypatch, caplog):
    from tapo_monitor import config as cfg
    monkeypatch.delenv("RECORDING_ROOT", raising=False)
    monkeypatch.setenv("TAPO_REVIEW_LOG_DIR", str(tmp_path / "review"))
    app = cfg.load_config_from_dict({"cameras": [{
        "name": "front", "host": "192.0.2.10",
        "scorer": {"url": "http://127.0.0.1:1/score", "threshold": 0.55}}]})
    with caplog.at_level(logging.WARNING):
        summary = shadowscan.run_scan(app, "2026-08-12", out_dir=str(tmp_path / "w"),
                                      ledger_factory=lambda: _FakeLedger())
    assert summary["cameras"] == {}
    assert (tmp_path / "review" / shadowscan.SUMMARY_NAME).exists()
    assert any("RECORDING_ROOT" in r.message for r in caplog.records)


def test_run_scan_archives_evidence_with_scan_time_not_observation_time(
        tmp_path, monkeypatch):
    """The index ts must be the scan run's now, not the (earlier) observation time,
    or a digest collecting the last 24h will never see most of a night's candidates."""
    app, _root = _app_with_recorder(tmp_path, monkeypatch)
    review = tmp_path / "review"
    monkeypatch.setenv("TAPO_REVIEW_LOG_DIR", str(review))
    seg_start = time.mktime((2026, 8, 12, 7, 0, 0, 0, 0, -1))   # early in the scanned day

    def fake_runner(args):
        target = args[-1]
        if "%02d" in target:
            with open(target % 1, "wb") as f:
                f.write(b"\xff\xd8hit")
            return "[showinfo] pts_time:5.0\n"
        with open(target, "wb") as f:
            f.write(b"\xff\xd8mid")
        return ""

    def fake_score(url, path, timeout=10, tiles=1):
        if path.endswith("_sc_01.jpg"):
            return {"person": 0.81, "animal": 0.0, "box": None}
        return {"person": 0.02, "animal": 0.0, "box": None}

    scan_now = seg_start + 90000    # the scan runs long after the observation
    fake = _FakeLedger()
    shadowscan.run_scan(
        app, "2026-08-12", out_dir=str(tmp_path / "work"),
        ledger_factory=lambda: fake, score=fake_score, runner=fake_runner,
        now=scan_now)

    lines = (review / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ts"] == scan_now
    assert record["event_ts"] == seg_start + 5.0


def test_run_scan_survives_ledger_construction_failure(tmp_path, monkeypatch):
    from tapo_monitor import config as cfg
    monkeypatch.setenv("TAPO_REVIEW_LOG_DIR", str(tmp_path / "review"))
    app = cfg.load_config_from_dict({"cameras": [{
        "name": "front", "host": "192.0.2.10",
        "scorer": {"url": "http://127.0.0.1:1/score", "threshold": 0.55}}]})

    def boom():
        raise OSError("boom")

    summary = shadowscan.run_scan(app, "2026-08-12", out_dir=str(tmp_path / "w"),
                                  ledger_factory=boom)
    assert summary["aborted"] is True
    assert (tmp_path / "review" / shadowscan.SUMMARY_NAME).exists()
