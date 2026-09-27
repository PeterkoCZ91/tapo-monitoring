"""Detection-off notices and following the Tapo app's notification switch (roadmap 10.8)."""

import logging

import pytest

from tapo_monitor import config as cfg
from tapo_monitor import daemon, runtime_state

HOUR = 3600


def _app(**camera):
    base = {"name": "front", "host": "203.0.113.10", "detection_notice": True}
    base.update(camera)
    return cfg.load_config_from_dict({"cameras": [base]})


def _run(app, state, sent, *, motion=None, person=None, now=1000.0, ok=True):
    state.detection_seen = {"front": {"motion": motion, "person": person}}
    daemon.detection_notice_pass(app, state, secrets={}, now=now,
                                 send_text=lambda text: sent.append(text) or ok)


# ── config ───────────────────────────────────────────────────────────────────

def test_both_switches_default_off_and_reject_non_booleans():
    camera = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    assert camera.detection_notice is False and camera.follow_app_notifications is False
    for key in ("detection_notice", "follow_app_notifications"):
        with pytest.raises(cfg.ConfigError, match=key):
            cfg.load_config_from_dict(
                {"cameras": [{"name": "a", "host": "203.0.113.10", key: "yes"}]})


# ── motion detection: never re-asserted, so plain off / on ───────────────────

def test_motion_off_is_announced_once_and_on_again():
    app, state, sent = _app(), daemon.MonitorState(), []
    _run(app, state, sent, motion=True, person=True)      # fresh start, all on: quiet
    _run(app, state, sent, motion=False, person=True)
    _run(app, state, sent, motion=False, person=True)     # still off: no repeat
    _run(app, state, sent, motion=None, person=None)      # unread pass: no decision
    _run(app, state, sent, motion=True, person=True)
    assert len(sent) == 2
    assert "🚫" in sent[0] and "motion detection" in sent[0]
    assert "✅" in sent[1] and "motion detection" in sent[1]


def test_a_fresh_start_with_motion_off_is_announced():
    app, state, sent = _app(), daemon.MonitorState(), []
    _run(app, state, sent, motion=False, person=True)
    assert len(sent) == 1 and "motion detection is switched off" in sent[0]


def test_a_failed_send_is_retried_next_pass():
    app, state, sent = _app(), daemon.MonitorState(), []
    _run(app, state, sent, motion=False, ok=False)
    _run(app, state, sent, motion=False)
    _run(app, state, sent, motion=False)
    assert len(sent) == 2


def test_cameras_without_the_option_stay_silent():
    app, state, sent = _app(detection_notice=False), daemon.MonitorState(), []
    _run(app, state, sent, motion=False, person=False)
    assert sent == []


# ── person detection: the self-heal turns it back on ──────────────────────────

def test_a_restored_person_switch_is_one_message_not_an_off_on_pair():
    app, state, sent = _app(), daemon.MonitorState(), []
    _run(app, state, sent, motion=True, person=True)
    _run(app, state, sent, motion=True, person="restored")   # found off, switched back
    _run(app, state, sent, motion=True, person=True)         # next pass: on, as announced
    assert len(sent) == 1
    assert "↩️" in sent[0] and "switched it back on" in sent[0]


def test_restorations_are_capped_to_one_notice_a_day_and_counted():
    app, state, sent = _app(), daemon.MonitorState(), []
    for minute in range(10):         # something turns it off before every control pass
        _run(app, state, sent, person="restored", now=1000.0 + 60 * minute)
        _run(app, state, sent, person=True, now=1030.0 + 60 * minute)
    assert len(sent) == 1
    _run(app, state, sent, person="restored", now=1000.0 + 25 * HOUR)
    assert len(sent) == 2
    assert "9 more time(s)" in sent[1]


def test_a_person_switch_the_self_heal_cannot_restore_is_off_then_on():
    app, state, sent = _app(), daemon.MonitorState(), []
    _run(app, state, sent, person=False)
    _run(app, state, sent, person=False)
    _run(app, state, sent, person=True)
    assert len(sent) == 2
    assert "person detection is off and was not switched back on" in sent[0]
    assert "person detection is back on" in sent[1]


def test_a_restoration_after_an_announced_off_is_never_held():
    app, state, sent = _app(), daemon.MonitorState(), []
    _run(app, state, sent, person="restored", now=1000.0)
    _run(app, state, sent, person=False, now=1100.0)        # self-heal refused for a while
    _run(app, state, sent, person="restored", now=1200.0)   # inside the daily cap
    assert len(sent) == 3 and "switched it back on" in sent[2]


def test_an_undelivered_restoration_is_sent_on_the_next_pass():
    app, state, sent = _app(), daemon.MonitorState(), []
    _run(app, state, sent, person="restored", ok=False)
    _run(app, state, sent, person=True)          # the state is on now, the notice is owed
    _run(app, state, sent, person=True)
    assert len(sent) == 2 and all("switched it back on" in t for t in sent)


def test_the_announced_state_and_the_daily_cap_survive_a_restart(tmp_path):
    app, state, sent = _app(), daemon.MonitorState(), []
    _run(app, state, sent, motion=False, person="restored")
    assert len(sent) == 2
    path = tmp_path / "runtime.json"
    assert runtime_state.save(path, runtime_state.snapshot(state), now=1000)
    restarted = daemon.MonitorState()
    runtime_state.load(path, restarted, now=1010)
    _run(app, restarted, sent, motion=False, person="restored", now=1100.0)
    assert len(sent) == 2                         # same motion state; restore capped
    _run(app, restarted, sent, motion=True, person=True, now=1200.0)
    assert len(sent) == 3 and "motion detection is back on" in sent[2]


def test_runtime_state_drops_malformed_detection_entries(tmp_path):
    state = daemon.MonitorState()
    state.detection_announced = {"a": {"motion": "off", "person": True, "restored_at": "x",
                                       "restored_repeats": -1}, "b": "junk"}
    assert runtime_state.snapshot(state)["detection_announced"] == {"a": {"person": True}}


# ── the control pass reads before it re-asserts ──────────────────────────────

class Cam:
    def __init__(self, motion="on", person="on", refuse=False, pushes=None):
        self.motion, self.person, self.refuse = motion, person, refuse
        self.pushes = pushes if pushes is not None else {"notification_enabled": "on"}
        self.calls = []

    def getMotionDetection(self):
        self.calls.append("getMotionDetection")
        return {"enabled": self.motion}

    def getPersonDetection(self):
        self.calls.append("getPersonDetection")
        return {"enabled": self.person}

    def setPersonDetection(self, enabled, **_):
        self.calls.append("setPersonDetection")
        if self.refuse:
            raise Exception("-40106")
        self.person = "on" if enabled else "off"

    def getNotificationsEnabled(self):
        self.calls.append("getNotificationsEnabled")
        if isinstance(self.pushes, Exception):
            raise self.pushes
        return self.pushes


def _static(**camera):
    return _app(role="static", tracking={"day_preset": None, "night_preset": None},
                **camera)


def test_person_is_read_before_the_self_heal_and_reported_restored():
    cam = Cam(person="off")
    seen = {}
    daemon.run_once(_static(), now=1000, connect=lambda c: (cam, None),
                    is_night=lambda: False, is_raining=lambda *a, **k: False,
                    detection_seen=seen)
    assert cam.calls.index("getPersonDetection") < cam.calls.index("setPersonDetection")
    assert seen == {"front": {"motion": True, "person": "restored"}}
    assert cam.person == "on"


def test_a_refused_self_heal_leaves_person_reported_off():
    cam = Cam(person="off", refuse=True)
    seen = {}
    daemon.run_once(_static(), now=1000, connect=lambda c: (cam, None),
                    is_night=lambda: False, is_raining=lambda *a, **k: False,
                    detection_seen=seen)
    assert seen["front"]["person"] is False


def test_a_self_heal_the_policy_forbids_leaves_person_reported_off():
    app = cfg.load_config_from_dict({
        "reliability": {"enabled": True, "auto_fix": True, "allowed_repairs": ["smarttrack"]},
        "cameras": [{"name": "front", "host": "203.0.113.10", "detection_notice": True,
                     "role": "static",
                     "tracking": {"day_preset": None, "night_preset": None}}]})
    cam = Cam(person="off")
    seen = {}
    daemon.run_once(app, now=1000, connect=lambda c: (cam, None),
                    is_night=lambda: False, is_raining=lambda *a, **k: False,
                    detection_seen=seen)
    assert "setPersonDetection" not in cam.calls
    assert seen["front"]["person"] is False


def test_cameras_without_the_options_get_no_extra_reads():
    cam = Cam()
    daemon.run_once(_static(detection_notice=False), now=1000,
                    connect=lambda c: (cam, None), is_night=lambda: False,
                    is_raining=lambda *a, **k: False, detection_seen={}, app_push_seen={})
    assert cam.calls == ["setPersonDetection"]


def test_an_odd_detection_answer_is_not_known_rather_than_off():
    class Odd(Cam):
        def getMotionDetection(self):
            return {"enabled": 1}

        def getPersonDetection(self):
            raise Exception("-40210")

    assert daemon.read_detection(Odd()) == {"motion": None, "person": None}
    assert daemon.read_detection(object()) == {"motion": None, "person": None}


# ── the Tapo app's notification switch ───────────────────────────────────────

def test_the_app_switch_is_read_only_for_cameras_that_follow_it():
    cam = Cam(pushes={"notification_enabled": "off", "rich_notification_enabled": "off"})
    seen = {}
    daemon.run_once(_static(detection_notice=False, follow_app_notifications=True),
                    now=1000, connect=lambda c: (cam, None), is_night=lambda: False,
                    is_raining=lambda *a, **k: False, app_push_seen=seen)
    assert seen == {"front": False}


@pytest.mark.parametrize("answer", [
    {}, {"notification_enabled": True}, "off", [], Exception("-40210"),
])
def test_an_unknown_app_answer_never_silences_and_is_logged_once(answer, caplog):
    cam = Cam(pushes=answer)
    app = _static(detection_notice=False, follow_app_notifications=True)
    seen = {}
    with caplog.at_level(logging.DEBUG, logger="tapo_monitor.daemon"):
        for _ in range(3):
            daemon.run_once(app, now=1000, connect=lambda c: (cam, None),
                            is_night=lambda: False, is_raining=lambda *a, **k: False,
                            app_push_seen=seen)
    assert seen == {}
    if not isinstance(answer, Exception):
        assert caplog.text.count("not understood") == 1


def test_extra_app_fields_are_logged_once_and_the_switch_still_read(caplog):
    with caplog.at_level(logging.INFO, logger="tapo_monitor.daemon"):
        for _ in range(3):
            assert daemon.app_notifications_from_reading(
                {"notification_enabled": "off", "push_plan": "0000-0600"}, "front") is False
    assert caplog.text.count("push_plan") == 1


def _send(camera, tmp_path, monkeypatch):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8frame")
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(tmp_path / "sent"))
    posted = []
    monkeypatch.setattr(daemon.notify, "send_photo", lambda *a, **k: posted.append(a) or True)
    ok = daemon.send_alert_photo(camera, {"telegram_token": "t", "telegram_chat": "c"},
                                 str(image), "caption", score=0.9, send_path="live")
    return ok, posted


def test_a_silenced_app_records_the_alert_without_sending(tmp_path, monkeypatch):
    app = _app(detection_notice=False, follow_app_notifications=True)
    state = daemon.MonitorState(app_push_seen={"front": False})
    assert daemon.sync_app_silence(app, state) == {"front"}
    ok, posted = _send(app.cameras[0], tmp_path, monkeypatch)
    assert ok is True and posted == []
    assert '"camera": "front"' in (tmp_path / "sent" / "index.jsonl").read_text()


def test_an_unread_or_unfollowed_app_switch_still_sends(tmp_path, monkeypatch):
    app = _app(detection_notice=False, follow_app_notifications=True)
    assert daemon.sync_app_silence(app, daemon.MonitorState()) == set()   # unread
    assert len(_send(app.cameras[0], tmp_path, monkeypatch)[1]) == 1
    other = _app(detection_notice=False)                                  # not followed
    assert daemon.sync_app_silence(other, daemon.MonitorState(
        app_push_seen={"front": False})) == set()
    assert len(_send(other.cameras[0], tmp_path, monkeypatch)[1]) == 1
