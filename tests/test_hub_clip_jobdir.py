"""Temp files of a hub clip job: pid-named, cleaned on every outcome."""

import os
import subprocess
import sys

import pytest

from tapo_monitor import daemon, sdworker
from tests.test_daemon import _hub_app

_DEVICE = {"device_id": "d", "mac": "m"}


def _job_dirs(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("sdjob_"))


def _writes_clip(*args):
    with open(args[6], "wb") as fh:
        fh.write(b"ts")
    return args[6]


def _cut(_clip, out_dir, _duration):
    frames = []
    for i in range(2):
        path = f"{out_dir}/f{i}.jpg"
        with open(path, "wb") as fh:
            fh.write(b"jpg")
        frames.append(path)
    return frames


def _fetch(tmp_path, **kw):
    cfg = _hub_app(hub_clip_worker=True).cameras[0]
    return daemon.fetch_hub_clip_frames(
        cfg, _DEVICE, {"start_time": 100, "end_time": 110}, "pw", tmp=str(tmp_path), **kw)


def test_job_files_live_in_a_pid_named_dir_and_the_clip_is_dropped(tmp_path):
    frames = _fetch(tmp_path, download=_writes_clip, extract=_cut)
    assert len(frames) == 2
    (name,) = _job_dirs(tmp_path)
    assert name.startswith(f"sdjob_{os.getpid()}_")
    assert all(f.startswith(str(tmp_path / name)) for f in frames)
    # clip.ts is gone, the frames stay for the caller
    assert sorted(p.name for p in (tmp_path / name).iterdir()) == ["f0.jpg", "f1.jpg"]


def test_a_failed_job_leaves_nothing_behind(tmp_path):
    def broken(*args):
        _writes_clip(*args)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _fetch(tmp_path, download=broken)
    assert _job_dirs(tmp_path) == []


def test_a_failed_extraction_and_empty_results_leave_nothing_behind(tmp_path):
    def bad_extract(_clip, _dir, _duration):
        raise OSError("ffmpeg gone")

    with pytest.raises(OSError):
        _fetch(tmp_path, download=_writes_clip, extract=bad_extract)
    assert _fetch(tmp_path, download=lambda *a: None) == []
    assert _fetch(tmp_path, download=_writes_clip, extract=lambda *a: []) == []
    assert _job_dirs(tmp_path) == []


def test_a_killed_daemons_job_dir_is_cleared_at_the_next_start(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    orphan = tmp_path / f"sdjob_{dead.pid}_abc"
    orphan.mkdir()
    (orphan / "f0.jpg").write_bytes(b"jpg")
    _fetch(tmp_path, download=_writes_clip, extract=_cut)       # this process's own dir
    assert sdworker.clean_orphan_job_dirs(str(tmp_path)) == 1
    assert not orphan.exists() and len(_job_dirs(tmp_path)) == 1


def test_delivery_removes_the_job_dir_with_its_last_frame(tmp_path):
    frames = _fetch(tmp_path, download=_writes_clip, extract=_cut)
    for f in frames:
        daemon._safe_unlink(f)
    daemon._drop_job_dirs(frames)
    assert _job_dirs(tmp_path) == []


# -- the job dir goes with its frame on every later outcome ------------------------------

def _frame_in_job_dir(tmp_path):
    job = sdworker.job_dir(str(tmp_path))
    path = os.path.join(job, "frame.jpg")
    with open(path, "wb") as fh:
        fh.write(b"jpg")
    return path


@pytest.mark.parametrize("outcome", ["sent", "gave_up", "too_old"])
def test_a_retried_hub_alert_removes_its_job_dir_when_it_is_settled(
        tmp_path, monkeypatch, outcome):
    app = _hub_app()
    state = daemon.MonitorState()
    state.pending_hub.append({
        "camera": "gate", "start_time": 100, "image": _frame_in_job_dir(tmp_path),
        "caption": "c", "score": None, "etype": "motion", "attempts": 1,
        "queued_at": 1000, "due_at": 1000, "audit_extra": {}})
    now = 1000
    if outcome == "too_old":
        now += daemon.HUB_PENDING_TTL + 1
    if outcome == "gave_up":
        state.pending_hub[0]["attempts"] = daemon.HUB_RETRY_MAX_ATTEMPTS - 1
    monkeypatch.setattr(daemon.notify, "send_photo", lambda *a, **k: outcome == "sent")
    daemon.process_pending_hub(app, state, now=now, secrets={
        "telegram_token": "t", "telegram_chat": "c", "groq_key": ""})
    assert state.pending_hub == []
    assert _job_dirs(tmp_path) == []


def test_a_late_hub_clip_result_removes_its_job_dir(tmp_path):
    app = _hub_app()
    state = daemon.MonitorState()
    frame = _frame_in_job_dir(tmp_path)
    worker = sdworker.InlineSdWorker()
    # The entry was already settled or dropped (it is not in the queue any more).
    worker.submit("192.0.2.60", ("gate", 100), lambda: [frame],
                  context={"camera": "gate", "start_time": 100})
    daemon.process_hub_clip_queue(
        app, state, worker, now=1000, secrets={}, frame_for=None, score_for=None,
        time_str=str, clip_frame_for=None, cooldown=0)
    assert not os.path.exists(frame)
    assert _job_dirs(tmp_path) == []
