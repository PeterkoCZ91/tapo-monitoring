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


def test_extract_candidates_fast_mode_uses_seek_samples_only(tmp_path):
    calls = []

    def runner(args):
        calls.append(args)
        target = args[-1]
        assert "%02d" not in target
        with open(target, "wb") as f:
            f.write(b"fallback")
        return ""

    got = shadowscan.extract_candidates(
        "/rec/segment.mkv", 0.0, str(tmp_path), "seg0", runner=runner,
        scene_pass=False)

    assert len(got) == shadowscan.FALLBACK_FRAME_CAP + 1
    assert all("-ss" in args for args in calls)


def test_extract_candidates_bounds_and_optimizes_scene_ffmpeg(tmp_path, monkeypatch):
    """Scene analysis must not decode full-resolution video with an open-ended cost."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        target = args[-1]
        if "%02d" in target:
            with open(target % 1, "wb") as f:
                f.write(b"scene")
            return type("Result", (), {"stderr": b"[showinfo] pts_time:5.0\n"})()
        with open(target, "wb") as f:
            f.write(b"mid")
        return type("Result", (), {"stderr": b""})()

    monkeypatch.setattr(shadowscan.subprocess, "run", fake_run)
    got = shadowscan.extract_candidates(
        "/rec/segment.mkv", 1000.0, str(tmp_path), "seg0")

    assert len(got) == 2
    assert [kwargs["timeout"] for _args, kwargs in calls] == [
        shadowscan.SCENE_EXTRACTION_TIMEOUT, shadowscan.SEEK_EXTRACTION_TIMEOUT]
    scene_args = calls[0][0]
    scene_filter = scene_args[scene_args.index("-vf") + 1]
    assert scene_filter.index("scale=") < scene_filter.index("select=")
    assert "-an" in scene_args


def test_extract_candidates_survives_ffmpeg_failure(tmp_path):
    def boom(args):
        raise RuntimeError("ffmpeg exploded")
    got = shadowscan.extract_candidates(
        "/rec/zaznam_20260812T070000.mkv", 0.0, str(tmp_path), "seg0", runner=boom)
    assert got == []


def test_extract_candidates_uses_spread_seek_fallback_after_scene_timeout(tmp_path):
    calls = []

    def runner(args):
        calls.append(args)
        target = args[-1]
        if "%02d" in target:
            raise TimeoutError("scene pass too slow")
        with open(target, "wb") as f:
            f.write(b"\xff\xd8fallback")
        return ""

    got = shadowscan.extract_candidates(
        "/rec/zaznam_20260812T070000.mkv", 0.0, str(tmp_path), "seg0",
        runner=runner)

    seek_offsets = [args[args.index("-ss") + 1] for args in calls if "-ss" in args]
    assert len(seek_offsets) == shadowscan.FALLBACK_FRAME_CAP + 1  # + uniform mid-frame
    assert "0" not in seek_offsets
    assert len(got) == shadowscan.FALLBACK_FRAME_CAP + 1


def test_extract_candidates_caps_total_ffmpeg_work_per_segment(tmp_path, monkeypatch):
    """A timed-out scene pass must not unlock another full fallback budget."""
    now = [0.0]
    calls = []

    def fake_run(args, *, timeout):
        calls.append((args, timeout))
        now[0] += timeout
        if "-ss" not in args:
            raise TimeoutError("scene pass too slow")
        target = args[-1]
        with open(target, "wb") as f:
            f.write(b"fallback")
        return ""

    monkeypatch.setattr(shadowscan, "_run_ffmpeg", fake_run)
    got = shadowscan.extract_candidates(
        "/rec/segment.mkv", 0.0, str(tmp_path), "seg0",
        clock=lambda: now[0])

    assert sum(timeout for _args, timeout in calls) <= (
        shadowscan.SEGMENT_EXTRACTION_TIMEOUT)
    assert len(calls) == 3  # scene + two seeks; later fallback/mid are skipped
    assert len(got) == 2


def test_ffmpeg_seek_extraction_uses_short_timeout(monkeypatch):
    seen = {}

    def run(args, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return type("Proc", (), {"stderr": b""})()

    monkeypatch.setattr(shadowscan.subprocess, "run", run)
    shadowscan._run_ffmpeg(["ffmpeg", "-ss", "112", "-i", "segment.mkv"])
    assert seen["timeout"] == shadowscan.SEEK_EXTRACTION_TIMEOUT


def test_score_candidates_hits_budget_and_rate(tmp_path):
    naps = []
    results = {"a.jpg": {"person": 0.8, "animal": 0.0, "box": [1, 2, 3, 4]},
               "b.jpg": {"person": 0.1, "animal": 0.0, "box": None},
               "c.jpg": {"person": 0.9, "animal": 0.0, "box": None}}

    def fake_score(url, path, timeout=10, tiles=1, **kw):
        assert tiles == 1
        return results[os.path.basename(path)]

    out = shadowscan.score_candidates(
        [("a.jpg", 10.0), ("b.jpg", 20.0), ("c.jpg", 30.0)],
        "http://127.0.0.1:1/score", 0.55,
        budget=2, score=fake_score, sleep=naps.append)
    assert [h["ts"] for h in out["hits"]] == [10.0]   # c.jpg fell past the budget
    assert out["scored"] == 2 and out["trimmed"] is True and out["aborted"] is False
    assert naps == [shadowscan.DEFAULT_RATE]          # gap between request 1 and 2 only


def test_score_candidates_passes_pseudonymous_source_id():
    seen = []

    def fake_score(url, path, timeout=10, tiles=1, *, source_id=None):
        seen.append(source_id)
        return {"person": 0.8, "animal": 0.0}

    out = shadowscan.score_candidates(
        [("frame.jpg", 10.0)], "http://scorer/score", 0.55,
        budget=1, score=fake_score, source_id="0123456789abcdef", sleep=lambda _s: None)

    assert out["scored"] == 1
    assert seen == ["0123456789abcdef"]


def test_score_candidates_aborts_after_consecutive_failures():
    def dead(url, path, timeout=10, tiles=1, **kw):
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

    def fake_score(url, path, timeout=10, tiles=1, **kw):
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


def test_run_scan_keeps_scene_pass_enabled_with_default_runner(tmp_path, monkeypatch):
    app, _root = _app_with_recorder(tmp_path, monkeypatch)
    calls = []

    def fake_extract(*args, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(shadowscan, "extract_candidates", fake_extract)
    shadowscan.run_scan(
        app, "2026-08-12", out_dir=str(tmp_path / "work"),
        ledger_factory=_FakeLedger, runner=None)

    assert calls
    assert calls[0].get("scene_pass", True) is True


def test_run_scan_gives_each_shadow_only_observation_a_distinct_evidence_file(
        tmp_path, monkeypatch):
    """Two shadow-only observations in one run with the same peak score must not
    collide on `_stamp(now)` and overwrite each other's archived JPEG."""
    app, _root = _app_with_recorder(tmp_path, monkeypatch)
    review = tmp_path / "review"
    monkeypatch.setenv("TAPO_REVIEW_LOG_DIR", str(review))
    seg_start = time.mktime((2026, 8, 12, 7, 0, 0, 0, 0, -1))

    def fake_runner(args):
        target = args[-1]
        if "%02d" in target:
            for k in (1, 2):                         # pts_time below: >180s apart
                with open(target % k, "wb") as f:
                    f.write(b"\xff\xd8hit")
            return "[showinfo] pts_time:5.0\n[showinfo] pts_time:400.0\n"
        with open(target, "wb") as f:
            f.write(b"\xff\xd8mid")
        return ""

    def fake_score(url, path, timeout=10, tiles=1, **kw):
        if path.endswith("_sc_01.jpg") or path.endswith("_sc_02.jpg"):
            return {"person": 0.81, "animal": 0.0, "box": None}
        return {"person": 0.02, "animal": 0.0, "box": None}

    fake = _FakeLedger()          # no camera events -> both hits are shadow-only
    summary = shadowscan.run_scan(
        app, "2026-08-12", out_dir=str(tmp_path / "work"),
        ledger_factory=lambda: fake, score=fake_score, runner=fake_runner,
        now=seg_start + 90000)

    assert summary["cameras"]["front"]["observations"] == 2
    assert summary["cameras"]["front"]["shadow_only"] == 2

    saved = list(review.glob("front_shadow_p0.81_*.jpg"))
    assert len(saved) == 2                      # both frames archived, neither overwritten

    lines = (review / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert records[0]["ts"] != records[1]["ts"]


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

    def fake_score(url, path, timeout=10, tiles=1, **kw):
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

    def fake_score(url, path, timeout=10, tiles=1, **kw):
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


def _app_with_many_segments(tmp_path, monkeypatch, count, host="192.0.2.11"):
    from tapo_monitor import config as cfg
    root = tmp_path / "rec"
    hour = root / host / "2026-08-12" / "07"
    hour.mkdir(parents=True)
    for index in range(count):
        (hour / f"zaznam_20260812T07{index:02d}00.mkv").write_bytes(b"mkv")
    monkeypatch.setenv("RECORDING_ROOT", str(root))
    app = cfg.load_config_from_dict({"cameras": [{
        "name": "front", "host": host,
        "scorer": {"url": "http://127.0.0.1:1/score", "threshold": 0.55}}]})
    return app


def test_run_scan_stops_extracting_when_the_run_budget_is_spent(tmp_path, monkeypatch, caplog):
    # Per-segment timeouts bound one ffmpeg call, not the night: 96 segments x 120 s is
    # over three hours of decode per camera, and the timer starts at 03:00. The run needs
    # its own ceiling, and a cap that trims work must say so instead of looking complete.
    app = _app_with_many_segments(tmp_path, monkeypatch, 5)
    ticks = iter([0, 0, 10, 40, 130, 200, 260])

    def fake_extract(*args, **kwargs):
        return []

    monkeypatch.setattr(shadowscan, "extract_candidates", fake_extract)
    with caplog.at_level("WARNING", logger="tapo_monitor.shadowscan"):
        summary = shadowscan.run_scan(
            app, "2026-08-12", out_dir=str(tmp_path / "work"),
            ledger_factory=_FakeLedger, score=lambda *a, **k: None,
            extract_budget=100, clock=lambda: next(ticks), now=1_000_000)

    front = summary["cameras"]["front"]
    assert front["segments"] == 5
    assert front["segments_skipped"] == 2       # budget ran out mid-run
    assert summary["extract_exhausted"] is True
    assert any("extraction budget" in message for message in caplog.messages)


def test_run_scan_extracts_every_segment_inside_the_budget(tmp_path, monkeypatch):
    app = _app_with_many_segments(tmp_path, monkeypatch, 3, host="192.0.2.12")
    monkeypatch.setattr(shadowscan, "extract_candidates", lambda *a, **k: [])

    summary = shadowscan.run_scan(
        app, "2026-08-12", out_dir=str(tmp_path / "work"),
        ledger_factory=_FakeLedger, score=lambda *a, **k: None,
        extract_budget=3600, now=1_000_000)

    assert summary["cameras"]["front"]["segments_skipped"] == 0
    assert summary["extract_exhausted"] is False


def _app_with_two_recorders(tmp_path, monkeypatch, count, hosts=("192.0.2.20",
                                                                "192.0.2.21")):
    """Two scannable cameras, each with ``count`` segments on the same date."""
    from tapo_monitor import config as cfg
    root = tmp_path / "rec"
    for host in hosts:
        for index in range(count):
            # real recorder layout: four 15-minute segments per hour directory, so a
            # 96-segment day stays a valid set of parseable timestamps
            hour, minute = divmod(index, 4)
            hour_dir = root / host / "2026-08-12" / f"{hour:02d}"
            hour_dir.mkdir(parents=True, exist_ok=True)
            name = f"zaznam_20260812T{hour:02d}{minute * 15:02d}00.mkv"
            (hour_dir / name).write_bytes(b"mkv")
    monkeypatch.setenv("RECORDING_ROOT", str(root))
    return cfg.load_config_from_dict({"cameras": [
        {"name": "first", "host": hosts[0],
         "scorer": {"url": "http://127.0.0.1:1/score", "threshold": 0.55}},
        {"name": "second", "host": hosts[1],
         "scorer": {"url": "http://127.0.0.1:1/score", "threshold": 0.55}},
    ]})


def test_run_scan_does_not_let_the_first_camera_eat_the_whole_extract_budget(
        tmp_path, monkeypatch):
    # A single run-wide deadline plus sequential cameras starves whoever is last in
    # cameras.yaml: in production the first camera spent the whole budget and the second
    # got 25 of its 96 segments. Each camera must get its own share of the decode budget.
    app = _app_with_two_recorders(tmp_path, monkeypatch, 4)
    elapsed = [0.0]

    def fake_extract(*args, **kwargs):
        elapsed[0] += 30.0          # every segment costs the same to decode
        return []

    monkeypatch.setattr(shadowscan, "extract_candidates", fake_extract)
    summary = shadowscan.run_scan(
        app, "2026-08-12", out_dir=str(tmp_path / "work"),
        ledger_factory=_FakeLedger, score=lambda *a, **k: None,
        extract_budget=100, clock=lambda: elapsed[0], now=1_000_000)

    assert summary["cameras"]["first"]["segments_skipped"] > 0    # nobody gets it all
    assert summary["cameras"]["second"]["segments_skipped"] < 4   # nobody starves


def test_run_scan_splits_the_scoring_budget_between_cameras(tmp_path, monkeypatch):
    # frames_scored is bounded by one shared counter decremented in camera order, so the
    # first camera can consume every frame of the night's budget. Same starvation as the
    # decode budget, one loop further down.
    app = _app_with_two_recorders(tmp_path, monkeypatch, 3,
                                  hosts=("192.0.2.22", "192.0.2.23"))
    frames = [(str(tmp_path / "f.jpg"), 1_000_000.0 + n) for n in range(20)]
    monkeypatch.setattr(shadowscan, "extract_candidates", lambda *a, **k: list(frames))

    summary = shadowscan.run_scan(
        app, "2026-08-12", out_dir=str(tmp_path / "work"),
        ledger_factory=_FakeLedger, score=lambda *a, **k: {"person_score": 0.0},
        budget=10, rate=0, extract_budget=3600, now=1_000_000)

    assert summary["cameras"]["second"]["frames_scored"] > 0


def test_run_scan_rolls_an_unspent_camera_share_over_to_the_next_camera(
        tmp_path, monkeypatch):
    # A fair share must not become a per-camera cap: when the first camera finishes
    # cheaply, the second one gets the leftover instead of being held to half.
    app = _app_with_two_recorders(tmp_path, monkeypatch, 6,
                                  hosts=("192.0.2.24", "192.0.2.25"))
    frames = [(str(tmp_path / "f.jpg"), 1_000_000.0 + n) for n in range(4)]
    scored_per_camera = {}

    def fake_extract(mkv, *a, **k):
        return list(frames) if "192.0.2.25" in mkv else []

    def fake_score(url, path, **kwargs):
        key = kwargs.get("source_id")
        scored_per_camera[key] = scored_per_camera.get(key, 0) + 1
        return {"person_score": 0.0}

    monkeypatch.setattr(shadowscan, "extract_candidates", fake_extract)
    summary = shadowscan.run_scan(
        app, "2026-08-12", out_dir=str(tmp_path / "work"),
        ledger_factory=_FakeLedger, score=fake_score,
        budget=20, rate=0, extract_budget=3600, now=1_000_000)

    # The first camera produced no frames, so all 20 stay available for the second.
    assert summary["cameras"]["second"]["frames_scored"] == 20


def test_extract_candidates_scene_pass_decodes_keyframes_only(tmp_path, monkeypatch):
    # A 15-minute 4K HEVC segment is ~18000 frames but only ~300 keyframes. Decoding
    # every frame cannot finish a quiet segment inside SCENE_EXTRACTION_TIMEOUT (measured
    # on production media: >200 s and still no verdict), so the scene pass was killed and
    # the segment fell back to two seek frames. Keyframes are enough for scene changes.
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        target = args[-1]
        if "%02d" in target:
            with open(target % 1, "wb") as f:
                f.write(b"scene")
            return type("Result", (), {"stderr": b"[showinfo] pts_time:5.0\n"})()
        with open(target, "wb") as f:
            f.write(b"mid")
        return type("Result", (), {"stderr": b""})()

    monkeypatch.setattr(shadowscan.subprocess, "run", fake_run)
    shadowscan.extract_candidates("/rec/segment.mkv", 1000.0, str(tmp_path), "seg0")

    scene_args = calls[0]
    assert scene_args[scene_args.index("-skip_frame") + 1] == "nokey"
    # a decoder flag only takes effect on the input it precedes
    assert scene_args.index("-skip_frame") < scene_args.index("-i")


def test_default_extract_budget_covers_a_full_day_on_both_cameras(tmp_path, monkeypatch):
    # Sizing test, not a unit test: measured over a real day on a 2-core 2.8 GHz x86
    # host, a keyframe-only scene pass over one 15-minute 4K HEVC segment costs 30 s
    # (exits early on scene changes) to 59 s (bright daylight, traversed whole). Two
    # cameras x 96 segments at the worst case must fit, or the scan silently drops part
    # of the day again -- and the point of the nightly scan is the whole day.
    app = _app_with_two_recorders(tmp_path, monkeypatch, 96,
                                  hosts=("192.0.2.26", "192.0.2.27"))
    elapsed = [0.0]

    def fake_extract(*args, **kwargs):
        elapsed[0] += 59.0
        return []

    monkeypatch.setattr(shadowscan, "extract_candidates", fake_extract)
    summary = shadowscan.run_scan(
        app, "2026-08-12", out_dir=str(tmp_path / "work"),
        ledger_factory=_FakeLedger, score=lambda *a, **k: None,
        clock=lambda: elapsed[0], now=1_000_000)

    assert summary["extract_exhausted"] is False
    assert [cam["segments_skipped"] for cam in summary["cameras"].values()] == [0, 0]
