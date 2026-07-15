import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import recclip


def _name(dt):
    return f"zaznam_{dt.strftime('%Y%m%dT%H%M%S')}.mkv"


# ── segment location ─────────────────────────────────────────────────────────

def test_parse_segment_start_roundtrips_local_wallclock():
    dt = datetime(2026, 7, 11, 22, 45, 4)
    ts = recclip.parse_segment_start("/x/" + _name(dt))
    assert datetime.fromtimestamp(ts) == dt


def test_parse_segment_start_rejects_bad_name():
    with pytest.raises(ValueError):
        recclip.parse_segment_start("/x/notasegment.mkv")


def test_segment_for_picks_covering_segment():
    day = datetime(2026, 7, 11, 22, 45, 4)              # covers 22:45:04..23:00:04
    event = (day + timedelta(seconds=200)).timestamp()  # 22:48:24
    files = {
        "/r/HOST/2026-07-11/22": [
            "/r/HOST/2026-07-11/22/" + _name(day),
            "/r/HOST/2026-07-11/22/" + _name(day - timedelta(minutes=15)),
        ],
    }
    got = recclip.segment_for("/r", "HOST", event, lister=lambda d: files.get(d, []))
    assert got is not None and got[0].endswith(_name(day))


def test_segment_for_none_when_no_cover():
    day = datetime(2026, 7, 11, 22, 45, 4)
    event = (day - timedelta(seconds=10)).timestamp()   # before the only segment
    def lister(d):
        return ["/r/HOST/2026-07-11/22/" + _name(day)] if d.endswith("/22") else []
    assert recclip.segment_for("/r", "HOST", event, lister=lister) is None


# ── blur scoring + sharpest selection ────────────────────────────────────────

def test_blur_score_parses_ffmpeg_output():
    out = "frame stuff\n[Parsed_blurdetect_0 @ 0x1] blur mean: 4.6165748\nmore\n"
    assert abs(recclip.blur_score("/x.jpg", runner=lambda p: out) - 4.6165748) < 1e-6


def test_blur_score_none_when_no_match():
    assert recclip.blur_score("/x.jpg", runner=lambda p: "no metric here") is None


def test_select_sharpest_picks_lowest_blur():
    cands = [("a.jpg", 9.0), ("b.jpg", 3.0), ("c.jpg", 7.0)]
    assert recclip.select_sharpest(cands) == "b.jpg"


def test_select_sharpest_falls_back_to_first_when_no_blur():
    assert recclip.select_sharpest([("a.jpg", None), ("b.jpg", None)]) == "a.jpg"


def test_select_sharpest_empty_is_none():
    assert recclip.select_sharpest([]) is None


# ── extraction + fetch + delay ───────────────────────────────────────────────

def test_fresh_delay_has_no_pytapo_guard():
    assert recclip.fresh_delay(48) == 48 + recclip.RECORDING_READY_MARGIN


def test_extract_frames_seeks_from_event_offset(tmp_path):
    calls = []
    def runner(args):
        calls.append(args)
        open(args[-1], "w").write("x")   # simulate ffmpeg writing the output file
    out = recclip.extract_frames("/seg.mkv", seg_start=1000.0, event_start=1030.0,
                                 span=12, every=4, out_dir=str(tmp_path), base="rec_1",
                                 runner=runner)
    ss_values = [a[a.index("-ss") + 1] for a in calls]
    assert ss_values == ["30", "34", "38"]   # offset 30 (=1030-1000), +every up to span
    assert len(out) == 3


def test_fetch_recording_frames_empty_when_no_segment():
    got = recclip.fetch_recording_frames(
        cfg=None, event_start=1000.0, span=12, out_dir="/tmp",
        base_dir="/r", segment_for=lambda *a, **k: None)
    assert got == []


def test_fetch_recording_frames_empty_when_no_root(monkeypatch):
    monkeypatch.delenv("RECORDING_ROOT", raising=False)
    called = []
    got = recclip.fetch_recording_frames(
        cfg=None, event_start=1000.0, span=12, out_dir="/tmp",
        segment_for=lambda *a, **k: called.append(1))   # must NOT be reached
    assert got == [] and called == []


def test_fetch_recording_frames_defaults_base_dir_from_env(monkeypatch):
    class Cfg:
        host = "HOST"
    monkeypatch.setenv("RECORDING_ROOT", "/envroot")
    seen = {}
    def seg(base_dir, host, ev, **k):
        seen["base_dir"] = base_dir
        return ("/envroot/HOST/x.mkv", 1000.0)
    recclip.fetch_recording_frames(
        cfg=Cfg(), event_start=1010.0, span=8, out_dir="/tmp",
        segment_for=seg, extract=lambda *a, **k: [])
    assert seen["base_dir"] == "/envroot"


def test_fetch_recording_frames_uses_segment(tmp_path):
    class Cfg:
        host = "HOST"
    seg = ("/r/HOST/2026-07-11/22/zaznam_20260711T224504.mkv", 1000.0)
    captured = {}
    def fake_extract(mkv, seg_start, event_start, span, every, out_dir, base, **k):
        captured.update(mkv=mkv, seg_start=seg_start, event_start=event_start)
        return ["/tmp/rec_0.jpg"]
    got = recclip.fetch_recording_frames(
        cfg=Cfg(), event_start=1030.0, span=12, out_dir=str(tmp_path),
        base_dir="/r", segment_for=lambda *a, **k: seg, extract=fake_extract)
    assert got == ["/tmp/rec_0.jpg"]
    assert captured["mkv"] == seg[0] and captured["seg_start"] == 1000.0
