import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import detection

# ── has_person_bit ───────────────────────────────────────────────────────────

def test_person_bit_set():
    assert detection.has_person_bit(detection.PERSON_BIT) is True

def test_person_bit_combined_mask():
    assert detection.has_person_bit(detection.PERSON_BIT | 1) is True

def test_person_bit_absent():
    assert detection.has_person_bit(7) is False

def test_person_bit_garbage():
    assert detection.has_person_bit(None) is False


# ── decode_events_1 ──────────────────────────────────────────────────────────

def test_decode_person_plus_motion():
    # 524290 = bit 1 (motion) + bit 19 (person) — the common real-person mask.
    f = detection.decode_events_1(524290)
    assert f["person"] is True and f["motion"] is True
    assert f["pir"] is False and f["unknown_bits"] == []

def test_decode_motion_only():
    f = detection.decode_events_1(2)
    assert f["motion"] is True and f["person"] is False and f["unknown_bits"] == []

def test_decode_pir_and_motion():
    f = detection.decode_events_1(34)  # 32 PIR + 2 motion
    assert f["pir"] is True and f["motion"] is True and f["person"] is False

def test_decode_reports_unknown_bits():
    # 130 = bit 1 (motion) + bit 7 (unmapped AI category, alarm_type 8).
    f = detection.decode_events_1(130)
    assert f["motion"] is True and f["person"] is False
    assert f["unknown_bits"] == [7]

def test_decode_unknown_with_person():
    # 524418 = bit 1 + bit 7 + bit 19: person still recognized, bit 7 surfaced.
    f = detection.decode_events_1(524418)
    assert f["person"] is True and f["unknown_bits"] == [7]

def test_decode_garbage_is_all_false():
    f = detection.decode_events_1(None)
    assert f == {"raw": 0, "motion": False, "pir": False, "person": False, "unknown_bits": []}


# ── classify_onvif ───────────────────────────────────────────────────────────

def test_onvif_people_flag():
    assert detection.classify_onvif("", {"ispeople": "true"}) == (True, "person")

def test_onvif_car_never_alerts():
    assert detection.classify_onvif("", {"iscar": "true", "ispeople": "true"}) == (False, "")

def test_onvif_pet_flag():
    assert detection.classify_onvif("", {"ispet": "true"}) == (True, "pet")

def test_onvif_tamper_topic():
    assert detection.classify_onvif("tns1:tamper/x", {}) == (True, "tamper")

def test_onvif_person_in_topic():
    assert detection.classify_onvif("rule/PeopleDetection", {}) == (True, "person")

def test_onvif_motion_ignored_when_strict():
    assert detection.classify_onvif("", {"ismotion": "true"}, strict_people=True) == (False, "")

def test_onvif_motion_alerts_when_not_strict():
    assert detection.classify_onvif("", {"ismotion": "true"}, strict_people=False) == (True, "motion")

def test_onvif_changed_without_topic_is_motion():
    assert detection.classify_onvif("", {}, prop_op="changed") == (True, "motion")

def test_onvif_initialized_is_ignored():
    assert detection.classify_onvif("", {}, prop_op="initialized") == (False, "")


# ── classify_getevent ────────────────────────────────────────────────────────

def test_getevent_face_is_person():
    assert detection.classify_getevent("motion", has_face=True) == "person"

def test_getevent_person_type():
    assert detection.classify_getevent("humanDetection") == "person"

def test_getevent_vehicle_skipped():
    assert detection.classify_getevent("vehicleDetection") == ""

def test_getevent_pet():
    assert detection.classify_getevent("petDetection") == "pet"

def test_getevent_motion_dropped_when_strict():
    assert detection.classify_getevent("motion", strict_people=True) == ""

def test_getevent_motion_kept_when_not_strict():
    assert detection.classify_getevent("motion", strict_people=False) == "motion"


# ── classify_getevent via events_1 bitmask (firmware reports no event_type) ───

def test_getevent_person_bit_is_person_even_when_typeless():
    # Real C560WS firmware: event_type is None, the AI-person signal is the bit.
    assert detection.classify_getevent(None, events_1=detection.PERSON_BIT) == "person"

def test_getevent_person_bit_kept_under_strict():
    assert detection.classify_getevent(
        None, events_1=detection.PERSON_BIT, strict_people=True
    ) == "person"

def test_getevent_nonperson_motion_bit_dropped_when_strict():
    assert detection.classify_getevent(None, events_1=2, strict_people=True) == ""

def test_getevent_nonperson_motion_bit_kept_when_not_strict():
    assert detection.classify_getevent(None, events_1=2, strict_people=False) == "motion"
