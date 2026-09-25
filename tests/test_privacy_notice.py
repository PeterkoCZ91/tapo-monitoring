"""Privacy-mode notices and data-collection-only cameras (roadmap 10.8)."""

from tapo_monitor import config as cfg
from tapo_monitor import daemon, runtime_state


def _app(**camera):
    base = {"name": "front", "host": "203.0.113.10", "privacy_notice": True}
    base.update(camera)
    return cfg.load_config_from_dict({"cameras": [base]})


def _run(app, state, seen, sent, ok=True):
    state.privacy_seen = {} if seen is None else {"front": seen}
    daemon.privacy_notice_pass(app, state, secrets={},
                               send_text=lambda text: sent.append(text) or ok)


def test_privacy_on_is_announced_once_and_off_again():
    app, state, sent = _app(), daemon.MonitorState(), []
    _run(app, state, False, sent)           # fresh start, lens watching: nothing to say
    _run(app, state, True, sent)
    _run(app, state, True, sent)            # still parked: no repeat
    _run(app, state, None, sent)            # no reading this pass: no decision
    _run(app, state, False, sent)
    assert len(sent) == 2
    assert "🔒" in sent[0] and "privacy mode" in sent[0]
    assert "🔓" in sent[1]


def test_a_fresh_start_with_the_lens_parked_is_announced():
    app, state, sent = _app(), daemon.MonitorState(), []
    _run(app, state, True, sent)
    assert len(sent) == 1 and "🔒" in sent[0]


def test_a_failed_send_is_retried_next_pass():
    app, state, sent = _app(), daemon.MonitorState(), []
    _run(app, state, True, sent, ok=False)
    _run(app, state, True, sent, ok=True)
    _run(app, state, True, sent, ok=True)
    assert len(sent) == 2


def test_cameras_without_the_option_stay_silent():
    app, state, sent = _app(privacy_notice=False), daemon.MonitorState(), []
    _run(app, state, True, sent)
    assert sent == []


def test_the_announced_state_survives_a_restart(tmp_path):
    app, state, sent = _app(), daemon.MonitorState(), []
    _run(app, state, True, sent)
    path = tmp_path / "runtime.json"
    assert runtime_state.save(path, runtime_state.snapshot(state), now=1000)
    restarted = daemon.MonitorState()
    runtime_state.load(path, restarted, now=1010)
    _run(app, restarted, True, sent)        # same state after the restart: no repeat
    assert len(sent) == 1
    _run(app, restarted, False, sent)
    assert len(sent) == 2


def test_the_control_pass_reads_privacy_on_a_static_camera_with_the_option():
    app = _app(role="static", tracking={"day_preset": None, "night_preset": None})
    reads = []

    class Cam:
        def getPrivacyMode(self):
            reads.append(1)
            return {"enabled": "on"}

    seen = {}
    daemon.run_once(app, now=1000, connect=lambda c: (Cam(), None),
                    is_night=lambda: False, is_raining=lambda *a, **k: False,
                    privacy_seen=seen)
    assert reads and seen == {"front": True}


def test_a_static_camera_without_the_option_is_not_asked():
    app = _app(role="static", privacy_notice=False, tracking={"day_preset": None, "night_preset": None})
    reads = []

    class Cam:
        def getPrivacyMode(self):
            reads.append(1)
            return {"enabled": "on"}

    daemon.run_once(app, now=1000, connect=lambda c: (Cam(), None),
                    is_night=lambda: False, is_raining=lambda *a, **k: False,
                    privacy_seen={})
    assert reads == []


def test_telegram_alerts_off_records_the_alert_without_sending(tmp_path, monkeypatch):
    camera = _app(telegram_alerts=False).cameras[0]
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8frame")
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(tmp_path / "sent"))
    posted = []
    monkeypatch.setattr(daemon.notify, "send_photo", lambda *a, **k: posted.append(a) or True)
    assert daemon.send_alert_photo(camera, {"telegram_token": "t", "telegram_chat": "c"},
                                   str(image), "caption", score=0.9,
                                   incident="front-1", send_path="live") is True
    assert posted == []
    index = (tmp_path / "sent" / "index.jsonl").read_text()
    assert '"camera": "front"' in index and '"path": "live"' in index


def test_telegram_alerts_default_still_sends(tmp_path, monkeypatch):
    camera = _app(privacy_notice=False).cameras[0]
    assert camera.telegram_alerts is True
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8frame")
    posted = []
    monkeypatch.setattr(daemon.notify, "send_photo", lambda *a, **k: posted.append(a) or True)
    daemon.send_alert_photo(camera, {"telegram_token": "t", "telegram_chat": "c"},
                            str(image), "caption")
    assert len(posted) == 1
