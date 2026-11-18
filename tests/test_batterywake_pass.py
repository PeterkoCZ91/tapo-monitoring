"""Delivering the events a battery doorbell collected: rings, people, clips, cleanup."""
import os

from tapo_monitor import batterycam, batteryrunner, daemon, runtime_state
from tapo_monitor import config as cfg_mod

SECRETS = {"telegram_token": "t", "telegram_chat": "c", "groq_key": ""}


def _app(cooldown=0, **extra):
    cam = {"name": "door", "host": "192.0.2.70", "detection": {"sources": ["batterywake"]},
           "enrich": {"groq": False}}
    cam.update(extra)
    return cfg_mod.load_config_from_dict({"alerts": {"cooldown": cooldown}, "cameras": [cam]})


def _scorer_app(**kw):
    return _app(scorer={"url": "http://scorer/score", "threshold": 0.5}, **kw)


class FakeRunner:
    def __init__(self, jobs, cursors=None):
        self.jobs, self.cursors = jobs, cursors or {}

    def drain(self):
        jobs, self.jobs = self.jobs, []
        return jobs, dict(self.cursors)


def _job(tmp_path, *, ring=False, alarm_type=18, frames=2, clip=True, name="job"):
    d = tmp_path / name
    d.mkdir()
    paths = []
    for i in range(frames):
        f = d / f"f{i}.jpg"
        f.write_bytes(b"jpg")
        paths.append(str(f))
    mp4 = None
    if clip:
        mp4 = str(d / "clip.mp4")
        open(mp4, "wb").write(b"mp4")
    event = {"start_time": 1000.0, "end_time": 1010.0,
             "events_1": 512 if ring else 8, "alarm_type": alarm_type}
    return batterycam.Job("door", event, paths, mp4, str(d))


def _scores(*values):
    queue = list(values)

    def score_for(_cfg):
        return lambda _image: queue.pop(0) if queue else None
    return score_for


class Sent:
    def __init__(self, monkeypatch, photo_ok=True):
        self.photos, self.videos, self.texts = [], [], []
        monkeypatch.setattr(daemon.notify, "send_photo", self._photo)
        monkeypatch.setattr(daemon.notify, "send_video",
                            lambda t, c, path, cap, reply_to=None, **k:
                            self.videos.append((path, cap, reply_to)) or True)
        monkeypatch.setattr(daemon.notify, "send_text",
                            lambda t, c, text: self.texts.append(text) or True)
        monkeypatch.setattr(daemon.notify, "last_message_id", lambda: 77)
        self.photo_ok = photo_ok

    def _photo(self, token, chat, image, caption, *a, **k):
        self.photos.append(caption)
        return self.photo_ok


def _run(app, jobs, state=None, **kw):
    state = state or daemon.MonitorState()
    daemon.run_batterywake_pass(app, state, FakeRunner(jobs, kw.pop("cursors", None)),
                                now=1200, secrets=SECRETS, time_str=lambda e: "T",
                                **kw)
    return state


def test_ring_sent_without_scorer_gate(monkeypatch, tmp_path):
    sent = Sent(monkeypatch)
    _run(_scorer_app(), [_job(tmp_path, ring=True)], score_for=_scores(0.01, 0.01))
    assert len(sent.photos) == 1
    assert "🔔" in sent.photos[0] and "answered" in sent.photos[0]


def test_ring_ignores_cooldown(monkeypatch, tmp_path):
    sent = Sent(monkeypatch)
    state = daemon.MonitorState()
    state.last_alert[("door", "confirmed")] = 1190
    state.last_alert[("door", "motion")] = 1190
    _run(_app(cooldown=600), [_job(tmp_path, ring=True, alarm_type=17)], state=state)
    assert len(sent.photos) == 1 and "missed" in sent.photos[0]


def test_ring_ignores_the_alert_window_but_detections_do_not(monkeypatch, tmp_path):
    sent = Sent(monkeypatch)
    app = _app(night_only=True)
    _run(app, [_job(tmp_path, ring=True, name="r"), _job(tmp_path, name="m")], night=False)
    assert len(sent.photos) == 1 and "🔔" in sent.photos[0]


def test_ring_without_frames_goes_out_as_text(monkeypatch, tmp_path):
    sent = Sent(monkeypatch)
    _run(_app(), [_job(tmp_path, ring=True, frames=0, clip=False)])
    assert sent.photos == [] and len(sent.texts) == 1 and "🔔" in sent.texts[0]


def test_person_job_goes_through_scorer_gate(monkeypatch, tmp_path):
    sent = Sent(monkeypatch)
    _run(_scorer_app(), [_job(tmp_path, name="a"), _job(tmp_path, name="b")],
         score_for=_scores(0.1, 0.1, 0.9, 0.9))
    assert len(sent.photos) == 1


def test_vehicle_only_job_dropped(monkeypatch, tmp_path):
    # A vehicle-only frame has no person score, so the threshold drops it.
    sent = Sent(monkeypatch)
    _run(_scorer_app(), [_job(tmp_path)], score_for=_scores(0.0, 0.0))
    assert sent.photos == [] and sent.videos == []


def test_clip_sent_as_reply_after_photo(monkeypatch, tmp_path):
    sent = Sent(monkeypatch)
    job = _job(tmp_path)
    clip = job.clip
    _run(_app(), [job])
    assert len(sent.photos) == 1
    assert sent.videos == [(clip, "🎬 " + sent.photos[0], 77)]


def test_no_clip_when_send_clip_false(monkeypatch, tmp_path):
    sent = Sent(monkeypatch)
    _run(_app(battery_wake={"send_clip": False}), [_job(tmp_path)])
    assert len(sent.photos) == 1 and sent.videos == []


def test_no_clip_when_photo_not_delivered(monkeypatch, tmp_path):
    sent = Sent(monkeypatch)
    _run(_scorer_app(), [_job(tmp_path)], score_for=_scores(0.1, 0.1))
    assert sent.videos == []


def test_job_dirs_removed_whatever_happened(monkeypatch, tmp_path):
    Sent(monkeypatch)
    jobs = [_job(tmp_path, name="sent"), _job(tmp_path, name="dropped", ring=False),
            _job(tmp_path, name="ring", ring=True), _job(tmp_path, name="empty", frames=0)]
    dirs = [j.dir for j in jobs]
    _run(_scorer_app(), jobs, score_for=_scores(0.9, 0.9, 0.1, 0.1, 0.2, 0.2))
    assert not any(os.path.exists(d) for d in dirs)


def test_failed_send_keeps_only_the_retry_frame(monkeypatch, tmp_path):
    Sent(monkeypatch, photo_ok=False)
    job = _job(tmp_path, ring=True)
    state = _run(_app(), [job])
    (entry,) = state.pending_hub
    assert os.path.exists(entry["image"])
    assert os.listdir(job.dir) == [os.path.basename(entry["image"])]


def test_cursor_is_copied_to_state_and_persisted(monkeypatch, tmp_path):
    Sent(monkeypatch)
    state = daemon.MonitorState()
    state.runtime_path = str(tmp_path / "runtime.json")
    _run(_app(), [], state=state, cursors={"door": 1234.0})
    assert state.battery_cursor == {"door": 1234.0}
    fresh = daemon.MonitorState()
    runtime_state.load(state.runtime_path, fresh, 1300)
    assert fresh.battery_cursor == {"door": 1234.0}


def test_hubpoll_paths_unchanged(monkeypatch, tmp_path):
    # default path= keeps the hubpoll audit/log strings
    seen = []
    monkeypatch.setattr(daemon.monitor, "audit_event",
                        lambda cfg, event, etype, path, action, **k: seen.append(path))
    monkeypatch.setattr(daemon.notify, "send_photo", lambda *a, **k: True)
    app = _app()
    f = tmp_path / "f.jpg"
    f.write_bytes(b"x")
    daemon._deliver_hub_frames(
        app.cameras[0], daemon.MonitorState(), {"start_time": 1}, {"start_time": 1},
        "motion", str(f), {}, score=None, can_alert=lambda *a: True,
        on_alert=lambda *a, **k: None, secrets=SECRETS, time_str=lambda c: "T", now=2)
    assert seen == ["hubpoll"]


# ── runner ──────────────────────────────────────────────────────────────

def _runner(app, collect, cursors=None):
    return batteryrunner.BatteryRunner(app, cursors=cursors, collect=collect,
                                       client_factory=lambda cfg: (lambda: None),
                                       ping=lambda h: False)


def test_runner_persists_cursor_with_its_jobs(tmp_path):
    job = _job(tmp_path)
    runner = _runner(_app(), lambda cfg, client, cursor, **k: ([job], 1000.0),
                     cursors={"door": 5.0})
    runner.run_collect("door")
    jobs, cursors = runner.drain()
    assert jobs == [job] and cursors == {"door": 1000.0}
    assert runner.drain() == ([], {"door": 1000.0})


def test_runner_cursor_none_first_then_stays(tmp_path):
    seen = []

    def collect(cfg, client, cursor, **k):
        seen.append(cursor)
        return [], 42.0
    runner = _runner(_app(), collect)
    runner.run_collect("door")
    runner.run_collect("door")
    assert seen == [None, 42.0]


def test_runner_wake_is_not_queued_twice_while_busy():
    runner = _runner(_app(), lambda *a, **k: ([], 1.0))
    runner.on_wake("door")
    runner.on_wake("door")
    assert runner.busy("door")
    assert runner._queues["door"].qsize() == 1


def test_runner_worker_runs_collect_and_clears_busy():
    import threading
    done = threading.Event()

    def collect(cfg, client, cursor, **k):
        done.set()
        return [], 9.0
    runner = _runner(_app(), collect, cursors={"door": 1.0})
    runner.start()
    try:
        runner.on_wake("door")
        assert done.wait(5)
        for _ in range(100):
            if not runner.busy("door"):
                break
            threading.Event().wait(0.05)
        assert not runner.busy("door")
    finally:
        runner.stop()


def test_runner_stop_removes_undrained_job_dirs(tmp_path):
    job = _job(tmp_path)
    runner = _runner(_app(), lambda *a, **k: ([job], 1.0))
    runner.run_collect("door")
    runner.stop()
    assert not os.path.exists(job.dir)


def test_loop_step_runs_the_batterywake_pass_only_with_a_runner():
    calls = []
    noop = lambda *a, **k: None  # noqa: E731

    def step(**kw):
        daemon.loop_step(
            _app(), {}, daemon.MonitorState(), now=1200, secrets=SECRETS, last_control=1200,
            control_interval=60, monitor=noop, hubpoll=noop, sample=noop, drain=noop,
            guard=noop, digest=noop, late=noop, is_night=lambda: False,
            batterywake=lambda *a, **k: calls.append(a[2]), **kw)

    step()
    assert calls == []
    step(battery_runner="runner")
    assert calls == ["runner"]
