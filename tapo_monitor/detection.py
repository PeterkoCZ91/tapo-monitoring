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

# Named events_1 bits confirmed on C560WS/C260 (docs/tapo-firmware-api-research.md §5).
# Bits the firmware sets that we have NOT yet ground-truthed (3, 7, 8 — correlated with
# alarm_type 4/8/9, likely vehicle/pet/line-crossing AI categories) are reported as
# ``unknown_bits`` rather than guessed at, so logs can map them empirically.
EVENTS_1_BITS = {
    1: "motion",   # value 2   — basic motion (often a false positive on its own)
    5: "pir",      # value 32  — hardware PIR sensor confirmed
    19: "person",  # value 524288 — on-device AI confirmed a person
}

EVENT_TYPES = ("person", "pet", "vehicle", "tamper", "motion")


def decode_events_1(events_1):
    """Decode the getEvents ``events_1`` bitmask into named flags.

    Returns a dict ``{"raw": int, "motion": bool, "pir": bool, "person": bool,
    "unknown_bits": [bit, ...]}``. Bits without a known meaning are listed (not
    discarded) so they can be mapped from logs. Invalid input decodes to all-False.
    """
    try:
        raw = int(events_1)
    except (TypeError, ValueError):
        raw = 0
    flags = {"raw": raw, "motion": False, "pir": False, "person": False, "unknown_bits": []}
    for bit in range(raw.bit_length()):
        if not raw & (1 << bit):
            continue
        name = EVENTS_1_BITS.get(bit)
        if name:
            flags[name] = True
        else:
            flags["unknown_bits"].append(bit)
    return flags

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


def classify_getevent(event_type, has_face=False, strict_people=True, events_1=None):
    """Classify a getEvents() entry. Returns an event type, or '' to skip.

    ``event_type`` is the raw type string; ``has_face`` flags a recognized face_id.
    ``events_1`` is the firmware bitmask: many C560WS events arrive with
    ``event_type=None`` and the only signal is this mask, so we consult it after the
    string heuristics — the AI-person bit always classifies as ``person``. Bare motion
    (no person bit) classifies as ``motion`` in both modes; whether that ends in an
    alert is decided later by run_monitor's Groq gate (strict = confirmed person/animal
    only). Vehicles are always dropped.
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
    # Typeless firmware events: the bitmask is the only signal we have.
    if has_person_bit(events_1):
        return "person"
    # Bare motion (the C560WS AI routinely misses a real person and fires only motion).
    # We no longer blind-drop it under strict; it becomes a candidate that run_monitor
    # funnels through Groq, which alerts only on a confirmed person/animal and drops a
    # lone vehicle/empty frame. ``strict_people`` now governs that alert-time gate.
    return "motion"
