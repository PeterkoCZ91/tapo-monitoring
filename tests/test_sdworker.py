"""SD/recording follow-ups read off the main loop (``tapo_monitor.sdworker``).

The worker itself (threads per camera, results, orphan cleanup) and what the drain does
with it: an entry stays queued while its job runs, is never submitted twice, is decided
on the loop when its result is back — with the alert gate asked again — and its temp dir
goes whatever the outcome.
"""

import os
import threading
import time

import pytest

from tapo_monitor import config as cfg
from tapo_monitor import daemon, runtime_state, sdworker

SECRETS = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c", "face_names": {}}


@pytest.fixture(autouse=True)
def _job_dirs_under_tmp(monkeypatch, tmp_path):
    """Job dirs a test leaves in flight on purpose land in its own tmp, not /tmp."""
    real = sdworker.job_dir
    monkeypatch.setattr(sdworker, "job_dir", lambda tmp=None: real(tmp or str(tmp_path)))


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


class ManualWorker(sdworker.SdWorker):
    """Holds every job until the test says ``finish``: a download that takes its time."""

    def __init__(self):
        super().__init__()
        self.held = []

    def _enqueue(self, camera, key, fn):
        self.held.append((camera, key, fn))

    def finish(self, camera=None):
        for item in [i for i in self.held if camera in (None, i[0])]:
            self.held.remove(item)
            self._done.put(sdworker.run_job(item[1], item[2]))


# ── the worker ───────────────────────────────────────────────────────────────

def test_jobs_of_one_camera_never_overlap_and_other_cameras_do_not_wait():
    worker = sdworker.SdWorker()
    release = threading.Event()
    running, peak, order = [], [], []
    lock = threading.Lock()

    def job(name, block=False):
        def run():
            with lock:
                running.append(name)
                peak.append(sum(1 for r in running if r.startswith("a")))
            if block:
                assert release.wait(5)
            with lock:
                running.remove(name)
                order.append(name)
            return name
        return run

    worker.submit("a", 1, job("a1", block=True))
    worker.submit("a", 2, job("a2"))
    worker.submit("b", 3, job("b1"))
    _wait(lambda: "b1" in order)                  # camera b ran while a1 still blocks
    assert order == ["b1"] and worker.busy("a")
    release.set()
    _wait(lambda: len(order) == 3)
    assert order.index("a1") < order.index("a2")
    assert max(peak) == 1                          # never two reads of camera a at once
    _wait(lambda: worker._done.qsize() == 3)
    done = {f.key: f.value for f in worker.poll()}
    assert done == {1: "a1", 2: "a2", 3: "b1"}
    assert not worker.busy("a") and worker.in_flight() == 0
    worker.shutdown(wait=True)


def test_a_failing_job_comes_back_as_a_result_and_the_thread_keeps_working():
    worker = sdworker.SdWorker()
    boom = RuntimeError("card gone")

    def fail():
        raise boom

    worker.submit("a", 1, fail, context="first")
    worker.submit("a", 2, lambda: "ok", context="second")
    _wait(lambda: worker._done.qsize() == 2)
    finished = sorted(worker.poll(), key=lambda f: f.key)
    assert (finished[0].error, finished[0].context) == (boom, "first")
    assert (finished[1].value, finished[1].error) == ("ok", None)
    worker.shutdown(wait=True)


def test_a_key_is_pending_until_polled_and_cannot_be_submitted_twice():
    worker = ManualWorker()
    worker.submit("a", 7, lambda: 1)
    assert worker.pending(7) and worker.busy("a")
    with pytest.raises(ValueError):
        worker.submit("a", 7, lambda: 1)
    worker.finish()
    assert worker.pending(7)                       # finished, not yet collected
    assert [f.key for f in worker.poll()] == [7]
    assert not worker.pending(7) and not worker.busy("a")


def test_shutdown_does_not_wait_for_a_download_in_progress():
    worker = sdworker.SdWorker()
    release = threading.Event()
    started = threading.Event()

    def slow():
        started.set()
        release.wait(5)

    worker.submit("a", 1, slow)
    assert started.wait(5)
    began = time.monotonic()
    worker.shutdown(wait=False)
    assert time.monotonic() - began < 0.5
    assert all(t.daemon for t in worker._threads)  # interpreter exit does not join them
    release.set()


def test_inline_worker_answers_on_the_next_poll():
    worker = sdworker.InlineSdWorker()
    worker.submit("a", 1, lambda: "frames")
    assert not worker.busy("a")
    assert [f.value for f in worker.poll()] == ["frames"]


def test_orphaned_job_dirs_of_a_stopped_daemon_are_removed(tmp_path, monkeypatch):
    dead, alive = 999_999, os.getpid()
    monkeypatch.setattr(sdworker, "_pid_alive", lambda pid: pid == alive)
    for name in (f"sdjob_{dead}_x1", f"sdjob_{alive}_x2", "sdjob_old", "sdjob_new",
                 "other_dir"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "seg.mp4").write_bytes(b"x")
    now = time.time()
    os.utime(tmp_path / "sdjob_old", (now - 7200, now - 7200))

    assert sdworker.clean_orphan_job_dirs(str(tmp_path), now=now) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([
        f"sdjob_{alive}_x2", "other_dir", "sdjob_new"])


def test_job_dirs_carry_the_daemon_pid(tmp_path):
    path = sdworker.job_dir(str(tmp_path))
    assert os.path.basename(path).startswith(f"sdjob_{os.getpid()}_")


# ── the drain with a worker ──────────────────────────────────────────────────

def _app(**camera):
    base = {"name": "a", "host": "203.0.113.10", "sd_snapshot": True}
    base.update(camera)
    return cfg.load_config_from_dict({"groq": {}, "cameras": [base]})


def _entry(etype="person", start=1000, **extra):
    return {"camera": "a", "etype": etype, "event": {"start_time": start}, "due_at": 1075,
            "live_sent": True, **extra}


class Reads:
    """A fetch that writes one frame into the job's dir and remembers the dir."""

    def __init__(self, frames=1, error=None):
        self.calls, self.dirs = [], []
        self.frames, self.error = frames, error

    def __call__(self, cfg_, start_time, span=None, out_dir=None, **kw):
        self.calls.append(start_time)
        self.dirs.append(out_dir)
        if self.error is not None:
            raise self.error
        out = []
        for i in range(self.frames):
            path = os.path.join(out_dir, f"f{i}.jpg")
            with open(path, "wb") as f:
                f.write(b"\xff\xd8")
            out.append(path)
        return out


def _drain(monkeypatch, app, state, now, fetch, worker=None, sent=None, snapshots=None,
           send_ok=True):
    sent = [] if sent is None else sent
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda tok, chat, img, cap, **k: sent.append(img) or send_ok)
    monkeypatch.setattr(daemon.enrich, "groq_describe", lambda *a, **k: "Person")

    def snapshot_for(_cfg):
        def snap(cam, ev):
            if snapshots is not None:
                snapshots.append(cam)
            return "/tmp/rtsp.jpg"
        return snap
    daemon.process_pending_sd(app, {"a": object()}, state, now=now, secrets=SECRETS,
                              snapshot_for=snapshot_for, time_str=lambda ev: "T",
                              fetch_frames=fetch, worker=worker)
    return sent


def test_an_entry_being_read_stays_queued_and_is_not_submitted_again(monkeypatch):
    app, state, reads, worker = _app(), daemon.MonitorState(), Reads(), ManualWorker()
    entry = _entry()
    state.pending_sd = [entry]
    sent = _drain(monkeypatch, app, state, 1080, reads, worker)
    assert sent == [] and state.pending_sd == [entry] and len(worker.held) == 1
    _drain(monkeypatch, app, state, 1085, reads, worker, sent)
    _drain(monkeypatch, app, state, 1090, reads, worker, sent)
    assert len(worker.held) == 1 and sent == []   # still one job, nothing read twice
    assert reads.calls == []                     # the held job has not even run yet

    worker.finish()
    _drain(monkeypatch, app, state, 1095, reads, worker, sent)
    assert reads.calls == [1000] and len(sent) == 1 and state.pending_sd == []


def test_an_alert_sent_while_the_window_is_read_drops_the_motion_follow_up(monkeypatch):
    # The gate is asked before the read, as always; but the live pass and the sampler keep
    # running while the job reads, and one of them may alert on the same passage. Asking
    # only before the read would send the passage twice. While that alert's cooldown runs
    # the entry waits (as it would have before the read); once it is over, the gate knows
    # the passage was alerted and the entry goes without a second read.
    app, state, reads, worker = _app(), daemon.MonitorState(), Reads(), ManualWorker()
    entry = _entry("motion", span=30, full_span=30, live_sent=False)
    state.pending_sd = [entry]
    _drain(monkeypatch, app, state, 1080, reads, worker)
    assert len(worker.held) == 1                 # nothing had alerted: the read starts

    _, on_alert = daemon.alert_gate(state, "a", app.alerts.cooldown, 1082)
    on_alert("motion", entry["event"])           # the sampler alerts this passage meanwhile
    worker.finish()
    sent = _drain(monkeypatch, app, state, 1090, reads, worker)
    assert sent == [] and state.pending_sd == [entry]
    assert not os.path.exists(reads.dirs[0])

    _drain(monkeypatch, app, state, 1082 + app.alerts.cooldown + 1, reads, worker, sent)
    assert sent == [] and state.pending_sd == [] and worker.held == []
    assert reads.calls == [1000]


def test_a_restart_while_a_window_is_read_reads_it_again(monkeypatch, tmp_path):
    app, state, reads, worker = _app(), daemon.MonitorState(), Reads(), ManualWorker()
    state.runtime_path = str(tmp_path / "runtime.json")
    state.pending_sd = [_entry()]
    _drain(monkeypatch, app, state, 1080, reads, worker)
    assert runtime_state.save_if_changed(state, 1080)
    # The job never comes back: the daemon stops with it in flight.

    restarted = daemon.MonitorState()
    runtime_state.load(state.runtime_path, restarted, 1100)
    assert restarted.pending_sd == [_entry()]    # nothing about the job was persisted
    sent = _drain(monkeypatch, app, restarted, 1100, reads)   # a fresh (inline) worker
    assert reads.calls == [1000] and len(sent) == 1 and restarted.pending_sd == []


def test_a_failed_job_carries_on_as_a_read_that_produced_nothing(monkeypatch, caplog):
    # A job exception must not reach the loop. For a person with no live frame out, zero
    # checked frames is no evidence of absence: the live RTSP fallback sends.
    app, state, worker = _app(), daemon.MonitorState(), ManualWorker()
    reads = Reads(error=RuntimeError("download crashed"))
    state.pending_sd = [_entry(live_sent=False)]
    _drain(monkeypatch, app, state, 1080, reads, worker)
    worker.finish()
    snapshots = []
    sent = _drain(monkeypatch, app, state, 1090, reads, worker, snapshots=snapshots)
    assert sent == ["/tmp/rtsp.jpg"] and len(snapshots) == 1
    assert state.pending_sd == []
    assert "SD follow-up job failed" in caplog.text
    assert not any(os.path.exists(d) for d in reads.dirs)


def test_one_read_per_camera_at_a_time(monkeypatch):
    app, state, reads, worker = _app(), daemon.MonitorState(), Reads(), ManualWorker()
    first, second = _entry(), _entry(start=1020)
    state.pending_sd = [first, second]
    _drain(monkeypatch, app, state, 1080, reads, worker)
    assert len(worker.held) == 1 and state.pending_sd == [first, second]

    worker.finish()
    sent = _drain(monkeypatch, app, state, 1085, reads, worker)
    assert len(sent) == 1 and state.pending_sd == [second] and len(worker.held) == 1
    worker.finish()
    _drain(monkeypatch, app, state, 1090, reads, worker, sent)
    assert reads.calls == [1000, 1020] and len(sent) == 2 and state.pending_sd == []


@pytest.mark.parametrize("outcome", ["sent", "no_subject", "early_look_retry",
                                     "delivery_failed", "gate", "error", "muted"])
def test_the_job_dir_is_removed_whatever_the_outcome(monkeypatch, outcome):
    app, state, worker = _app(), daemon.MonitorState(), ManualWorker()
    reads = Reads(error=RuntimeError("x") if outcome == "error" else None)
    entry = _entry("motion" if outcome == "gate" else "person")
    if outcome == "early_look_retry":
        entry["rest_span"] = 60
    state.pending_sd = [entry]
    _drain(monkeypatch, app, state, 1080, reads, worker)
    if outcome in ("no_subject", "early_look_retry"):
        monkeypatch.setattr(daemon.notify, "is_empty_scene", lambda desc: True)
    if outcome == "gate":
        _, on_alert = daemon.alert_gate(state, "a", app.alerts.cooldown, 1082)
        on_alert("motion", entry["event"])
    if outcome == "muted":
        monkeypatch.setattr(daemon, "camera_muted", lambda *a: True)
    worker.finish()
    assert os.path.isdir(reads.dirs[0])
    sent = _drain(monkeypatch, app, state, 1090, reads, worker,
                  send_ok=outcome != "delivery_failed")
    assert not os.path.exists(reads.dirs[0])
    assert bool(sent) == (outcome in ("sent", "delivery_failed"))
    kept = ("early_look_retry", "delivery_failed", "gate")   # gate: waits out the cooldown
    assert bool(state.pending_sd) == (outcome in kept)


def test_a_read_that_outlasts_the_cooldown_still_drops_the_alerted_passage(monkeypatch):
    # A camera-card read can take longer than the cooldown: by the time it is back only
    # the passage check still knows the sampler alerted it.
    app, state, reads, worker = _app(), daemon.MonitorState(), Reads(), ManualWorker()
    entry = _entry("motion", span=30, full_span=30, live_sent=False)
    state.pending_sd = [entry]
    _drain(monkeypatch, app, state, 1080, reads, worker)
    _, on_alert = daemon.alert_gate(state, "a", app.alerts.cooldown, 1082)
    on_alert("motion", entry["event"])
    worker.finish()
    sent = _drain(monkeypatch, app, state, 1082 + app.alerts.cooldown + 30, reads, worker)
    assert sent == [] and state.pending_sd == [] and reads.calls == [1000]
