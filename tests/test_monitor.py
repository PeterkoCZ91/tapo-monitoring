import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import config, detection, monitor


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
        {"cameras": [{"name": "a", "host": "1.1.1.1"}]}).cameras[0]
    wm = monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        on_alert=lambda et: alerted.append(et),
        defer=lambda ev, et, live_sent: deferred.append((ev["start_time"], et, live_sent)))
    assert sent == []                               # no empty live ping
    assert deferred == [(100, "person", False)]     # SD must deliver, live_sent=False
    assert alerted == ["person"]                    # still counts for per-type cooldown
    assert wm == 100


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
        {"cameras": [{"name": "a", "host": "1.1.1.1"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        defer=None)
    assert len(sent) == 1     # safety net: empty live still sent when there's no SD path


def test_run_monitor_no_defer_when_live_has_person(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person at the gate")
    deferred = []

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "1.1.1.1"}]}).cameras[0]
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
        {"cameras": [{"name": "a", "host": "1.1.1.1"}]}).cameras[0]
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
        {"cameras": [{"name": "a", "host": "1.1.1.1"}]}).cameras[0]
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
        {"cameras": [{"name": "a", "host": "1.1.1.1"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: None,   # live grab fails (both attempts)
        time_str=lambda ev: "T",
        defer=lambda ev, et, live_sent: deferred.append((et, live_sent)))
    assert sent == []                       # nothing sent live
    assert deferred == [("person", False)]  # SD follow-up must rescue, live_sent=False


def test_run_monitor_defer_leaves_motion_inline(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "Person walking")
    deferred = []

    class Cam:
        def getEvents(self):
            return [{"start_time": 100, "events_1": 2, "alarm_type": 2}]  # bare motion

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "1.1.1.1"}]}).cameras[0]
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
        {"cameras": [{"name": "a", "host": "1.1.1.1"}]}).cameras[0]
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
        {"cameras": [{"name": "a", "host": "1.1.1.1"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        can_alert=lambda et: False)
    assert len(sent) == 1     # face event alerts despite the active cooldown


def test_cooldown_still_skips_person_without_face(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(monitor.enrich, "groq_describe", lambda *a, **k: "A person")

    class Cam:
        def getEvents(self):
            return [_person_event(100)]

    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "1.1.1.1"}]}).cameras[0]
    monitor.run_monitor(
        Cam(), cfg, 0, now=1000, groq_key="k", telegram_token="t", telegram_chat="c",
        snapshot=lambda cam, ev: "/tmp/live.jpg", time_str=lambda ev: "T",
        can_alert=lambda et: False)
    assert sent == []         # faceless burst duplicate stays cooled down
