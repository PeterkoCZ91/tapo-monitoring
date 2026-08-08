import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import config, detection, monitor, snapshot


def test_collect_detections_basic():
    events = [
        {"start_time": 50, "event_type": "personDetection"},   # old, skipped
        {"start_time": 200, "event_type": "personDetection"},  # new person
        {"start_time": 150, "event_type": "vehicleDetection"}, # new vehicle -> skip
    ]
    alertable, watermark = monitor.collect_detections(events, last_seen=100)
    assert [t for _e, t in alertable] == ["person"]
    assert watermark == 200


def test_collect_detections_face_is_person():
    events = [{"start_time": 10, "event_type": "motion",
               "event_info": [{"face_id": 7}]}]
    alertable, _ = monitor.collect_detections(events, last_seen=0)
    assert alertable[0][1] == "person"


def test_collect_detections_person_bit_without_type():
    # The live failure: firmware sends event_type=None, person signal in events_1.
    events = [{"start_time": 10, "events_1": detection.PERSON_BIT}]
    alertable, _ = monitor.collect_detections(events, last_seen=0, strict_people=True)
    assert [t for _e, t in alertable] == ["person"]


def test_collect_detections_strict_funnels_motion():
    # Under strict, bare motion is now a candidate (funnelled through Groq at alert
    # time), not blind-dropped — recovers people the on-device AI misses as motion.
    events = [{"start_time": 10, "event_type": "motion"}]
    alertable, _ = monitor.collect_detections(events, last_seen=0, strict_people=True)
    assert [t for _e, t in alertable] == ["motion"]


def test_collect_detections_keeps_motion_when_not_strict():
    events = [{"start_time": 10, "event_type": "motion"}]
    alertable, _ = monitor.collect_detections(events, last_seen=0, strict_people=False)
    assert [t for _e, t in alertable] == ["motion"]


def test_collect_detections_empty_advances_nothing():
    alertable, watermark = monitor.collect_detections([], last_seen=500)
    assert alertable == []
    assert watermark == 500


def test_collect_detections_watermark_only_from_fresh():
    events = [{"start_time": 50, "event_type": "personDetection"}]  # older than watermark
    alertable, watermark = monitor.collect_detections(events, last_seen=100)
    assert alertable == []
    assert watermark == 100


def _person_event(start=100):
    # events_1 bit19 (524288) + bit1 (2) = camera-confirmed person
    return {"start_time": start, "events_1": 524290, "alarm_type": 2}


def test_run_monitor_defers_without_live_send_when_empty(monkeypatch):
    # Confirmed person but the live frame is empty (mistimed grab or a phantom AI-person
    # fire): don't ping an empty photo now. Defer to the SD follow-up with live_sent=False
    # so it delivers the real event-time frame and, if it finds no subject, an event-time
    # fallback instead of a duplicate empty live ping.
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "empty scene")
    deferred = []
    alerted = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    wm = monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        on_alert=lambda et: alerted.append(et),
        defer=lambda ev, et, live_sent: deferred.append((ev["start_time"], et, live_sent)))
    assert sent == []                               # no empty live ping
    assert deferred == [(100, "person", False)]     # SD must deliver, live_sent=False
    assert alerted == ["person"]                    # still counts for per-type cooldown
    assert wm == 100


def test_run_monitor_drops_empty_person_when_burst_already_sent(monkeypatch):
    # A frame of this same passage already alerted (e.g. bare motion scored above
    # motion_send_threshold seconds earlier): an SD follow-up would just repeat it.
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "empty scene")
    deferred = []
    alerted = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        on_alert=lambda et: alerted.append(et),
        defer=lambda ev, et, live_sent: deferred.append((ev["start_time"], et, live_sent)),
        burst_sent=lambda: True)
    assert sent == []                 # no empty live ping
    assert deferred == []             # no duplicate SD follow-up queued
    assert alerted == []              # nothing delivered -> nothing recorded for cooldown


def test_run_monitor_defers_empty_person_when_burst_unsent(monkeypatch):
    # Existing defer behavior preserved when nothing from this burst has gone out yet.
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "empty scene")
    deferred = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        defer=lambda ev, et, live_sent: deferred.append((ev["start_time"], et, live_sent)),
        burst_sent=lambda: False)
    assert deferred == [(100, "person", False)]


def test_run_monitor_sends_empty_live_when_no_sd(monkeypatch):
    # With SD disabled (defer=None, e.g. the Pi Zero), keep the always-send safety net:
    # a confirmed person with an empty live frame still goes out so we never miss it.
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "empty scene")

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        defer=None)
    assert len(sent) == 1     # safety net: empty live still sent when there's no SD path


def test_run_monitor_no_defer_when_live_has_person(monkeypatch):
    sent = []
    monkeypatch.setattr(
        monitor.notify, "send_photo", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person at the gate")
    deferred = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        defer=lambda ev, et, live_sent: deferred.append(et))
    assert len(sent) == 1     # live sent
    assert deferred == []     # live already has the person -> no SD follow-up


def test_run_monitor_removes_live_snapshot_after_send(monkeypatch, tmp_path):
    image = tmp_path / "live.jpg"
    image.write_bytes(b"jpg")
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person at the gate")

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: str(image), time_str=lambda ev: "T")
    assert len(sent) == 1
    assert not image.exists()


def test_run_monitor_removes_live_snapshot_after_motion_drop(monkeypatch, tmp_path):
    image = tmp_path / "motion.jpg"
    image.write_bytes(b"jpg")
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "empty scene")

    class Cam:
        def getEvents(self):
            return [{"start_time": 100, "events_1": 2, "alarm_type": 2}]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: str(image), time_str=lambda ev: "T")
    assert sent == []
    assert not image.exists()


def test_run_monitor_defers_on_live_snapshot_failure(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo",
                        lambda *a, **k: sent.append(a))
    deferred = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: None,   # live grab fails (both attempts)
        time_str=lambda ev: "T",
        defer=lambda ev, et, live_sent: deferred.append((et, live_sent)))
    assert sent == []                       # nothing sent live
    assert deferred == [("person", False)]  # SD follow-up must rescue, live_sent=False


def test_run_monitor_defer_leaves_motion_inline(monkeypatch):
    sent = []
    monkeypatch.setattr(
        monitor.notify, "send_photo", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person walking")
    deferred = []

    class Cam:
        def getEvents(self):
            return [{"start_time": 100, "events_1": 2, "alarm_type": 2}]  # bare motion

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/x.jpg", time_str=lambda ev: "T",
        defer=lambda ev, et, live_sent: deferred.append(et))
    assert deferred == []        # motion is NOT deferred
    assert len(sent) == 1        # motion sent inline as today


def test_run_monitor_drops_motion_when_groq_blank(monkeypatch):
    # Groq timeout/failure returns '' (not "empty scene"); a bare-motion event must still
    # drop, not send a content-free alert (the night-time false-motion blanks).
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "")

    class Cam:
        def getEvents(self):
            return [{"start_time": 100, "events_1": 2, "alarm_type": 2}]  # bare motion

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/x.jpg", time_str=lambda ev: "T")
    assert sent == []            # blank Groq -> motion dropped, no blank alert


def test_cooldown_overridden_by_recognized_face(monkeypatch):
    # 2026-07-06 18:37:22 live: the camera recognized 3 known faces 40 s after a person
    # alert, and the per-type cooldown silently ate the richest event of the day. A face
    # is new information, not a duplicate ping — it must break through the cooldown.
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Two people")

    event = dict(_person_event(100), event_info=[{"face_id": 7}, {"face_id": 9}])

    class Cam:
        def getEvents(self):
            return [event]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        can_alert=lambda et: False,
        face_names={7: "Alice", 9: "Bob"})
    assert len(sent) == 1     # known face event alerts despite the active cooldown


def test_unknown_face_does_not_override_cooldown(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "A person")

    event = dict(_person_event(100), event_info=[{"face_id": 7}])

    class Cam:
        def getEvents(self):
            return [event]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        can_alert=lambda et: False,
        face_names={})
    assert sent == []         # unknown face burst duplicate stays cooled down


def test_cooldown_still_skips_person_without_face(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "A person")

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        can_alert=lambda et: False)
    assert sent == []         # faceless burst duplicate stays cooled down


def test_cooldown_skip_does_not_feed_sampler():
    observed = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        can_alert=lambda et: False,
        observe=lambda ev, et, sent: observed.append((ev, et, sent)))

    assert observed == []


def _pir_motion_event(start=100):
    # events_1 bit1 (motion) + bit5 (PIR) = physically-near motion, no AI person bit —
    # exactly how the camera reported a woman with children in the yard (2026-07-06).
    return {"start_time": start, "events_1": 34, "alarm_type": 6}


def test_sd_motion_defers_empty_pir_motion(monkeypatch):
    # Live 2026-07-06 16:28-16:57: five PIR-backed motion events with people in the
    # yard were dropped because the live grab missed them and bare motion had no SD
    # second chance. With sd_motion the empty live defers instead of dropping.
    sent, deferred = [], []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "empty scene")

    class Cam:
        def getEvents(self):
            return [_pir_motion_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10", "sd_motion": True,
                      "detection": {"strict_people": False}}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        defer=lambda ev, et, live_sent: deferred.append((et, live_sent)))
    assert sent == []
    assert deferred == [("motion", False)]


def test_sd_motion_ignores_software_only_motion(monkeypatch):
    # events_1=2 (software motion, no PIR) is distant flicker — street traffic, shadows.
    # Deferring those would queue a ~2 min SD download for every passing car; only
    # PIR-backed motion (something physically near) earns the second chance.
    sent, deferred = [], []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "empty scene")

    class Cam:
        def getEvents(self):
            return [{"start_time": 100, "events_1": 2, "alarm_type": 2}]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10", "sd_motion": True,
                      "detection": {"strict_people": False}}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        defer=lambda ev, et, live_sent: deferred.append((et, live_sent)))
    assert sent == [] and deferred == []      # dropped exactly as before


def test_sd_motion_defers_pir_without_motion_bit(monkeypatch):
    # PIR-only events are still hardware-backed motion and deserve the SD second chance.
    sent, deferred = [], []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "empty scene")

    class Cam:
        def getEvents(self):
            return [{"start_time": 100, "events_1": 32, "alarm_type": 6}]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10", "sd_motion": True,
                      "detection": {"strict_people": False}}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        defer=lambda ev, et, live_sent: deferred.append((et, live_sent)))
    assert sent == [] and deferred == [("motion", False)]

def test_motion_empty_still_drops_without_sd_motion(monkeypatch):
    sent, deferred = [], []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "empty scene")

    class Cam:
        def getEvents(self):
            return [_pir_motion_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10",
                      "detection": {"strict_people": False}}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        defer=lambda ev, et, live_sent: deferred.append((et, live_sent)))
    assert sent == [] and deferred == []


def test_run_monitor_raw_mode_sends_motion_without_groq(monkeypatch):
    # enrich.groq=false = raw mode: no AI arbiter, so bare motion must not be dropped
    # as "empty scene" (blank description) — the live frame goes straight to Telegram.
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    groq_calls = []
    monkeypatch.setattr(monitor.enrich, "groq_describe",
                        lambda *a, **k: groq_calls.append(a) or "empty scene")

    class Cam:
        def getEvents(self):
            return [{"start_time": 100, "event_type": "motion"}]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10",
                      "enrich": {"groq": False}}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T")
    assert len(sent) == 1     # raw mode: motion frame sent, not dropped
    assert groq_calls == []   # and Groq never called


def test_run_monitor_raw_mode_sends_person_live_directly(monkeypatch):
    # Raw mode: a confirmed person's live frame is never "empty" -> no SD defer detour.
    sent = []
    monkeypatch.setattr(
        monitor.notify, "send_photo", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "empty scene")
    deferred = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10",
                      "enrich": {"groq": False}}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        defer=lambda ev, et, live_sent: deferred.append(et))
    assert len(sent) == 1
    assert deferred == []

def _motion_event(start=100):
    return {"start_time": start, "event_type": "motion"}


def _cfg_with_scorer(threshold=0.4):
    return config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10",
                      "scorer": {"url": "http://127.0.0.1:8765/score",
                                 "threshold": threshold}}]}).cameras[0]


def test_run_monitor_scorer_sends_motion_above_threshold(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    # Groq is caption-only now: a caption comes back but must not gate the send.
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person walking")

    class Cam:
        def getEvents(self):
            return [_motion_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.9)
    assert len(sent) == 1
    assert "Person walking" in sent[0][3]      # caption present


def test_run_monitor_scorer_drops_motion_below_threshold(monkeypatch):
    sent = []
    groq_calls = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe",
                        lambda *a, **k: groq_calls.append(a) or "whatever")
    observed = []

    class Cam:
        def getEvents(self):
            return [_motion_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.1,
        observe=lambda ev, et, s: observed.append((ev["start_time"], et, s)))
    assert sent == []
    assert groq_calls == []                     # no caption for a dropped frame
    assert observed == [(100, "motion", False)]  # sampler group gets to keep looking


def test_run_monitor_scorer_unavailable_passes_through(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "empty scene")

    class Cam:
        def getEvents(self):
            return [_motion_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: None)
    assert len(sent) == 1                       # raw passthrough, never a silent drop
    assert "empty scene" not in (sent[0][3] or "")  # empty-marker caption suppressed


def test_run_monitor_scorer_person_below_threshold_still_defers(monkeypatch):
    # Camera-confirmed person, scorer says frame is empty -> SD follow-up path, exactly
    # like the Groq-empty case: never dropped outright.
    sent = []
    deferred = []
    alerted = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "x")
    observed = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.1,
        on_alert=lambda et: alerted.append(et),
        defer=lambda ev, et, live_sent: deferred.append((et, live_sent)),
        observe=lambda ev, et, s: observed.append(s))
    assert sent == []
    assert deferred == [("person", False)]
    assert alerted == ["person"]
    assert observed == [True]                   # handed to SD = handled, group stops


def test_run_monitor_observe_reports_sent_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(
        monitor.notify, "send_photo", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person")
    observed = []

    class Cam:
        def getEvents(self):
            return [_motion_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.9,
        observe=lambda ev, et, s: observed.append(s))
    assert observed == [True]


def test_run_monitor_observe_reports_delivered_send(monkeypatch):
    # ``sent`` only means "handled"; the sampler's dedupe guard needs to know whether
    # a photo actually reached Telegram, so a real delivery also reports delivered.
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: True)
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person")
    observed = []

    class Cam:
        def getEvents(self):
            return [_motion_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.9,
        observe=lambda ev, et, sent, delivered=False: observed.append((sent, delivered)))
    assert observed == [(True, True)]


def test_run_monitor_observe_reports_queued_followup_as_undelivered(monkeypatch):
    # Handed to the SD follow-up: nothing has been delivered yet, so a later frame of
    # the same burst must still be allowed its own follow-up.
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: True)
    observed = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.1,
        defer=lambda ev, et, live_sent: None,
        observe=lambda ev, et, sent, delivered=False: observed.append((sent, delivered)))
    assert observed == [(True, False)]


def test_run_monitor_observe_reports_failed_send_as_undelivered(monkeypatch):
    # Telegram refused the photo and the SD retry is queued: handled, not delivered.
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: False)
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person")
    observed = []

    class Cam:
        def getEvents(self):
            return [_motion_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.9,
        defer=lambda ev, et, live_sent: None,
        observe=lambda ev, et, sent, delivered=False: observed.append((sent, delivered)))
    assert observed == [(True, False)]


def test_run_monitor_failed_delivery_does_not_arm_alert_gate(monkeypatch):
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: False)
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person")
    alerted = []
    observed = []

    class Cam:
        def getEvents(self):
            return [_motion_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.9,
        on_alert=lambda et: alerted.append(et),
        observe=lambda ev, et, sent: observed.append(sent))

    assert alerted == []
    assert observed == [False]


def test_run_monitor_failed_delivery_queues_sd_retry(monkeypatch):
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: False)
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person")
    deferred = []
    observed = []

    class Cam:
        def getEvents(self):
            return [_motion_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.9,
        defer=lambda ev, et, live_sent: deferred.append((et, live_sent)),
        observe=lambda ev, et, sent: observed.append(sent))

    assert deferred == [("motion", False)]
    assert observed == [True]


# ── multi-frame corroboration wiring (non-PIR bare motion) ───────────────────

def test_run_monitor_motion_holds_on_corroborate_hold(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "x")
    observed = []

    class Cam:
        def getEvents(self):
            return [_motion_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(threshold=0.3), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.4, corroborate=lambda ev, s: "hold",
        observe=lambda ev, et, s: observed.append((et, s)))
    assert sent == []                         # held, not sent
    assert observed == [("motion", False)]    # group stays open to keep sampling


def test_run_monitor_hold_archives_review_frame(monkeypatch):
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: None)
    reviews = []
    monkeypatch.setattr(monitor.sentlog, "archive_review_if_configured",
                        lambda path, meta, **k: reviews.append((path, meta)))

    class Cam:
        def getEvents(self):
            return [_motion_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(threshold=0.3), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.4, corroborate=lambda ev, s: "hold")
    assert len(reviews) == 1
    assert reviews[0][1]["verdict"] == "hold"
    assert reviews[0][1]["camera"] == "a"


def test_run_monitor_motion_sends_on_corroborate_send(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person")

    class Cam:
        def getEvents(self):
            return [_motion_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(threshold=0.3), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.4, corroborate=lambda ev, s: "send")
    assert len(sent) == 1


def test_run_monitor_pir_motion_ignores_corroborate(monkeypatch):
    # PIR-backed motion is a stronger signal: corroboration must not gate it.
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person")
    called = []

    class Cam:
        def getEvents(self):
            return [_pir_motion_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(threshold=0.3), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.9, corroborate=lambda ev, s: called.append(s) or "hold")
    assert called == []      # corroborate never consulted for PIR motion
    assert len(sent) == 1    # sent on the normal path (0.9 >= threshold)


def test_run_monitor_person_ignores_corroborate(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person")
    called = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    monitor.run_monitor(
        Cam(), _cfg_with_scorer(threshold=0.3), 0, now=1000, groq_key="k",
        telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        score=lambda img: 0.9, corroborate=lambda ev, s: called.append(s) or "hold")
    assert called == []
    assert len(sent) == 1



def test_run_monitor_mute_drains_watermark_without_alerting(monkeypatch):
    # night_only during the day: still poll + advance the watermark (so the day's
    # backlog doesn't replay at nightfall) but grab nothing and send nothing.
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    grabbed = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0]
    wm = monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: grabbed.append(ev) or "/tmp/live.jpg",
        time_str=lambda ev: "T", mute=True)
    assert sent == []       # nothing sent
    assert grabbed == []    # never grabbed a frame
    assert wm == 100        # watermark advanced past the day's events


def test_run_monitor_reports_event_and_media_health(monkeypatch):
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *args, **kwargs: True)
    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}
    ).cameras[0]
    event_health = []
    media_health = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, event: "/tmp/live.jpg", time_str=lambda event: "T",
        poll_observe=event_health.append, media_observe=media_health.append,
    )

    assert event_health == [True]
    assert media_health == [True]


def test_run_monitor_reports_failed_event_poll_without_media_probe():
    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}
    ).cameras[0]
    event_health = []
    media_health = []

    class Cam:
        def getEvents(self):
            raise RuntimeError("camera API unavailable")

    watermark = monitor.run_monitor(
        Cam(), cfg, 123, now=1000, groq_key="", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, event: None, time_str=lambda event: "T",
        poll_observe=event_health.append, media_observe=media_health.append,
    )

    assert watermark == 123
    assert event_health == [False]
    assert media_health == []


def test_monitor_safe_unlink_removes_the_native_twin(tmp_path):
    native = tmp_path / "native.jpg"
    reduced = tmp_path / "reduced.jpg"
    native.write_bytes(b"big")
    reduced.write_bytes(b"small")
    frame = snapshot.Frame(str(reduced), native=str(native), native_width=3840)

    monitor._safe_unlink(frame)

    assert not reduced.exists()
    assert not native.exists(), "live path leaks the native original"
