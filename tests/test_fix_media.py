"""Cleanup/uniqueness fixes in snapshot, sdclip, recclip and sdworker."""
import os
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from tapo_monitor import recclip, sdclip, sdworker, snapshot

# 1. partial output removed on ffmpeg failure -------------------------------------

def test_sdclip_extract_frames_unlinks_partial(tmp_path, monkeypatch):
    def fake_run(argv, **kw):
        with open(argv[-1], "wb") as f:
            f.write(b"partial")
        raise subprocess.TimeoutExpired(argv, 30)
    monkeypatch.setattr(sdclip.subprocess, "run", fake_run)
    paths = sdclip._extract_frames("x.mp4", str(tmp_path), "b", 6, 3)
    assert paths == []
    assert os.listdir(tmp_path) == []


def test_recclip_extract_frames_unlinks_partial(tmp_path):
    def runner(args):
        with open(args[-1], "wb") as f:
            f.write(b"partial")
        raise RuntimeError("ffmpeg died")
    paths = recclip.extract_frames("x.mkv", 1000, 1000, 6, 3, str(tmp_path), "b",
                                   runner=runner)
    assert paths == []
    assert os.listdir(tmp_path) == []


def test_recclip_run_ffmpeg_unlinks_partial(tmp_path, monkeypatch):
    out = tmp_path / "o.jpg"

    def fake_run(args, **kw):
        out.write_bytes(b"partial")
        raise subprocess.TimeoutExpired(args, 30)
    monkeypatch.setattr(recclip.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        recclip._run_ffmpeg(["ffmpeg", str(out)])
    assert not out.exists()


# 2. timed-out download does not leak its file -------------------------------------

def test_fresh_loop_cleanup_runs_on_timeout_and_after_late_finish():
    import asyncio
    calls = []
    release = threading.Event()

    async def slow():
        while not release.is_set():
            await asyncio.sleep(0.01)

    with pytest.raises(TimeoutError):
        sdclip._run_in_fresh_loop(slow, timeout=0.05, cleanup=lambda: calls.append(1))
    assert len(calls) == 1
    release.set()
    deadline = time.time() + 3
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert len(calls) >= 2          # again when the abandoned worker finally ends


def test_subprocess_timeout_removes_partial_mp4(tmp_path):
    old = tmp_path / "sd_1.mp4"
    old.write_bytes(b"x")
    os.utime(old, (1, 1))            # an older read's file is not ours to remove

    def run(argv, **kw):
        (tmp_path / "sd_100.mp4").write_bytes(b"partial")
        raise subprocess.TimeoutExpired(argv, 1)
    cfg = SimpleNamespace(host="h", name="c", user_env="", password_env="",
                          cloud_password_env="", rotate=0)
    assert sdclip.fetch_sd_frames_subprocess(cfg, 100, out_dir=str(tmp_path), run=run,
                                             python="py", timeout=1) == []
    assert not (tmp_path / "sd_100.mp4").exists()
    assert old.exists()


# 3. unique snap names --------------------------------------------------------------

def test_snap_paths_unique_within_same_millisecond(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot._time, "time", lambda: 1234.5)
    a = snapshot._unique_snap_path(str(tmp_path))
    b = snapshot._unique_snap_path(str(tmp_path))
    assert a != b

    paths = []

    def fake_run(argv, **kw):
        paths.append(argv[-1])
    for _ in range(2):
        snapshot.capture_rtsp("rtsp://x", out_dir=str(tmp_path), _run=fake_run)
    assert len(set(paths)) == 2


# 4. newest recording lookup ---------------------------------------------------------

def _old_walk(cam_dir):
    latest, latest_mtime = None, -1.0
    for dirpath, _d, names in os.walk(cam_dir):
        for name in names:
            if not name.lower().endswith((".mkv", ".mp4", ".ts")):
                continue
            path = os.path.join(dirpath, name)
            m = os.path.getmtime(path)
            if m > latest_mtime:
                latest, latest_mtime = path, m
    return latest


def test_newest_recording_matches_full_walk(tmp_path):
    cam = tmp_path / "cam"
    t = 1_700_000_000
    n = 0
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        for hour in ("08", "09", "10"):
            d = cam / day / hour
            d.mkdir(parents=True)
            for seg in range(3):
                p = d / f"zaznam_{day}_{hour}_{seg}.mkv"
                p.write_bytes(b"x")
                n += 1
                os.utime(p, (t + n * 60, t + n * 60))
    (cam / "2026-10-03" / "10" / "notes.txt").write_text("x")
    (cam / "2026-10-03" / "11").mkdir()          # empty newest hour
    assert snapshot._newest_recording(str(cam)) == _old_walk(str(cam))
    assert snapshot._newest_recording(str(tmp_path / "missing")) is None


def test_newest_recording_does_not_walk_history(tmp_path, monkeypatch):
    cam = tmp_path / "cam"
    for day in ("2026-10-01", "2026-10-02"):
        (cam / day / "10").mkdir(parents=True)
        (cam / day / "10" / "a.mkv").write_bytes(b"x")
    monkeypatch.setattr(snapshot.os, "walk",
                        lambda *a, **k: pytest.fail("walked the whole tree"))
    assert snapshot._newest_recording(str(cam)).endswith("2026-10-02/10/a.mkv")


# 5. orphan job dirs ----------------------------------------------------------------

def test_clean_orphan_reused_pid_by_age(tmp_path, monkeypatch):
    monkeypatch.setattr(sdworker, "_pid_alive", lambda pid: True)
    other = os.getpid() + 1
    old = tmp_path / f"{sdworker.JOB_DIR_PREFIX}{other}_abc"
    fresh = tmp_path / f"{sdworker.JOB_DIR_PREFIX}{other}_def"
    mine = tmp_path / f"{sdworker.JOB_DIR_PREFIX}{os.getpid()}_old"
    for d in (old, fresh, mine):
        d.mkdir()
    ancient = time.time() - sdworker.JOB_DIR_MAX_AGE - 100
    os.utime(old, (ancient, ancient))
    os.utime(mine, (ancient, ancient))
    assert sdworker.clean_orphan_job_dirs(tmp=str(tmp_path)) == 1
    assert not old.exists() and fresh.exists() and mine.exists()


# 6. DST ----------------------------------------------------------------------------

def test_parse_segment_start_is_fold_zero(monkeypatch):
    # 2026-10-25 02:30 happens twice in Europe/Prague (CEST then CET); the first pass wins
    monkeypatch.setenv("TZ", "Europe/Prague")
    time.tzset()
    try:
        got = recclip.parse_segment_start("zaznam_20261025T023000.mkv")
        assert got == 1792888200.0          # 00:30 UTC = 02:30 CEST (fold=0), not 01:30 UTC (1792891800)
        # an unambiguous time on either side is unaffected
        assert recclip.parse_segment_start("zaznam_20261025T013000.mkv") == 1792884600.0
        assert recclip.parse_segment_start("zaznam_20261025T033000.mkv") == 1792895400.0
    finally:
        monkeypatch.undo()
        time.tzset()
