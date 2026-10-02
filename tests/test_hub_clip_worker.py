"""Hub clips downloaded off the main loop (``hub_clip_worker``)."""

import pytest

from tapo_monitor import config as cfg
from tapo_monitor import daemon, runtime_state, sdworker
from tests.test_daemon import (
    _clip,
    _clip_frames,
    _CountingNotify,
    _FakeHub,
    _frames,
    _hub_app,
    _hub_for,
    _hub_secrets,
    _scores,
)


class _DeferredWorker(sdworker.SdWorker):
    """Holds each job until the test runs it; nothing needs a real thread."""

    def __init__(self):
        super().__init__()
        self.held = []

    def _enqueue(self, camera, key, fn):
        self.held.append((camera, key, fn))

    def run_all(self):
        held, self.held = self.held, []
        for _camera, key, fn in held:
            self._done.put(sdworker.run_job(key, fn))


def _pass(app, state, tmp_path, worker, now, hub=None, clip_frame_for=None, **kw):
    hub = hub or _FakeHub()
    daemon.run_hubpoll_pass(
        app, {}, state, now=now, secrets=_hub_secrets(), hub_for=_hub_for(hub),
        frame_for=kw.pop("frame_for", _frames(tmp_path)),
        clip_frame_for=clip_frame_for or _clip_frames(tmp_path), hub_worker=worker, **kw)


def _armed(**overrides):
    app = _hub_app(hub_clip_worker=True, **overrides)
    state = daemon.MonitorState()
    state.hub_cursor["gate"] = 1000
    return app, state


@pytest.fixture
def notify_counter(monkeypatch):
    counter = _CountingNotify()
    monkeypatch.setattr(daemon.notify, "send_photo", counter.send_photo)
    return counter


def test_option_defaults_off_and_must_be_boolean():
    assert _hub_app().cameras[0].hub_clip_worker is False
    assert _hub_app(hub_clip_worker=True).cameras[0].hub_clip_worker is True
    with pytest.raises(cfg.ConfigError):
        _hub_app(hub_clip_worker="yes")


def test_inline_worker_gives_the_same_alert_as_the_loop(notify_counter, tmp_path):
    app, state = _armed()
    hub = _FakeHub(clips=[[_clip(1100)]])
    _pass(app, state, tmp_path, sdworker.InlineSdWorker(), 1200, hub=hub)
    assert notify_counter.photos == 1
    assert state.hub_cursor["gate"] == 1100
    assert state.pending_hub_clips == []


def test_option_off_still_downloads_on_the_loop(notify_counter, tmp_path):
    app = _hub_app()
    state = daemon.MonitorState()
    state.hub_cursor["gate"] = 1000
    worker = _DeferredWorker()
    _pass(app, state, tmp_path, worker, 1200, hub=_FakeHub(clips=[[_clip(1100)]]))
    assert notify_counter.photos == 1
    assert worker.held == [] and state.pending_hub_clips == []


def test_loop_does_not_wait_for_the_download(notify_counter, tmp_path):
    app, state = _armed()
    worker = _DeferredWorker()
    fetch = _clip_frames(tmp_path)
    _pass(app, state, tmp_path, worker, 1200, hub=_FakeHub(clips=[[_clip(1100)]]),
          clip_frame_for=fetch)
    # The clip is queued and the cursor is past it, but nothing was fetched or sent yet.
    assert fetch.calls == [] and notify_counter.photos == 0
    assert state.hub_cursor["gate"] == 1100
    assert [e["start_time"] for e in state.pending_hub_clips] == [1100.0]
    worker.run_all()
    _pass(app, state, tmp_path, worker, 1205, clip_frame_for=fetch)
    assert notify_counter.photos == 1 and state.pending_hub_clips == []


def test_one_download_per_hub_at_a_time(notify_counter, tmp_path):
    app, state = _armed()
    worker = _DeferredWorker()
    hub = _FakeHub(clips=[[_clip(1100), _clip(1130)]])
    _pass(app, state, tmp_path, worker, 1200, hub=hub)
    assert len(worker.held) == 1
    worker.run_all()
    _pass(app, state, tmp_path, worker, 1205, hub=hub)       # collects 1st, submits 2nd
    assert notify_counter.photos == 1 and len(worker.held) == 1
    worker.run_all()
    _pass(app, state, tmp_path, worker, 1210, hub=hub)
    assert notify_counter.photos == 2 and state.pending_hub_clips == []


def test_a_failed_job_falls_back_to_a_live_frame(notify_counter, tmp_path):
    app, state = _armed()
    worker = _DeferredWorker()

    def broken(_cfg, _device):
        def fetch(_clip):
            raise RuntimeError("boom")
        return fetch

    _pass(app, state, tmp_path, worker, 1200, hub=_FakeHub(clips=[[_clip(1100)]]),
          clip_frame_for=broken)
    worker.run_all()
    grabs = _frames(tmp_path)
    _pass(app, state, tmp_path, worker, 1205, clip_frame_for=broken, frame_for=grabs)
    assert notify_counter.photos == 1 and len(grabs.calls) == 1
    assert state.pending_hub_clips == []


def test_clip_is_tried_twice_before_the_live_frame(notify_counter, tmp_path):
    app, state = _armed()
    fetch = _clip_frames(tmp_path, results=[None, "ok"])
    grabs = _frames(tmp_path)
    _pass(app, state, tmp_path, sdworker.InlineSdWorker(), 1200,
          hub=_FakeHub(clips=[[_clip(1100)]]), clip_frame_for=fetch, frame_for=grabs)
    assert len(fetch.calls) == 2 and grabs.calls == []
    assert notify_counter.photos == 1


def test_no_frame_at_all_drops_the_clip_without_an_alert(notify_counter, tmp_path):
    app, state = _armed()
    fetch = _clip_frames(tmp_path, results=[None, None])
    grabs = _frames(tmp_path, results=[None])
    _pass(app, state, tmp_path, sdworker.InlineSdWorker(), 1200,
          hub=_FakeHub(clips=[[_clip(1100)]]), clip_frame_for=fetch, frame_for=grabs)
    assert notify_counter.photos == 0 and state.pending_hub_clips == []


def test_queued_clip_survives_a_restart(notify_counter, tmp_path):
    app, state = _armed()
    worker = _DeferredWorker()
    _pass(app, state, tmp_path, worker, 1200, hub=_FakeHub(clips=[[_clip(1100)]]))
    path = tmp_path / "runtime.json"
    assert runtime_state.save(path, runtime_state.snapshot(state), now=1201)
    # the daemon dies here; its worker and job are gone
    reborn = daemon.MonitorState()
    counts = runtime_state.load(path, reborn, now=1210)
    assert counts["pending_hub_clips"] == 1
    reborn.hub_devices["gate"] = state.hub_devices["gate"]
    reborn.hub_cursor["gate"] = 1100
    fresh = _DeferredWorker()
    _pass(app, reborn, tmp_path, fresh, 1215)
    assert len(fresh.held) == 1
    fresh.run_all()
    _pass(app, reborn, tmp_path, fresh, 1220)
    assert notify_counter.photos == 1 and reborn.pending_hub_clips == []


def test_queue_is_saved_before_the_cursor_moves_past_the_clip(notify_counter, tmp_path,
                                                              monkeypatch):
    app, state = _armed()
    state.runtime_path = str(tmp_path / "runtime.json")
    state.hub_cursor_path = str(tmp_path / "cursor.json")
    order = []
    real_save = runtime_state.save
    real_cursor = daemon.save_hub_cursor

    def save(*a, **k):
        order.append("queue")
        return real_save(*a, **k)

    def save_cursor(*a, **k):
        order.append("cursor")
        return real_cursor(*a, **k)

    monkeypatch.setattr(runtime_state, "save", save)
    monkeypatch.setattr(daemon, "save_hub_cursor", save_cursor)
    _pass(app, state, tmp_path, _DeferredWorker(), 1200, hub=_FakeHub(clips=[[_clip(1100)]]))
    assert order[:2] == ["queue", "cursor"]


def test_job_past_its_deadline_is_replaced_by_a_live_frame(notify_counter, tmp_path):
    app, state = _armed()
    worker = _DeferredWorker()
    _pass(app, state, tmp_path, worker, 1200, hub=_FakeHub(clips=[[_clip(1100)]]))
    late = daemon.HUB_CLIP_JOB_TIMEOUT + 1
    grabs = _frames(tmp_path)
    _pass(app, state, tmp_path, worker, 1200 + late, frame_for=grabs)
    assert notify_counter.photos == 1 and state.pending_hub_clips == []
    # the stuck job finally returns: its frames are cleaned up, nothing is sent twice
    leftover = tmp_path / "late.jpg"
    leftover.write_bytes(b"x")
    worker.held = [(c, k, lambda: [str(leftover)]) for c, k, _ in worker.held]
    worker.run_all()
    _pass(app, state, tmp_path, worker, 1200 + late + 5)
    assert notify_counter.photos == 1 and not leftover.exists()


def test_stale_queue_entry_is_dropped(notify_counter, tmp_path):
    app, state = _armed()
    state.pending_hub_clips.append({"camera": "gate", "start_time": 1100.0,
                                    "end_time": 1111.0, "video_type": "2", "queued_at": 1100})
    _pass(app, state, tmp_path, _DeferredWorker(), 1100 + daemon.HUB_PENDING_TTL + 1)
    assert state.pending_hub_clips == [] and notify_counter.photos == 0


def test_a_clip_is_queued_once(tmp_path):
    app, state = _armed()
    entry = {"camera": "gate", "start_time": 1100.0, "end_time": 1111.0,
             "video_type": "2", "queued_at": 1150}
    state.pending_hub_clips.append(dict(entry))
    _pass(app, state, tmp_path, _DeferredWorker(), 1200, hub=_FakeHub(clips=[[_clip(1100)]]))
    assert len(state.pending_hub_clips) == 1


def test_score_gate_still_applies_to_queued_clips(notify_counter, tmp_path):
    app, state = _armed(scorer={"url": "http://scorer/score", "threshold": 0.5})
    _pass(app, state, tmp_path, sdworker.InlineSdWorker(), 1200,
          hub=_FakeHub(clips=[[_clip(1100)]]), score_for=_scores(0.2))
    assert notify_counter.photos == 0 and state.pending_hub_clips == []


def test_clip_stuck_behind_a_hung_download_still_alerts(notify_counter, tmp_path):
    app, state = _armed()
    worker = _DeferredWorker()
    hub = _FakeHub(clips=[[_clip(1100)], [_clip(1130)]])
    _pass(app, state, tmp_path, worker, 1200, hub=hub)         # 1100 is handed out, hangs
    _pass(app, state, tmp_path, worker, 1230, hub=hub)         # 1130 waits behind it
    assert len(state.pending_hub_clips) == 2 and notify_counter.photos == 0
    # past the job deadline: the hung clip and the one behind it both get a live frame
    _pass(app, state, tmp_path, worker, 1230 + daemon.HUB_CLIP_JOB_TIMEOUT + 1)
    assert state.pending_hub_clips == [] and notify_counter.photos >= 1


def test_loop_step_hands_the_hub_worker_to_the_hub_pass_only_when_given():
    seen = []

    def hubpoll(_app, _clients, _state, **kw):
        seen.append(kw.get("hub_worker", "absent"))

    app = _hub_app()
    marker = object()
    for worker, expected in ((None, "absent"), (marker, marker)):
        daemon.loop_step(
            app, {}, daemon.MonitorState(), now=1000, secrets=_hub_secrets(),
            last_control=1000, control_interval=60, hubpoll=hubpoll, hub_worker=worker,
            monitor=lambda *a, **k: None, drain=lambda *a, **k: None,
            sample=lambda *a, **k: None, guard=lambda *a, **k: None,
            digest=lambda **k: None, late=lambda *a, **k: None,
            is_night=lambda: True)
        assert seen[-1] is expected or seen[-1] == expected
