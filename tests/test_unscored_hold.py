"""scorer.unscored_at_night: hold: no unverified photo at night while the scorer is down."""

import pytest

from tapo_monitor import config, daemon, monitor

HOST = "203.0.113.10"


def _cfg(**scorer):
    return config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": HOST,
                      "scorer": {"url": "http://127.0.0.1:8765/score", "threshold": 0.4,
                                 **scorer}}]}).cameras[0]


def test_default_keeps_the_passthrough():
    assert _cfg().scorer.unscored_at_night == "send"


def test_hold_is_read():
    assert _cfg(unscored_at_night="hold").scorer.unscored_at_night == "hold"


@pytest.mark.parametrize("bad", ["drop", True, 1, ""])
def test_unknown_value_rejected(bad):
    with pytest.raises(config.ConfigError, match="unscored_at_night"):
        _cfg(unscored_at_night=bad)


@pytest.mark.parametrize("value,night,expected", [
    ("send", True, False), ("send", False, False),
    ("hold", True, True), ("hold", False, False),
])
def test_holds_unscored(value, night, expected):
    assert daemon.holds_unscored(_cfg(unscored_at_night=value), night) is expected


def test_holds_unscored_follows_the_camera_schedule():
    cam = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": HOST, "schedule": "always_night",
                      "scorer": {"url": "http://x/score", "unscored_at_night": "hold"}}]}
    ).cameras[0]
    assert daemon.holds_unscored(cam, False) is True


def _run(cfg, event, *, hold, defer=None, held=None):
    sent = []

    class Cam:
        def getEvents(self):
            return [event]

    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: None, defer=defer, hold_unscored=hold,
        send_alert=lambda *a, **k: sent.append(a) or True,
        hold_archive=(lambda *a: held.append(a)) if held is not None else None)
    return sent


def _motion(start=100):
    return {"start_time": start, "event_type": "motion"}


def _person(start=100):
    return {"start_time": start, "events_1": 524290, "alarm_type": 2}


def test_live_unscored_motion_is_held_at_night(monkeypatch):
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: pytest.fail("sent"))
    archived, held = [], []
    monkeypatch.setattr(monitor.sentlog, "archive_review_if_configured",
                        lambda image, meta: archived.append(meta))
    sent = _run(_cfg(unscored_at_night="hold"), _motion(), hold=True, held=held)
    assert sent == []
    [meta] = archived                                    # kept for review, not lost
    assert (meta["verdict"], meta["etype"], meta["person"]) == ("hold", "motion", None)
    assert held == []          # not the sampler's hold: its expiry could send it later


def test_live_unscored_person_goes_to_the_card_follow_up_at_night():
    deferred = []
    sent = _run(_cfg(unscored_at_night="hold"), _person(), hold=True,
                defer=lambda ev, et, live_sent, **k: deferred.append((et, live_sent)) or True)
    assert sent == []
    assert deferred == [("person", False)]               # the card read scores it later


def test_live_unscored_still_passes_through_when_not_held():
    sent = _run(_cfg(), _motion(), hold=False)
    assert len(sent) == 1


# ---- sampler, card follow-up and hub clips ---------------------------------------------

def _sampler_app(hold=True):
    scorer = {"url": "http://127.0.0.1:1/score", "threshold": 0.4}
    if hold:
        scorer["unscored_at_night"] = "hold"
    return config.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": HOST,
                                  "sampler": {"enabled": True, "interval": 30,
                                              "max_frames": 6, "group_gap": 90},
                                  "scorer": scorer}]})


def _run_sampler(app, monkeypatch, *, night):
    sent, archived = [], []
    state = daemon.MonitorState()
    state.groups["a"] = {"camera": "a", "etype": "motion", "event": {"start_time": 1000},
                         "started": 1000, "last_event_at": 1010, "frames": 0,
                         "next_due": 1030, "sent": False}
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda tok, chat, img, cap, **k: sent.append(cap) or True)
    monkeypatch.setattr(daemon.scorer, "score_image", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_safe_unlink", lambda p: None)
    monkeypatch.setattr(daemon.sentlog, "archive_review_if_configured",
                        lambda image, meta: archived.append(meta))
    daemon.process_sampler(app, {"a": object()}, state, now=1035,
                           secrets={"groq_key": "k", "telegram_token": "t",
                                    "telegram_chat": "c", "face_names": {}},
                           snapshot_for=lambda c: (lambda cam, ev: "/tmp/f.jpg"),
                           time_str=lambda ev: "T", night=night)
    return sent, archived, state.groups["a"]


def test_sampler_holds_an_unscored_frame_at_night(monkeypatch):
    sent, archived, group = _run_sampler(_sampler_app(), monkeypatch, night=True)
    assert sent == [] and group["sent"] is False       # the group keeps sampling
    assert archived and archived[0]["person"] is None


def test_sampler_still_passes_through_by_day(monkeypatch):
    sent, _archived, _group = _run_sampler(_sampler_app(), monkeypatch, night=False)
    assert len(sent) == 1 and "(unverified)" in sent[0]


def test_sampler_passes_through_without_the_option(monkeypatch):
    sent, _archived, _group = _run_sampler(_sampler_app(hold=False), monkeypatch, night=True)
    assert len(sent) == 1


def _card_app(hold=True):
    scorer = {"url": "http://x/score", "threshold": 0.4}
    if hold:
        scorer["unscored_at_night"] = "hold"
    return config.load_config_from_dict({"cameras": [
        {"name": "a", "host": HOST, "sd_snapshot": True, "snapshot_source": "sd",
         "scorer": scorer}]})


def _run_card(app, monkeypatch, *, night):
    sent, archived = [], []
    state = daemon.MonitorState()
    state.pending_sd = [{"camera": "a", "etype": "person", "event": {"start_time": 1000},
                         "due_at": 1075, "live_sent": False}]
    monkeypatch.setattr(daemon, "score_for", lambda c: (lambda frame: None))
    monkeypatch.setattr(daemon, "_caption_describe", lambda *a, **k: "")
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda tok, chat, img, cap, **kw: sent.append(cap) or True)
    monkeypatch.setattr(daemon.sentlog, "archive_review_if_configured",
                        lambda image, meta: archived.append(meta))
    daemon.process_pending_sd(
        app, {"a": object()}, state, now=1075,
        secrets={"telegram_token": "t", "telegram_chat": "c", "groq_key": "k"},
        snapshot_for=lambda c: (lambda cam, ev: None), time_str=lambda ev: "T",
        fetch_frames=lambda c, start, **kw: ["/f0.jpg", "/f1.jpg"], night=night)
    return sent, archived, state


def test_card_follow_up_still_unscored_at_night_is_not_sent(monkeypatch):
    sent, archived, state = _run_card(_card_app(), monkeypatch, night=True)
    assert sent == [] and state.pending_sd == []        # dropped, not retried
    assert archived and archived[0]["verdict"] == "hold"


def test_card_follow_up_unscored_by_day_still_goes_out(monkeypatch):
    sent, _archived, _state = _run_card(_card_app(), monkeypatch, night=False)
    assert len(sent) == 1 and "(unverified)" in sent[0]


def test_hub_clip_unscored_at_night_is_held(monkeypatch):
    archived, sent = [], []
    cam = _card_app().cameras[0]
    monkeypatch.setattr(daemon.sentlog, "archive_review_if_configured",
                        lambda image, meta: archived.append(meta))
    monkeypatch.setattr(daemon, "send_alert_photo", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(daemon, "_safe_unlink", lambda p: None)
    daemon._deliver_hub_frames(cam, daemon.MonitorState(), {"start_time": 1000},
                               {"start_time": 1000}, "motion", "/tmp/h.jpg", {},
                               score=lambda frame: None, can_alert=lambda *a: True,
                               on_alert=lambda *a, **k: None, secrets={},
                               time_str=lambda ev: "T", now=1100, hold_unscored=True)
    assert sent == [] and archived and archived[0]["person"] is None
