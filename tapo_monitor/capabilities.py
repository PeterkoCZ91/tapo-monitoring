"""Safe, read-only camera capability snapshots.

The snapshotter operates on an *already connected* pytapo client.  It deliberately
uses only public getter wrappers backed by ``executeFunction``: no login, setter,
``performRequest`` or transport access happens here.  A camera may implement only a
subset of the probes, so every result carries its own state.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

SCHEMA_VERSION = 1
REDACTED = "<redacted>"

# getModuleSpec and getMotorCapability are intentionally absent: pytapo implements
# those wrappers with raw performRequest(), which has caused camera API lockouts on
# tested firmware.  Keep a visible placeholder in the twin instead of calling them.
_UNSAFE_PROBES = (("module", "spec", "raw_transport_disabled"),)

# group, public output name, verified public pytapo getter.  Each call is isolated by
# _probe(), so one firmware-specific failure cannot suppress the rest of the twin.
_SAFE_PROBES = (
    ("basic", "info", "getBasicInfo"),
    ("basic", "clock_correction", "getTimeCorrection"),
    ("firmware", "update_status", "getFirmwareUpdateStatus"),
    ("storage", "sd_card", "getSDCard"),
    ("storage", "record_plan", "getRecordPlan"),
    ("storage", "circular_recording", "getCircularRecordingConfig"),
    ("alerts", "event_types", "getAlertEventType"),
    ("alerts", "led", "getLED"),
    # The one switch that stops a camera watching altogether: privacy mode parks the
    # lens and the camera records nothing, detects nothing and answers every motor
    # call with MOTOR_BUSY. Two cameras sat like that for nine hours on 2026-08-29
    # and nothing in the system could say so, because nothing read it.
    ("privacy", "lens_mask", "getPrivacyMode"),
    ("detection", "motion", "getMotionDetection"),
    ("detection", "person", "getPersonDetection"),
    ("detection", "vehicle", "getVehicleDetection"),
    ("detection", "pet", "getPetDetection"),
    ("detection", "tamper", "getTamperDetection"),
    ("detection", "line_crossing", "getLinecrossingDetection"),
    ("detection", "baby_cry", "getBabyCryDetection"),
    ("detection", "bark", "getBarkDetection"),
    ("detection", "glass_break", "getGlassBreakDetection"),
    ("detection", "meow", "getMeowDetection"),
    ("track", "auto_target", "getAutoTrackTarget"),
    ("track", "smart_config", "getSmartTrackConfig"),
    ("track", "rotation", "getRotationStatus"),
    ("light", "whitelamp_status", "getWhitelampStatus"),
    ("light", "whitelamp_config", "getWhitelampConfig"),
    ("video", "qualities", "getVideoQualities"),
    ("video", "capability", "getVideoCapability"),
    ("video", "ldc", "getLensDistortionCorrection"),
    ("video", "osd", "getOsd"),
)

# Multi-lens cameras only (an event profile with ``channels``, e.g. the C545D): the lens
# layout, the firmware's lens linkage, and the detection switches read per lens with
# ``chn_id``. A getter without ``chn_id`` answers for channel 1 alone, so the pan/tilt
# lens's own detection settings are invisible without these. Same rules as above: public
# executeFunction getters only, read-only, each isolated. A single-lens camera never
# gets these calls.
_DUAL_LENS_PROBES = (
    ("dual_cam", "channels", "getAllChnInfo"),
    ("dual_cam", "capability", "getDualCamCapability"),
    ("dual_cam", "linkage", "getDualCamLinkage"),
    ("dual_cam", "linkage_targets", "getLinkageTargetSetting"),
)
_PER_CHANNEL_PROBES = (
    ("detection_chn", "motion", "getMotionDetection"),
    ("detection_chn", "person", "getPersonDetection"),
    ("detection_chn", "vehicle", "getVehicleDetection"),
    ("detection_chn", "pet", "getPetDetection"),
    ("detection_chn", "tamper", "getTamperDetection"),
)

# ``getSDCard`` ``detect_status`` values that mean the card itself is bad, whatever the
# mount status says. ``dilatant_suspect`` is the firmware's name for a card whose real
# capacity is smaller than it reports (counterfeit or defective): seen on a C545D, where
# such a card also came up read-only and would not format.
SD_CARD_WARNINGS = {"dilatant_suspect": "counterfeit or defective SD card"}

_SENSITIVE_KEYS = {
    "account",
    "alias",
    "auth",
    "authorization",
    "barcode",
    "chat_id",
    "credential",
    "device_alias",
    "device_id",
    "dev_id",
    "email",
    "face_id",
    "host",
    "hostname",
    "hw_id",
    "id",
    "ip",
    "ip_address",
    "latitude",
    "longitude",
    "mac",
    "nickname",
    "oem_id",
    "pass",
    "passwd",
    "password",
    "secret",
    "serial",
    "serial_number",
    "stok",
    "token",
    "username",
}
_MAC_RE = re.compile(r"(?i)(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|\b[0-9a-f]{12}\b")
_IPV4_RE = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
_EMAIL_RE = re.compile(r"(?i)\b[^\s@]+@[^\s@]+\.[^\s@]+\b")


def collect_snapshot(client, channels=()):
    """Return a redacted, JSON-serializable digital twin for ``client``.

    Probe states are ``available``, ``unknown`` (missing or empty response), or
    ``error``.  Error messages are never retained because vendor exceptions may embed
    endpoints, account data or session material.

    ``channels`` (lens channel numbers of a multi-lens camera, from its event profile)
    adds the ``dual_cam`` group and a ``detection_chn`` group read with
    ``chn_id=channels``; empty — every single-lens camera — sends exactly the probes it
    always did.
    """
    channels = [int(c) for c in (channels or ())]
    tables = (*_SAFE_PROBES, *_UNSAFE_PROBES)
    if channels:
        tables = (*tables, *_DUAL_LENS_PROBES, *_PER_CHANNEL_PROBES)
    # Derived from the probe tables rather than listed again: a hand-kept copy of the
    # group names silently desynchronises, and adding a probe in a new group then raises
    # KeyError deep inside the snapshot instead of simply working.
    groups = {group: {} for group, _name, _rest in tables}
    for group, name, method_name in _SAFE_PROBES:
        groups[group][name] = _probe(client, method_name)
    for group, name, reason in _UNSAFE_PROBES:
        groups[group][name] = {"state": "unknown", "reason": reason}
    if channels:
        for group, name, method_name in _DUAL_LENS_PROBES:
            groups[group][name] = _probe(client, method_name)
        for group, name, method_name in _PER_CHANNEL_PROBES:
            groups[group][name] = _probe(client, method_name, chn_id=list(channels))
    return {"schema_version": SCHEMA_VERSION, "groups": groups}


def storage_warnings(snapshot):
    """Human-readable warnings about the SD card itself, from a twin snapshot. Pure.

    Reads ``detect_status`` of every card the ``getSDCard`` probe listed (any model);
    see :data:`SD_CARD_WARNINGS`. Returns a sorted tuple, empty when nothing is wrong or
    the card was not read.
    """
    groups = snapshot.get("groups", {}) if isinstance(snapshot, Mapping) else {}
    storage = groups.get("storage", {}) if isinstance(groups, Mapping) else {}
    result = storage.get("sd_card", {}) if isinstance(storage, Mapping) else {}
    if not isinstance(result, Mapping) or result.get("state") != "available":
        return ()
    found = {SD_CARD_WARNINGS[word] for word in _detect_statuses(result.get("value"))
             if word in SD_CARD_WARNINGS}
    return tuple(sorted(found))


def _detect_statuses(value):
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() == "detect_status":
                yield str(item).lower()
            else:
                yield from _detect_statuses(item)
    elif isinstance(value, list):
        for item in value:
            yield from _detect_statuses(item)


def redact(value):
    """Return a JSON-safe copy with identifiers and credentials removed."""
    return _sanitize(value, seen=set(), depth=0)


def derive_health(snapshot, *, network=None, events=None, rtsp=None):
    """Purely derive layered health from a twin plus independent observations.

    ``network``, ``events`` and ``rtsp`` accept True/False/None.  API health comes
    only from probe outcomes; storage health comes from the SD-card probe and common
    status words.  The function performs no I/O.
    """
    groups = snapshot.get("groups", {}) if isinstance(snapshot, Mapping) else {}
    return {
        "network": _observed_health(network, false_status="down"),
        "api": _api_health(groups),
        "events": _observed_health(events, false_status="degraded"),
        "rtsp": _observed_health(rtsp, false_status="down"),
        "storage": _storage_health(groups),
    }


def _probe(client, method_name, **kwargs):
    method = getattr(client, method_name, None)
    if not callable(method):
        return {"state": "unknown", "reason": "missing_method"}
    try:
        value = method(**kwargs)
    except Exception as exc:  # noqa: BLE001 - isolate every vendor/firmware exception
        return {"state": "error", "error_type": type(exc).__name__}
    if _empty(value):
        return {"state": "unknown", "reason": "empty_response"}
    return {"state": "available", "value": redact(value)}


def _empty(value):
    return value is None or value == "" or isinstance(value, (Mapping, list, tuple, set)) and not value


def _sensitive_key(key):
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith("_id")
        or normalized.endswith("_mac")
        or normalized.endswith("_token")
        or normalized.endswith("_password")
        or normalized.endswith("_secret")
    )


def _sanitize(value, *, seen, depth):
    if depth > 20:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, bytes):
        return "<binary>"

    object_id = id(value)
    if object_id in seen:
        return "<cycle>"
    seen.add(object_id)
    try:
        if isinstance(value, Mapping):
            clean = {}
            for key, item in value.items():
                key = str(key)
                clean[key] = REDACTED if _sensitive_key(key) else _sanitize(
                    item, seen=seen, depth=depth + 1
                )
            return clean
        if isinstance(value, (list, tuple)):
            return [_sanitize(item, seen=seen, depth=depth + 1) for item in value]
        if isinstance(value, (set, frozenset)):
            items = [_sanitize(item, seen=seen, depth=depth + 1) for item in value]
            return sorted(items, key=lambda item: str(item))
        # Zeep/vendor objects are intentionally not introspected: their repr or private
        # attributes may include authentication/session data.
        return f"<{type(value).__name__}>"
    finally:
        seen.remove(object_id)


def _sanitize_string(value):
    if _MAC_RE.search(value) or _IPV4_RE.search(value) or _EMAIL_RE.search(value):
        return REDACTED
    if "://" in value:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return REDACTED
        if parsed.username is not None or parsed.password is not None:
            return REDACTED
        # Query and fragment commonly carry session tokens.  Preserve only the stable,
        # non-secret resource identity.
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return value


def _observed_health(value, *, false_status):
    if value is True:
        return "ok"
    if value is False:
        return false_status
    return "unknown"


def _api_health(groups):
    states = [
        result.get("state")
        for probes in groups.values()
        if isinstance(probes, Mapping)
        for result in probes.values()
        if isinstance(result, Mapping)
    ]
    if "available" in states:
        return "ok"
    if "error" in states:
        return "degraded"
    return "unknown"


def _storage_health(groups):
    storage = groups.get("storage", {}) if isinstance(groups, Mapping) else {}
    result = storage.get("sd_card", {}) if isinstance(storage, Mapping) else {}
    if not isinstance(result, Mapping) or result.get("state") == "unknown":
        return "unknown"
    if result.get("state") == "error":
        return "degraded"
    value = result.get("value")
    status_words = {word.lower() for word in _strings_for_status(value)}
    bad = {"abnormal", "error", "failed", "fault", "missing", "offline", "unformatted"}
    if status_words & bad or storage_warnings({"groups": groups}):
        return "degraded"
    return "ok"


def _strings_for_status(value, parent_key=""):
    if isinstance(value, Mapping):
        for key, item in value.items():
            key = str(key).lower()
            if key in {"status", "state", "health", "hd_status", "disk_status"}:
                yield str(item)
            else:
                yield from _strings_for_status(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from _strings_for_status(item, parent_key)
