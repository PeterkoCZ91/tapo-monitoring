"""Unified detection classification.

A camera reports activity through three different shapes; this module turns each into a
single event-type vocabulary (``person`` / ``pet`` / ``vehicle`` / ``tamper`` /
``motion``) with pure, testable functions. Transport/parsing (ONVIF zeep, getEvents
polling) lives in the camera layer; here we only classify already-parsed data.

``strict_people`` keeps alerts to confirmed people (and pets/tamper), ignoring generic
motion — the usual setting for unattended night monitoring.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

# events_1 bit 19 — the camera's on-device AI confirmed a person.
PERSON_BIT = 524288

# Named events_1 bits confirmed on C560WS/C260 (docs/tapo-firmware-api-research.md §5).
# Bits the firmware sets that we have NOT yet ground-truthed (3, 7 — correlated with
# alarm_type 4/8/9, likely vehicle/pet AI categories) are reported as
# ``unknown_bits`` rather than guessed at, so logs can map them empirically.
EVENTS_1_BITS = {
    1: "motion",   # value 2   — basic motion (often a false positive on its own)
    5: "pir",      # value 32  — hardware PIR sensor confirmed
    8: "linecrossing",  # value 256 — line-crossing detection (informational; never alerts alone)
    19: "person",  # value 524288 — on-device AI confirmed a person
}

EVENT_TYPES = ("person", "pet", "vehicle", "tamper", "motion")


@dataclass(frozen=True)
class EventProfile:
    """What one camera model's ``getEvents`` fields mean.

    The same bit does not mean the same thing on every model: on a C560WS bit 5 (value
    32, with ``alarm_type`` 6) is the hardware PIR, while a C545D has no PIR and sets
    the same pair when its AI saw a person. A profile is picked per camera in the config
    (``event_profile``); ``default`` is the behaviour every existing camera has.

    ``bits``         events_1 bit -> flag name (``motion``/``pir``/``person``/...).
    ``alarm_types``  ``alarm_type`` value -> flag name it also sets, for models where
                     the alarm class carries meaning of its own.
    ``pt_channel``   the channel of a pan/tilt lens the firmware moves by itself (dual-
                     lens linkage); None for a single-lens camera.
    ``linkage_hold`` seconds after an event on ``pt_channel`` during which neither the
                     scheduled preset recall nor the pan-limit guard moves that lens.
    ``channels``     lens channels the twin reads per channel (``chn_id``); empty for a
                     single-lens camera, which keeps its probes exactly as they were.
    """

    name: str
    bits: Mapping[int, str]
    alarm_types: Mapping[int, str] = field(default_factory=dict)
    pt_channel: int | None = None
    linkage_hold: int = 0
    channels: tuple[int, ...] = ()


# One entry per model whose events differ from the default. Amend a row when the audit
# log (``event ... alarm_type=... channels=...``) shows a mapping the table lacks.
EVENT_PROFILES = {
    "default": EventProfile(name="default", bits=EVENTS_1_BITS),
    # Tapo C545D, dual lens (channel 1 fixed wide, channel 2 pan/tilt). Observed, n=4,
    # owner-confirmed against the app: a person walking by is alarm_type 6 with events_1
    # 34 (bits 1 + 5) on both channels and bit 19 NOT set; plain motion is alarm_type 2
    # with events_1 2 on channel 1 only. The model has no PIR, so bit 5 / alarm_type 6
    # are read as the AI person here. The firmware's dual-cam linkage turns the pan/tilt
    # lens after a person by itself; 180 s is the firmware's event split, so the hold
    # outlasts one event segment.
    "c545d": EventProfile(
        name="c545d",
        bits={1: "motion", 5: "person", 8: "linecrossing", 19: "person"},
        alarm_types={6: "person"},
        pt_channel=2,
        linkage_hold=180,
        channels=(1, 2),
    ),
}
DEFAULT_PROFILE = EVENT_PROFILES["default"]


def event_profile(name=None):
    """The :class:`EventProfile` called ``name``; ``None`` or unknown gives the default."""
    if isinstance(name, EventProfile):
        return name
    return EVENT_PROFILES.get(str(name).lower(), DEFAULT_PROFILE) if name else DEFAULT_PROFILE


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_event(event, profile=None):
    """One getEvents entry in the shape the rest of the pipeline reads. Pure.

    Most models put ``events_1`` at the top level. A multi-lens C545D does not: each
    lens reports under ``chn_events: {"<channel>": {"events_1": N, "event_start_time":
    T}}``. When ``chn_events`` is present the result gains ``events_1`` (the OR of every
    channel's mask; a top-level value, if the firmware sends one, wins) and ``channels``
    (the sorted channel numbers that reported), and a missing ``start_time`` falls back
    to the earliest channel start. ``chn_events`` itself is kept.

    A non-default ``profile`` is stamped on the event as ``event_profile`` so every
    later reader (:func:`event_flags`, the sampler's PIR check) decodes the bits the
    same way. An event of the default shape and profile is returned unchanged — the very
    same object — so single-lens cameras see no difference at all. Idempotent.
    """
    if not isinstance(event, Mapping):
        return event
    profile = event_profile(profile)
    chn = event.get("chn_events")
    has_chn = isinstance(chn, Mapping) and bool(chn)
    stamp = profile is not DEFAULT_PROFILE
    if not has_chn and not stamp:
        return event
    out = dict(event)
    if has_chn:
        merged, channels, starts = 0, [], []
        for key, entry in chn.items():
            number = _int_or_none(key)
            if number is None or not isinstance(entry, Mapping):
                continue
            channels.append(number)
            mask = _int_or_none(entry.get("events_1"))
            if mask is not None:
                merged |= mask
            start = _int_or_none(entry.get("event_start_time"))
            if start is not None:
                starts.append(start)
        if channels:
            out["channels"] = sorted(set(channels))
            if event.get("events_1") is None:
                out["events_1"] = merged
            if event.get("start_time") is None and starts:
                out["start_time"] = min(starts)
    if stamp:
        out["event_profile"] = profile.name
    return out


def event_channels(event):
    """The lens channels an event fired on (after :func:`normalize_event`), or ``[]``."""
    channels = event.get("channels") if isinstance(event, Mapping) else None
    return list(channels) if isinstance(channels, (list, tuple)) else []


def decode_events_1(events_1, profile=None, alarm_type=None):
    """Decode the getEvents ``events_1`` bitmask into named flags.

    Returns a dict ``{"raw": int, "motion": bool, "pir": bool, "person": bool,
    "linecrossing": bool, "unknown_bits": [bit, ...]}``. Bits without a known meaning are listed (not
    discarded) so they can be mapped from logs. Invalid input decodes to all-False.

    ``profile`` (an :class:`EventProfile` or its name) picks the model's bit table; the
    default is the C560WS/C260 one. ``alarm_type`` only matters to a profile that maps it.
    """
    profile = event_profile(profile)
    try:
        raw = int(events_1)
    except (TypeError, ValueError):
        raw = 0
    flags = {"raw": raw, "motion": False, "pir": False, "person": False,
             "linecrossing": False, "unknown_bits": []}
    for bit in range(raw.bit_length()):
        if not raw & (1 << bit):
            continue
        name = profile.bits.get(bit)
        if name:
            flags[name] = True
        else:
            flags["unknown_bits"].append(bit)
    alarm = _int_or_none(alarm_type)
    if alarm is not None and alarm in profile.alarm_types:
        flags[profile.alarm_types[alarm]] = True
    return flags


def event_flags(event):
    """:func:`decode_events_1` of a whole (normalized) event under its stamped profile."""
    if not isinstance(event, Mapping):
        return decode_events_1(None)
    return decode_events_1(event.get("events_1"), event.get("event_profile"),
                           event.get("alarm_type"))

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


def classify_getevent(event_type, has_face=False, strict_people=True, events_1=None,
                      profile=None, alarm_type=None):
    """Classify a getEvents() entry. Returns an event type, or '' to skip.

    ``event_type`` is the raw type string; ``has_face`` flags a recognized face_id.
    ``events_1`` is the firmware bitmask: many C560WS events arrive with
    ``event_type=None`` and the only signal is this mask, so we consult it after the
    string heuristics — the AI-person bit always classifies as ``person``. Bare motion
    (no person bit) classifies as ``motion`` in both modes; whether that ends in an
    alert is decided later by run_monitor's Groq gate (strict = confirmed person/animal
    only). Vehicles are always dropped.

    ``profile``/``alarm_type`` decode the mask by the camera model's table (see
    :data:`EVENT_PROFILES`); without them the default table applies, as it always did.
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
    if event_profile(profile) is not DEFAULT_PROFILE and decode_events_1(
            events_1, profile, alarm_type)["person"]:
        return "person"
    # Bare motion (the C560WS AI routinely misses a real person and fires only motion).
    # We no longer blind-drop it under strict; it becomes a candidate that run_monitor
    # funnels through Groq, which alerts only on a confirmed person/animal and drops a
    # lone vehicle/empty frame. ``strict_people`` now governs that alert-time gate.
    return "motion"
