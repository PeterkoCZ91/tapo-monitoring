"""Unified detection classification.

A camera reports activity through three different shapes; this module turns each into a
single event-type vocabulary (``person`` / ``pet`` / ``vehicle`` / ``tamper`` /
``motion``) with pure, testable functions. Transport/parsing (ONVIF zeep, getEvents
polling) lives in the camera layer; here we only classify already-parsed data.

``strict_people`` keeps alerts to confirmed people (and pets/tamper), ignoring generic
motion — the usual setting for unattended night monitoring.
"""

# events_1 bit 19 — the camera's on-device AI confirmed a person.
PERSON_BIT = 524288

EVENT_TYPES = ("person", "pet", "vehicle", "tamper", "motion")

_PERSON_WORDS = ("person", "human", "people")
_VEHICLE_WORDS = ("vehicle", "car")


def has_person_bit(events_1, bit=PERSON_BIT):
    """True if the getEvents events_1 bitmask has the AI-person bit set."""
    try:
        return bool(int(events_1) & bit)
    except (TypeError, ValueError):
        return False


def classify_onvif(topic, items, prop_op="", strict_people=True):
    """Classify a parsed ONVIF event. Returns (triggered, event_type).

    ``items`` is a lowercased name->value dict; ``topic`` a lowercased string.
    """
    topic = (topic or "").lower()
    items = items or {}

    # Vehicles are never alerted.
    if items.get("iscar") == "true":
        return (False, "")
    if items.get("ispeople") == "true":
        return (True, "person")
    if items.get("ispet") == "true":
        return (True, "pet")
    if "tamper" in topic:
        return (True, "tamper")
    if any(w in topic for w in _PERSON_WORDS):
        return (True, "person")
    if items.get("ismotion") == "true" and not strict_people:
        return (True, "motion")
    if not strict_people and any(w in topic for w in ("motion", "analytics", "ruleengine")):
        return (True, "motion")
    # Firmware with no parseable topic/items: "Changed" is a real event, "Initialized"
    # is boot-time state we ignore.
    if prop_op == "changed" and not topic and not items:
        return (True, "motion")
    return (False, "")


def classify_getevent(event_type, has_face=False, strict_people=True):
    """Classify a getEvents() entry. Returns an event type, or '' to skip.

    ``event_type`` is the raw type string; ``has_face`` flags a recognized face_id.
    Under ``strict_people`` a bare ``motion`` classification is dropped (returns '').
    """
    if has_face:
        return "person"
    raw = (event_type or "").lower()
    if any(w in raw for w in _PERSON_WORDS):
        return "person"
    if any(w in raw for w in _VEHICLE_WORDS):
        return ""
    if "pet" in raw:
        return "pet"
    if "tamper" in raw:
        return "tamper"
    return "" if strict_people else "motion"
