import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import detection, monitor


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
