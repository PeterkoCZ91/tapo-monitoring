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
