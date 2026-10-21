"""Recorder paths search every failover candidate directory, newest recording wins."""
import os
from types import SimpleNamespace

from tapo_monitor import camera, recclip, shadowscan, snapshot


def _cfg(*hosts):
    return SimpleNamespace(host=hosts[0], hosts=tuple(hosts) if len(hosts) > 1 else ())


def _seg(root, host, name="zaznam_20260101T100000.mkv", mtime=None):
    d = root / host / "2026-01-01" / "10"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"x")
    if mtime:
        os.utime(f, (mtime, mtime))
    return str(f)


def test_recording_hosts_primary_first_and_scalar():
    assert camera.recording_hosts(_cfg("a", "b")) == ("a", "b")
    assert camera.recording_hosts(_cfg("a")) == ("a",)


def test_recording_key_newest_wins_and_defaults_to_primary(tmp_path):
    cfg = _cfg("a", "b")
    assert camera.recording_key(cfg) == "a"
    assert camera.recording_key(cfg, str(tmp_path)) == "a"
    _seg(tmp_path, "a", mtime=1000)
    _seg(tmp_path, "b", mtime=2000)
    assert camera.recording_key(cfg, str(tmp_path)) == "b"
    assert camera.recording_key(_cfg("a"), str(tmp_path)) == "a"


def test_latest_recording_frame_uses_newest_candidate(tmp_path):
    _seg(tmp_path, "a", mtime=1000)
    newer = _seg(tmp_path, "b", mtime=2000)
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        open(cmd[-1], "wb").write(b"jpg")

    out = snapshot.latest_recording_frame(("a", "b"), root=str(tmp_path), out_dir=str(tmp_path),
                                          max_age=10**9, now=3000, _run=run)
    assert out
    assert newer in seen[0]
    assert "snaprec_b_" in os.path.basename(out)


def test_segments_for_date_merges_candidates(tmp_path):
    _seg(tmp_path, "a", "zaznam_20260101T100000.mkv")
    _seg(tmp_path, "b", "zaznam_20260101T101500.mkv")
    got = shadowscan.segments_for_date(str(tmp_path), ("a", "b"), "2026-01-01")
    assert len(got) == 2
    assert len(shadowscan.segments_for_date(str(tmp_path), "a", "2026-01-01")) == 1


def test_segment_for_searches_all_hosts(tmp_path):
    _seg(tmp_path, "b", "zaznam_20260101T100000.mkv")
    start = recclip.parse_segment_start("zaznam_20260101T100000.mkv") + 60
    assert recclip.segment_for(str(tmp_path), ("a", "b"), start)
    assert recclip.segment_for(str(tmp_path), "a", start) is None
