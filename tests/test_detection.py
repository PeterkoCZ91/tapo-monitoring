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
