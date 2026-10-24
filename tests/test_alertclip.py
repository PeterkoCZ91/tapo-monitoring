"""Opt-in alert clip: config, queue, job and the ffmpeg command (tapo_monitor.alertclip)."""

import os

import pytest

from tapo_monitor import alertclip
from tapo_monitor import config as cfg

HOST = "203.0.113.10"
SECRETS = {"telegram_token": "T", "telegram_chat": "C"}


def _camera(**extra):
    data = {"name": "front", "host": HOST, **extra}
    return cfg.load_config_from_dict({"cameras": [data]}).cameras[0]


# ---- config ---------------------------------------------------------------------------

def test_clip_options_default_off():
    cam = _camera()
    assert cam.clip_video is False
    assert (cam.clip_seconds, cam.clip_pre_roll, cam.clip_height) == (30, 2, 720)


def test_clip_options_read():
    cam = _camera(clip_video=True, clip_seconds=20, clip_pre_roll=0, clip_height=480)
    assert (cam.clip_video, cam.clip_seconds, cam.clip_pre_roll, cam.clip_height) == (
        True, 20, 0, 480)


@pytest.mark.parametrize("key,bad", [
    ("clip_video", "yes"), ("clip_seconds", 4), ("clip_seconds", 61), ("clip_seconds", True),
    ("clip_pre_roll", -1), ("clip_pre_roll", 11), ("clip_height", 200), ("clip_height", 1440),
    ("clip_height", "720"),
])
def test_clip_options_rejected(key, bad):
    with pytest.raises(cfg.ConfigError, match=key):
        _camera(**{key: bad})


@pytest.mark.parametrize("sources", [["lastalarm"], ["reolink"], ["hubpoll"], ["onvif"]])
def test_clip_video_needs_a_card_camera(sources):
    extra = {"hub_host": "203.0.113.20", "go2rtc_src": "x"} if sources == ["hubpoll"] else {}
    with pytest.raises(cfg.ConfigError, match="clip_video"):
        _camera(clip_video=True, detection={"sources": sources}, **extra)


# ---- pure helpers ---------------------------------------------------------------------

def test_start_from_incident():
    assert alertclip.start_from_incident("front-1792712320") == 1792712320
    assert alertclip.start_from_incident("my-cam-2-1792712320") == 1792712320
    assert alertclip.start_from_incident("front") is None
    assert alertclip.start_from_incident(None) is None
    assert alertclip.start_from_incident("front-abc") is None


def test_due_at_waits_for_the_card_guard():
    cam = _camera(clip_video=True, clip_seconds=30, sd_fresh_guard=40)
    assert alertclip.due_at(cam, 1000) == 1000 + 30 + 40 + alertclip.DUE_MARGIN


def test_transcode_args():
    argv = alertclip.transcode_args("/in/sd.mp4", "/out/clip.mp4", start_offset=3,
                                    seconds=30, height=720)
    assert argv[0] == "ffmpeg"
    assert argv[argv.index("-ss") + 1] == "3"
    assert argv[argv.index("-i") + 1] == "/in/sd.mp4"
    assert argv[argv.index("-t") + 1] == "30"
    assert "scale=-2:720" in argv[argv.index("-vf") + 1]
    assert argv[argv.index("-c:v") + 1] == "libx264"
    assert argv[argv.index("-pix_fmt") + 1] == "yuv420p"
    assert "0:v:0" in argv and "0:a:0?" in argv          # first lens, audio when present
    assert argv[argv.index("-movflags") + 1] == "+faststart"
    assert argv[-1] == "/out/clip.mp4"
    assert argv[argv.index("-protocol_whitelist") + 1] == "file"


def test_transcode_args_never_seeks_negative():
    argv = alertclip.transcode_args("a", "b", start_offset=-4, seconds=30, height=720)
    assert argv[argv.index("-ss") + 1] == "0"


def test_caption():
    assert alertclip.caption("🚶 front 02:14:05") == "🎬 🚶 front 02:14:05"
    assert alertclip.caption("") == "🎬"


# ---- queue ----------------------------------------------------------------------------

def test_note_alert_queues_once_per_incident():
    q = alertclip.ClipQueue()
    cam = _camera(clip_video=True)
    assert q.note_alert(cam, "front-1000", message_id=7, caption="c", now=1010)
    assert not q.note_alert(cam, "front-1000", message_id=9, caption="c", now=1050)
    [entry] = q.entries
    assert entry.reply_to == 7 and entry.start == 1000 and entry.camera == "front"
    assert entry.due_at == alertclip.due_at(cam, 1000)


def test_note_alert_ignores_off_cameras_and_bad_ids():
    q = alertclip.ClipQueue()
    assert not q.note_alert(_camera(), "front-1000", message_id=7, caption="c", now=1)
    assert not q.note_alert(_camera(clip_video=True), "front", message_id=7, caption="c", now=1)
    assert not q.note_alert(_camera(clip_video=True), "front-1000", message_id=None,
                            caption="c", now=1)
    assert q.entries == []


def test_note_alert_skips_an_event_too_old_for_the_card():
    q = alertclip.ClipQueue()
    cam = _camera(clip_video=True)
    assert not q.note_alert(cam, "front-1000", message_id=7, caption="c",
                            now=1000 + alertclip.MAX_AGE + 1)


def test_seen_incidents_are_bounded():
    q = alertclip.ClipQueue(seen_limit=3)
    cam = _camera(clip_video=True)
    for i in range(5):
        q.note_alert(cam, f"front-{1000 + i}", message_id=i + 1, caption="c", now=1000)
    assert len(q.seen) == 3


class FakeWorker:
    def __init__(self, busy=()):
        self.jobs, self._busy = [], set(busy)

    def submit(self, camera, key, fn, context=None):
        self.jobs.append((camera, key, fn, context))

    def busy(self, camera):
        return camera in self._busy

    def pending(self, key):
        return any(k == key for _c, k, _f, _x in self.jobs)


def _app(**extra):
    return cfg.load_config_from_dict(
        {"cameras": [{"name": "front", "host": HOST, "clip_video": True, **extra}]})


def test_drain_submits_only_due_entries_on_an_idle_camera():
    app = _app()
    q = alertclip.ClipQueue()
    q.note_alert(app.cameras[0], "front-1000", message_id=7, caption="c", now=1000)
    entry = q.entries[0]
    worker = FakeWorker()
    q.drain(app, worker, now=entry.due_at - 1, secrets={})
    assert worker.jobs == []
    q.drain(app, FakeWorker(busy={"front"}), now=entry.due_at, secrets={})
    assert entry.submitted is False                          # SD follow-ups go first
    q.drain(app, worker, now=entry.due_at, secrets={})
    [(camera, key, fn, context)] = worker.jobs
    assert camera == "front" and isinstance(context, alertclip.ClipJob) and callable(fn)
    q.drain(app, worker, now=entry.due_at + 5, secrets={})
    assert len(worker.jobs) == 1                              # not submitted twice


def test_drain_drops_stale_and_unknown_camera_entries():
    app = _app()
    q = alertclip.ClipQueue()
    q.note_alert(app.cameras[0], "front-1000", message_id=7, caption="c", now=1000)
    q.drain(app, FakeWorker(), now=1000 + alertclip.MAX_AGE + 1, secrets={})
    assert q.entries == []
    q.note_alert(app.cameras[0], "front-2000", message_id=7, caption="c", now=2000)
    other = cfg.load_config_from_dict({"cameras": [{"name": "back", "host": HOST}]})
    q.drain(other, FakeWorker(), now=2000 + 100, secrets={})
    assert q.entries == []


def _inline_queue():
    return alertclip.ClipQueue(submit_send=lambda fn: fn())


def _queued(q, app, incident="front-1000", now=1000):
    q.note_alert(app.cameras[0], incident, message_id=7, caption="c", now=now)
    return q.entries[-1]


def test_finish_hands_the_read_to_the_sender_and_drain_collects_success(tmp_path):
    app = _app()
    q = _inline_queue()
    entry = _queued(q, app)
    entry.submitted = True
    read = alertclip.ClipRead(str(tmp_path / "raw.mp4"), 998, str(tmp_path))
    delivered = []
    q.finish(alertclip.ClipJob(entry, app.cameras[0], SECRETS), value=read, error=None,
             now=1200, deliver=lambda cfg, e, r, s: delivered.append((e, r, s)) or True)
    assert delivered == [(entry, read, SECRETS)]
    assert q.entries == [entry]                       # outcome is read on the next drain
    q.drain(app, FakeWorker(), now=1201, secrets=SECRETS)
    assert q.entries == []


def test_failed_read_retries_once_then_gives_up():
    app = _app()
    q = _inline_queue()
    entry = _queued(q, app)
    entry.submitted = True
    q.finish(alertclip.ClipJob(entry), value=None, error=None, now=1200)
    assert q.entries == [entry] and entry.submitted is False
    assert entry.due_at == 1200 + alertclip.RETRY_DELAY and entry.tries == 1
    entry.submitted = True
    q.finish(alertclip.ClipJob(entry), value=None, error=RuntimeError("x"), now=1300)
    assert q.entries == []


def test_failed_or_raising_delivery_counts_as_a_try(tmp_path):
    app = _app()
    q = _inline_queue()
    entry = _queued(q, app)
    read = alertclip.ClipRead("raw", 998, str(tmp_path))

    def boom(*a):
        raise RuntimeError("upload")

    q.finish(alertclip.ClipJob(entry, app.cameras[0], SECRETS), value=read, error=None,
             now=1200, deliver=lambda *a: False)
    q.drain(app, FakeWorker(busy={"front"}), now=1200, secrets=SECRETS)
    assert entry.tries == 1 and q.entries == [entry]
    q.finish(alertclip.ClipJob(entry, app.cameras[0], SECRETS), value=read, error=None,
             now=1300, deliver=boom)
    q.drain(app, FakeWorker(busy={"front"}), now=1300, secrets=SECRETS)
    assert q.entries == []


def test_an_entry_in_flight_is_not_dropped_as_too_old():
    app = _app()
    q = _inline_queue()
    entry = _queued(q, app)
    entry.submitted = True
    q.drain(app, FakeWorker(), now=1000 + alertclip.MAX_AGE + 5, secrets=SECRETS)
    assert q.entries == [entry]


def test_card_job_only_reads(tmp_path):
    cam = _camera(clip_video=True)
    entry = alertclip.ClipEntry("front", "front-1000", 1000, 7, "c", 0)

    def fetch(c, start, out_dir, pre_roll, seconds, guard):
        path = os.path.join(out_dir, "sd.mp4")
        open(path, "wb").close()
        return path, 998

    read = alertclip.read_clip(cam, entry, work_dir=str(tmp_path), fetch=fetch)
    assert isinstance(read, alertclip.ClipRead) and read.window_start == 998
    assert os.path.exists(read.raw)                   # kept for the sender
    assert alertclip.read_clip(cam, entry, work_dir=str(tmp_path),
                               fetch=lambda *a: (None, None)) is None
    assert alertclip.read_clip(cam, entry, work_dir=str(tmp_path),
                               fetch=lambda *a: 1 / 0) is None
    assert os.listdir(tmp_path) == [os.path.basename(read.job_dir)]


def test_default_sender_runs_the_delivery_on_the_sender_thread(monkeypatch, tmp_path):
    submitted = []

    class Pool:
        def submit(self, fn):
            submitted.append(fn)

    monkeypatch.setattr(alertclip, "_sender", lambda: Pool())
    app = _app()
    q = alertclip.ClipQueue()
    entry = _queued(q, app)
    read = alertclip.ClipRead("raw", 998, str(tmp_path))
    q.finish(alertclip.ClipJob(entry, app.cameras[0], SECRETS), value=read, error=None,
             now=1200, deliver=lambda *a: True)
    [fn] = submitted
    fn()                                              # what the sender thread runs
    q.drain(app, FakeWorker(), now=1201, secrets=SECRETS)
    assert q.entries == []


# ---- the job --------------------------------------------------------------------------

def test_run_job_fetches_transcodes_sends_and_cleans_up(tmp_path):
    cam = _camera(clip_video=True, clip_seconds=20, clip_pre_roll=2, clip_height=480,
                  sd_fresh_guard=45)
    entry = alertclip.ClipEntry(camera="front", incident="front-1000", start=1000,
                                reply_to=7, caption="🚶 front", due_at=0)
    calls = {}

    def fetch(c, start, out_dir, pre_roll, seconds, guard):
        calls["fetch"] = (c.name, start, pre_roll, seconds, guard)
        path = os.path.join(out_dir, "sd_998.mp4")
        with open(path, "wb") as f:
            f.write(b"raw")
        return path, 998                                     # window starts 2 s early

    def transcode(argv, timeout):
        calls["argv"] = argv
        calls["timeout"] = timeout
        with open(argv[-1], "wb") as f:
            f.write(b"x" * 1000)

    def send(token, chat, path, caption, reply_to=None, duration=None):
        calls["send"] = (token, chat, os.path.basename(path), caption, reply_to, duration)
        return True

    ok = alertclip.run_job(cam, entry, {"telegram_token": "T", "telegram_chat": "C"},
                           work_dir=str(tmp_path), fetch=fetch, transcode=transcode,
                           send=send)
    assert ok is True
    assert calls["fetch"] == ("front", 1000, 2, 20, 45)
    assert calls["argv"][calls["argv"].index("-ss") + 1] == "0"   # 1000-2 - 998
    assert calls["argv"][calls["argv"].index("-t") + 1] == "22"   # pre-roll + seconds
    assert calls["send"] == ("T", "C", "clip.mp4", "🎬 🚶 front", 7, 22)
    assert os.listdir(tmp_path) == []                            # job dir removed


def test_run_job_seeks_into_a_window_that_starts_early(tmp_path):
    cam = _camera(clip_video=True)
    entry = alertclip.ClipEntry("front", "front-1000", 1000, 7, "c", 0)
    seen = {}

    def fetch(c, start, out_dir, pre_roll, seconds, guard):
        path = os.path.join(out_dir, "sd.mp4")
        open(path, "wb").close()
        return path, 990                                     # card segment began at 990

    def transcode(argv, timeout):
        seen["ss"] = argv[argv.index("-ss") + 1]
        with open(argv[-1], "wb") as f:
            f.write(b"x")

    alertclip.run_job(cam, entry, {"telegram_token": "T", "telegram_chat": "C"},
                      work_dir=str(tmp_path), fetch=fetch, transcode=transcode,
                      send=lambda *a, **k: True)
    assert seen["ss"] == "8"                                 # 998 - 990


def test_run_job_without_a_card_read_sends_nothing(tmp_path):
    cam = _camera(clip_video=True)
    entry = alertclip.ClipEntry("front", "front-1000", 1000, 7, "c", 0)
    sent = []
    ok = alertclip.run_job(cam, entry, {"telegram_token": "T", "telegram_chat": "C"},
                           work_dir=str(tmp_path), fetch=lambda *a, **k: (None, None),
                           transcode=lambda *a, **k: None,
                           send=lambda *a, **k: sent.append(a) or True)
    assert ok is False and sent == []
    assert os.listdir(tmp_path) == []


def test_run_job_failed_transcode_or_oversized_clip_sends_nothing(tmp_path):
    cam = _camera(clip_video=True)
    entry = alertclip.ClipEntry("front", "front-1000", 1000, 7, "c", 0)

    def fetch(c, start, out_dir, pre_roll, seconds, guard):
        path = os.path.join(out_dir, "sd.mp4")
        open(path, "wb").close()
        return path, 998

    def failing(argv, timeout):
        raise RuntimeError("ffmpeg died")

    sent = []
    secrets = {"telegram_token": "T", "telegram_chat": "C"}
    assert alertclip.run_job(cam, entry, secrets, work_dir=str(tmp_path), fetch=fetch,
                             transcode=failing, send=lambda *a, **k: sent.append(a)) is False

    def huge(argv, timeout):
        with open(argv[-1], "wb") as f:
            f.truncate(alertclip.MAX_BYTES + 1)

    assert alertclip.run_job(cam, entry, secrets, work_dir=str(tmp_path), fetch=fetch,
                             transcode=huge, send=lambda *a, **k: sent.append(a)) is False
    assert sent == [] and os.listdir(tmp_path) == []


# ---- daemon wiring --------------------------------------------------------------------

@pytest.fixture
def fake_telegram(monkeypatch, tmp_path):
    from tapo_monitor import daemon

    last = {"id": None}
    photos = []

    def send_photo(tok, chat, img, cap, record_id=True, **kw):
        photos.append(cap)
        if record_id:
            last["id"] = 40 + len(photos)
        return True

    monkeypatch.setattr(daemon.notify, "send_photo", send_photo)
    monkeypatch.setattr(daemon.notify, "clear_last_message_id",
                        lambda: last.update(id=None), raising=False)
    monkeypatch.setattr(daemon.notify, "last_message_id", lambda: last["id"], raising=False)
    monkeypatch.setattr(daemon, "_clips", alertclip.ClipQueue())
    monkeypatch.setattr(daemon, "crop_for_subject", lambda cfg, image, *a, **k: image)
    image = tmp_path / "snap.jpg"
    image.write_bytes(b"jpg")
    return daemon, str(image), photos




def test_delivered_alert_queues_a_clip_replying_to_the_photo(fake_telegram):
    daemon, image, _ = fake_telegram
    import time

    cam = _camera(clip_video=True)
    incident = f"front-{int(time.time()) - 5}"
    assert daemon.send_alert_photo(cam, SECRETS, image, "🚶 front", score=0.9,
                                   incident=incident, send_path="live")
    [entry] = daemon._clips.entries
    assert (entry.incident, entry.reply_to, entry.caption) == (incident, 41, "🚶 front")


def test_no_clip_for_off_cameras_or_collect_only(fake_telegram):
    daemon, image, photos = fake_telegram
    import time

    now = int(time.time())
    daemon.send_alert_photo(_camera(), SECRETS, image, "c", score=0.9,
                            incident=f"front-{now}", send_path="live")
    daemon.send_alert_photo(_camera(clip_video=True, telegram_alerts=False), SECRETS, image,
                            "c", score=0.9, incident=f"front-{now - 1}", send_path="live")
    assert photos == ["c"]                                   # collect-only sent nothing
    assert daemon._clips.entries == []


def test_finished_clip_job_goes_back_to_the_clip_queue(fake_telegram, monkeypatch):
    daemon, _image, _ = fake_telegram
    from tapo_monitor import sdworker

    app = _app()
    daemon._clips.note_alert(app.cameras[0], "front-1000", message_id=7, caption="c",
                             now=1000)
    entry = daemon._clips.entries[0]
    daemon._clips.submit_send = lambda fn: fn()
    monkeypatch.setattr(alertclip, "deliver_clip", lambda cfg, e, r, s: True)
    worker = sdworker.InlineSdWorker()
    daemon._clips.drain(app, worker, now=entry.due_at, secrets=SECRETS,
                        read=lambda cfg, e: alertclip.ClipRead("raw", 998, "/nonexistent"))
    state = daemon.MonitorState()
    daemon.process_pending_sd(app, {}, state, now=entry.due_at + 1, secrets=SECRETS,
                              worker=worker)
    daemon._clips.drain(app, worker, now=entry.due_at + 2, secrets=SECRETS)
    assert daemon._clips.entries == []
