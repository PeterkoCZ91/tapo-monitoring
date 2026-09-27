"""Shareable, anonymized camera reports (``tapo-monitor report``).

The point is to let any Tapo owner describe what their camera model answers, so a new
model can be supported from a community report instead of someone buying the camera.
It needs no ``cameras.yaml``: ``--host`` plus credentials from ``TAPO_USER`` /
``TAPO_PASSWORD`` (or a prompt; never argv).

What it does, all over ONE authenticated session built by
:func:`camera.tapo_factory` (so the API deny-list is on):

* reads the same public, read-only getters as the digital twin
  (:func:`capabilities.collect_snapshot`), plus the per-lens reads when the camera
  reports more than one lens channel;
* records, per getter, whether it answered or the camera's numeric error code;
* keeps the last ``getEvents`` entries with times made relative;
* ``--watch SECONDS`` polls ``getEvents`` gently while the owner walks past, keeping the
  raw ``events_1`` / ``alarm_type`` / ``chn_events``;
* ``--rtsp`` runs ``ffprobe`` on ``stream1`` / ``stream2`` when ffmpeg is installed.

Anonymization is an **allow-list**: a scalar survives only under a key known to carry
no identity (:data:`SAFE_LEAF_KEYS`); every other value becomes ``"<redacted>"`` and its
path is listed in ``redacted_keys``, so a reviewer can see what was withheld and a
maintainer can widen the list. A final self-check scans the serialized report for
IPv4 addresses, MAC addresses, long hex runs, e-mail addresses and the host/user that
were typed in, and refuses to write the file on any hit. Nothing is uploaded.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time as _time
from collections.abc import Mapping

from . import apiguard, capabilities, detection

SCHEMA = "tapo-monitor-report/1"
REDACTED = "<redacted>"
REVIEW_NOTE = "Review the file before sharing it; nothing is uploaded."

RECENT_EVENTS = 20
DEFAULT_EVENT_HOURS = 12.0
WATCH_MIN_INTERVAL = 3.0
WATCH_DEFAULT_INTERVAL = 5.0
WATCH_WINDOW = 120          # seconds of history each watch poll asks for
WATCH_MAX_ERRORS = 5        # consecutive failed polls that end a watch early
MAX_STRING = 120
RTSP_STREAMS = ("stream1", "stream2")
RTSP_TIMEOUT = 20

# Scalar keys whose values describe the model and its settings, never the owner. Compared
# lowercased. Anything else is redacted and listed, which is the intended way to learn
# what a new model sends: widen this set in a reviewed change, never loosen the rule.
SAFE_LEAF_KEYS = frozenset({
    # model / firmware
    "device_type", "device_model", "model", "hw_version", "sw_version", "fw_ver",
    "device_info", "no_rtsp_constrain", "dev_type", "hw_desc",
    # detection switches and levels
    "enabled", "enable", "sensitivity", "digital_sensitivity", "level", "mode",
    "people_enabled", "vehicle_enabled", "non_vehicle_enabled", "pet_enabled",
    "baby_enabled", "package_enabled", "people_enable", "vehicle_enable", "pet_enable",
    "baby_enable", "motion_enable", "trigger", "detect_type",
    # storage
    "status", "state", "detect_status", "rw_attr", "total_space", "free_space",
    "video_total_space", "video_free_space", "picture_total_space", "picture_free_space",
    "percent", "type", "loop_record_status", "write_protect", "record_duration",
    "record_free_duration", "msg_push_free_space", "loop", "format_status",
    # firmware update state
    "progress", "lastupgradingsuccess", "upgrade_status_code",
    # recording schedule (weekly pattern only)
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    # light / night vision
    "rest_time", "force_time", "wtl_intensity_level", "wtl_force_time",
    # auto-tracking
    "track_mode", "back_time", "track_time",
    "night_vision_mode", "inf_type", "wtl_type",
    # video
    "resolution", "resolutions", "bitrate", "bitrates", "bitrate_type", "bitrate_types",
    "frame_rate", "frame_rates", "encode_type", "encode_types", "quality", "stream_type",
    "zooms", "ldc", "flip_type", "rotate_type", "x_coor", "y_coor",
    # lens layout
    "chn_id", "linkage_type", "chn_num", "channel",
    # getEvents
    "events_1", "alarm_type", "event_type",
})

# Setting keys that are safe only while the value looks like an enum token or a number
# ("auto", "on", "3", "107.3GB"): a free-text or oddly shaped value under them is still
# withheld. Learned from real reports (image, light, codec, SD size and flag fields).
SAFE_ENUM_KEYS = frozenset({
    # image / light
    "overexposure_people_suppression", "switch_mode", "best_view_distance",
    "clear_licence_plate_mode",
    # video capability and quality
    "change_fps_support", "minor_stream_support", "qualitys", "default_bitrate",
    "h265_default_bitrate", "smart_codec",
    # SD card sizes
    "crossline_free_space", "crossline_total_space", "msg_push_total_space",
    # boolean-like device feature flags
    "is_cal", "ffs", "mobile_access",
})
_SAFE_ENUM_PREFIXES = ("image_scene_mode", "full_color_")
_SAFE_ENUM_SUFFIXES = ("_accurate", "_support")
# Keys that are generic or would hit the deny list elsewhere, safe only at these paths
# (matched against the end of the value's path): alert type names, stream quality
# names, OSD font/date/week display settings. OSD label text stays withheld.
_SAFE_ENUM_PATHS = (
    re.compile(r"\.event_types\[\]\.name$"),
    re.compile(r"\.video\.(?:main|minor)\.name$"),
    re.compile(r"(?i)\.osd\.(?:date|week|font)\."
               r"(?:color|color_type|display|size|is_hour12|time_type)$"),
)
_ENUM_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9_.*+-]{0,39})?$")

# Keys whose whole subtree is withheld, container or not: identity, location, network,
# accounts. Matched as substrings of the normalized key.
_DENY_PARTS = ("mac", "serial", "ssid", "alias", "name", "face", "latitude", "longitude",
               "coord", "location", "timezone", "zone", "token", "pass", "email",
               "account", "user", "owner", "cloud_id", "wifi", "bssid", "gps")
_DENY_EXACT = {"ip", "id", "lat", "lon", "lng", "tz", "host", "hostname", "dns", "gateway",
               "netmask", "region", "city"}

# ``*_time`` keys are withheld as possible absolute times, except these durations.
_DURATION_KEYS = frozenset({"rest_time", "force_time", "wtl_force_time", "back_time",
                            "track_time", "full_color_min_keep_time"})

_IPV4_RE = re.compile(
    r"(?<![0-9.])(?:(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(?![0-9])")
_MAC_RE = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])")
# 12+ hex digits mixing letters and digits: a MAC without separators, a device or cloud
# id, a key. Pure-digit runs of 16+ as well (numeric ids); shorter numbers are sizes.
# A byte count with its unit ("115203047424B", the SD card's ``*_accurate`` sizes) is
# digits plus one upper-case B and is not taken for hex.
_HEX_RE = re.compile(r"(?i)(?<![0-9a-z])(?!(?-i:[0-9]+B)(?![0-9a-z]))"
                     r"(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{12,}(?![0-9a-z])")
_DIGITS_RE = re.compile(r"(?<![0-9])[0-9]{16,}(?![0-9])")
_EMAIL_RE = re.compile(r"(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}")
_PATTERNS = (("IPv4 address", _IPV4_RE), ("MAC address", _MAC_RE),
             ("long hex identifier", _HEX_RE), ("long numeric identifier", _DIGITS_RE),
             ("e-mail address", _EMAIL_RE))


class SelfCheckError(RuntimeError):
    """The finished report still looks like it holds an identifier; nothing was written."""

    def __init__(self, findings):
        self.findings = list(findings)
        super().__init__("report self-check failed: " + ", ".join(self.findings))


# --------------------------------------------------------------------------- anonymize

def _norm(key):
    return re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")


def _denied_key(key):
    key = _norm(key)
    if key in _DENY_EXACT or key.endswith("_id") and key != "chn_id":
        return True
    if key.endswith("_ip") or key.startswith("ip_"):
        return True
    if key.endswith("_time") and key not in _DURATION_KEYS:
        return True
    return any(part in key for part in _DENY_PARTS)


def _context_allowed(path):
    return any(rule.search(path) for rule in _SAFE_ENUM_PATHS)


def _enum_key(key, path):
    """True for a key (at ``path``) whose enum- or number-shaped values may be kept."""
    key = _norm(key)
    return (key in SAFE_ENUM_KEYS or key.startswith(_SAFE_ENUM_PREFIXES)
            or key.endswith(_SAFE_ENUM_SUFFIXES) or _context_allowed(path))


def _enum_like(value):
    if isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return value == value and abs(value) != float("inf")
    return (isinstance(value, str) and _ENUM_RE.match(value) is not None
            and not _unsafe_text(value))


def _unsafe_text(text):
    return any(pattern.search(text) for _label, pattern in _PATTERNS)


def _safe_key_text(key):
    key = str(key)
    return len(key) <= 64 and not _unsafe_text(key)


class Anonymizer:
    """Allow-list sanitizer that remembers the paths it withheld."""

    def __init__(self):
        self.redacted: set[str] = set()

    def clean(self, value, path, leaf_key=None):
        """Sanitized copy of ``value``; ``leaf_key`` names a bare scalar answer."""
        return self._walk(value, path, leaf_key=leaf_key, depth=0)

    def _withhold(self, path):
        self.redacted.add(path)
        return REDACTED

    def _walk(self, value, path, leaf_key, depth):
        if depth > 20:
            return self._withhold(path)
        if isinstance(value, Mapping):
            out = {}
            for index, (key, item) in enumerate(value.items()):
                key_text = str(key) if _safe_key_text(key) else f"<key{index}>"
                child = f"{path}.{key_text}" if path else key_text
                if _denied_key(key) and not _context_allowed(child):
                    out[key_text] = self._withhold(child)
                else:
                    out[key_text] = self._walk(item, child, str(key), depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            return [self._walk(item, f"{path}[]", leaf_key, depth + 1) for item in value]
        return self._scalar(value, path, leaf_key)

    def _scalar(self, value, path, leaf_key):
        if value is None:
            return None
        if value == REDACTED or leaf_key is None:
            return self._withhold(path)
        if _norm(leaf_key) not in SAFE_LEAF_KEYS:
            if isinstance(value, bool):
                return value    # a true/false flag under a non-denied key names nothing
            if _enum_key(leaf_key, path) and _enum_like(value):
                return value
            return self._withhold(path)
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value if value == value and abs(value) != float("inf") else None
        if isinstance(value, str):
            if len(value) > MAX_STRING or _unsafe_text(value):
                return self._withhold(path)
            return value
        return self._withhold(path)


def self_check(text, literals=()):
    """Names of the identifier kinds found in ``text``; empty means it may be written.

    ``literals`` are values that must not appear at all (the host and user typed in).
    Only the kind is named, never the matched value, so the error is safe to print.
    """
    found = [label for label, pattern in _PATTERNS if pattern.search(text)]
    lowered = text.lower()
    for literal in literals:
        literal = str(literal or "").strip()
        if len(literal) >= 4 and literal.lower() in lowered:
            found.append("a value you typed in (host or user)")
            break
    return found


def render(report, literals=()):
    """The report as JSON text, or :class:`SelfCheckError` when the self-check hits."""
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    findings = self_check(text, literals)
    if findings:
        raise SelfCheckError(findings)
    return text


# --------------------------------------------------------------------------- collect

def _basic_info(value):
    """``basic_info`` dict inside a getBasicInfo answer (shape differs by transport)."""
    if not isinstance(value, Mapping):
        return {}
    for key in ("device_info", "basic_info"):
        inner = value.get(key)
        if isinstance(inner, Mapping):
            nested = inner.get("basic_info")
            return dict(nested) if isinstance(nested, Mapping) else dict(inner)
    return dict(value)


def _profile_for_model(model):
    model = str(model or "").lower()
    for name, profile in detection.EVENT_PROFILES.items():
        if name != "default" and name in model:
            return profile
    return None


def _channel_list(value):
    """Lens channel numbers in a getAllChnInfo answer, or []."""
    found = set()

    def walk(item):
        if isinstance(item, Mapping):
            for key, sub in item.items():
                if key == "chn_id":
                    try:
                        found.add(int(sub))
                    except (TypeError, ValueError):
                        pass
                else:
                    walk(sub)
        elif isinstance(item, list):
            for sub in item:
                walk(sub)

    walk(value)
    return sorted(found)


def _call(client, method_name, **kwargs):
    """``(value, None)`` or ``(None, error dict)`` for one read-only getter call."""
    if apiguard.is_denied(method_name):
        return None, {"state": "unknown", "reason": "denied_method"}
    method = getattr(client, method_name, None)
    if not callable(method):
        return None, {"state": "unknown", "reason": "missing_method"}
    try:
        return method(**kwargs), None
    except apiguard.DeniedMethodError:
        return None, {"state": "unknown", "reason": "denied_method"}
    except Exception as exc:  # noqa: BLE001 - one getter's failure is a finding
        failed = {"state": "error", "error_type": type(exc).__name__}
        code = capabilities.error_code(exc)
        if code is not None:
            failed["error_code"] = code
        return None, failed


def _probe_methods(channels):
    rows = [(g, n, m) for g, n, m in capabilities._SAFE_PROBES]
    if channels:
        rows += [(g, n, m) for g, n, m in capabilities._DUAL_LENS_PROBES]
        rows += [(g, n, m) for g, n, m in capabilities._PER_CHANNEL_PROBES]
    return rows


def _event_entry(event, now, anon, path):
    """One getEvents entry: times made relative, raw bits kept, the rest withheld."""
    if not isinstance(event, Mapping):
        return {}
    start = _num(event.get("start_time"))
    end = _num(event.get("end_time"))
    entry = {
        "age_s": None if start is None else int(round(now - start)),
        "duration_s": None if start is None or end is None else int(round(end - start)),
        "alarm_type": _int(event.get("alarm_type")),
        "events_1": _int(event.get("events_1")),
    }
    if event.get("event_type") is not None:
        entry["event_type"] = anon.clean({"event_type": event["event_type"]},
                                         path)["event_type"]
    chn = event.get("chn_events")
    if isinstance(chn, Mapping):
        lenses = {}
        for key, item in chn.items():
            channel = _int(key)
            if channel is None or not isinstance(item, Mapping):
                anon.redacted.add(f"{path}.chn_events.<key>")
                continue
            lens = {"events_1": _int(item.get("events_1"))}
            lens_start = _num(item.get("event_start_time"))
            if lens_start is not None and start is not None:
                lens["start_offset_s"] = int(round(lens_start - start))
            extra = sorted(str(k) for k in item if k not in ("events_1", "event_start_time"))
            for name in extra:
                anon.redacted.add(f"{path}.chn_events.{channel}.{_key_label(name)}")
            lenses[str(channel)] = lens
        entry["chn_events"] = lenses
        if entry["events_1"] is None:
            del entry["events_1"]   # a multi-lens model reports it per lens only
    known = {"start_time", "end_time", "alarm_type", "events_1", "event_type", "chn_events",
             "startRelative", "endRelative"}
    for name in sorted(str(k) for k in event if k not in known):
        anon.redacted.add(f"{path}.{_key_label(name)}")
    return entry


def _key_label(name):
    return name if _safe_key_text(name) else "<key>"


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _events(client, now, hours, anon):
    try:
        raw = client.getEvents(startTime=int(now - hours * 3600), endTime=int(now + 60))
    except Exception as exc:  # noqa: BLE001 - an event index failure is a finding
        failed = {"state": "error", "error_type": type(exc).__name__,
                  "window_hours": hours}
        code = capabilities.error_code(exc)
        if code is not None:
            failed["error_code"] = code
        return failed
    raw = [e for e in (raw if isinstance(raw, list) else []) if isinstance(e, Mapping)]
    raw.sort(key=lambda e: _num(e.get("start_time")) or 0)
    recent = raw[-RECENT_EVENTS:]
    return {
        "state": "available" if raw else "unknown",
        "window_hours": hours,
        "total": len(raw),
        "recent": [_event_entry(e, now, anon, "events.recent[]") for e in recent],
    }


def watch_events(client, seconds, *, interval=WATCH_DEFAULT_INTERVAL, anon=None,
                 clock=_time.time, sleep=_time.sleep, say=None):
    """Poll getEvents for ``seconds`` and keep every new or changed entry.

    One call every ``interval`` seconds (at least :data:`WATCH_MIN_INTERVAL`), each for
    the last :data:`WATCH_WINDOW` seconds. An entry is recorded again when it changes
    (an event still running grows its ``end_time``), with ``seen_at_s`` measured from
    the start of the watch. Stops early after :data:`WATCH_MAX_ERRORS` failed polls in
    a row, so a camera that locked the session out is not hammered.
    """
    anon = anon or Anonymizer()
    interval = max(float(interval), WATCH_MIN_INTERVAL)
    started = clock()
    deadline = started + float(seconds)
    seen: set = set()
    events: list = []
    errors: list = []
    polls = 0
    streak = 0
    stopped = "time"
    while True:
        now = clock()
        polls += 1
        try:
            raw = client.getEvents(startTime=int(now - WATCH_WINDOW), endTime=int(now + 60))
            streak = 0
        except Exception as exc:  # noqa: BLE001 - record and keep watching
            raw = None
            streak += 1
            errors.append({"at_s": int(round(now - started)),
                           "error_type": type(exc).__name__,
                           "error_code": capabilities.error_code(exc)})
        for event in (raw if isinstance(raw, list) else []):
            if not isinstance(event, Mapping):
                continue
            signature = json.dumps(
                {k: event.get(k) for k in ("start_time", "end_time", "alarm_type",
                                           "events_1", "chn_events", "event_type")},
                sort_keys=True, default=str)
            if signature in seen:
                continue
            seen.add(signature)
            entry = _event_entry(event, now, anon, "watch.events[]")
            entry["seen_at_s"] = int(round(now - started))
            events.append(entry)
            if say:
                say(f"  event: alarm_type={entry['alarm_type']} events_1={entry['events_1']}"
                    + (f" chn_events={entry['chn_events']}" if "chn_events" in entry else ""))
        if streak >= WATCH_MAX_ERRORS:
            stopped = "errors"
            break
        if clock() + interval > deadline:
            break
        sleep(interval)
    return {"seconds": int(seconds), "interval_s": interval, "polls": polls,
            "stopped_by": stopped, "errors": errors, "events": events}


def probe_rtsp(host, user, password, streams=RTSP_STREAMS, run=subprocess.run,
               which=shutil.which):
    """ffprobe each stream; codec and geometry only, never the URL or stderr."""
    if not which("ffprobe"):
        return {"state": "skipped", "reason": "ffprobe_not_found"}
    from .snapshot import rtsp_url

    result = {}
    for stream in streams:
        argv = ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-show_entries",
                "stream=codec_type,codec_name,profile,width,height,avg_frame_rate,"
                "sample_rate,channels", "-of", "json", rtsp_url(host, user, password, stream)]
        try:
            done = run(argv, capture_output=True, text=True, timeout=RTSP_TIMEOUT,
                       check=False)
        except subprocess.TimeoutExpired:
            result[stream] = {"state": "error", "reason": "timeout"}
            continue
        except OSError:
            result[stream] = {"state": "error", "reason": "ffprobe_failed"}
            continue
        if done.returncode != 0:
            result[stream] = {"state": "error", "reason": "ffprobe_exit",
                              "exit_code": done.returncode}
            continue
        try:
            parsed = json.loads(done.stdout or "{}")
        except ValueError:
            result[stream] = {"state": "error", "reason": "unreadable_output"}
            continue
        keep = ("codec_type", "codec_name", "profile", "width", "height",
                "avg_frame_rate", "sample_rate", "channels")
        tracks = [{k: s[k] for k in keep if k in s and isinstance(s[k], (str, int, float))}
                  for s in parsed.get("streams", []) if isinstance(s, Mapping)]
        result[stream] = {"state": "available" if tracks else "unknown", "tracks": tracks}
    return result


def build_report(client, *, now=None, event_hours=DEFAULT_EVENT_HOURS, watch=None,
                 rtsp=None):
    """The anonymized report dict for a connected ``client``. Read-only calls only.

    ``watch`` is a callable ``(anonymizer) -> dict`` run after the reads (the CLI binds
    :func:`watch_events`); ``rtsp`` an already-collected :func:`probe_rtsp` result.
    """
    from . import __version__

    now = _time.time() if now is None else now
    anon = Anonymizer()

    basic_raw, basic_error = _call(client, "getBasicInfo")
    basic = _basic_info(basic_raw) if basic_error is None else {}
    model = basic.get("device_model") or basic.get("model")
    profile = _profile_for_model(model)

    chn_raw, _chn_error = _call(client, "getAllChnInfo")
    lens_channels = _channel_list(chn_raw)
    if len(lens_channels) < 2 and profile is not None and profile.channels:
        lens_channels = list(profile.channels)
    channels = lens_channels if len(lens_channels) > 1 else []

    snapshot = capabilities.collect_snapshot(client, channels=channels)
    getters = []
    groups: dict = {}
    for group, name, method in _probe_methods(channels):
        result = snapshot["groups"].get(group, {}).get(name, {})
        row = {"method": method, "group": group, "name": name,
               "state": result.get("state", "unknown")}
        for key in ("error_code", "error_type", "reason"):
            if result.get(key) is not None:
                row[key] = result[key]
        if group == "detection_chn":
            row["chn_id"] = list(channels)
        getters.append(row)
        if result.get("state") == "available":
            groups.setdefault(group, {})[name] = anon.clean(
                result.get("value"), f"values.{group}.{name}", leaf_key=name)
    for group, name, reason in capabilities._UNSAFE_PROBES:
        getters.append({"method": None, "group": group, "name": name, "state": "unknown",
                        "reason": reason})

    camera_block = anon.clean({k: basic.get(k) for k in (
        "device_model", "hw_version", "sw_version", "device_type", "device_info")
        if basic.get(k) is not None}, "camera")
    report = {
        "schema": SCHEMA,
        "tool": {"name": "tapo-monitor", "version": __version__},
        "camera": {
            "model": camera_block.get("device_model"),
            "hw_version": camera_block.get("hw_version"),
            "fw_version": camera_block.get("sw_version"),
            "device_type": camera_block.get("device_type"),
            "device_info": camera_block.get("device_info"),
            "lens_channels": lens_channels,
            "event_profile": profile.name if profile is not None else None,
        },
        "getters": getters,
        "values": groups,
        "events": _events(client, now, event_hours, anon),
        "watch": watch(anon) if watch is not None else None,
        "rtsp": rtsp,
        "note": REVIEW_NOTE,
    }
    report["redacted_keys"] = sorted(anon.redacted)
    return report


_GETTER_ROW_KEYS = ("method", "group", "name", "state", "error_type", "error_code",
                    "reason", "chn_id")
_EVENT_INT_KEYS = ("age_s", "duration_s", "alarm_type", "events_1", "seen_at_s")
_RTSP_TRACK_KEYS = ("codec_type", "codec_name", "profile", "width", "height",
                    "avg_frame_rate", "sample_rate", "channels")


def _resanitize_event(entry, anon, path):
    if not isinstance(entry, Mapping):
        return {}
    out = {key: _int(entry[key]) for key in _EVENT_INT_KEYS if key in entry}
    if entry.get("event_type") is not None:
        out["event_type"] = anon.clean({"event_type": entry["event_type"]},
                                       path)["event_type"]
    chn = entry.get("chn_events")
    if isinstance(chn, Mapping):
        lenses = {}
        for key, item in chn.items():
            channel = _int(key)
            if channel is None or not isinstance(item, Mapping):
                anon.redacted.add(f"{path}.chn_events.<key>")
                continue
            lens = {k: _int(item[k]) for k in ("events_1", "start_offset_s") if k in item}
            lenses[str(channel)] = lens
        out["chn_events"] = lenses
    for name in sorted(str(k) for k in entry
                       if k not in (*_EVENT_INT_KEYS, "event_type", "chn_events")):
        anon.redacted.add(f"{path}.{_key_label(name)}")
    return out


def _resanitize_rtsp(rtsp):
    if not isinstance(rtsp, Mapping):
        return None
    if "state" in rtsp:     # the whole probe was skipped
        return {k: rtsp[k] for k in ("state", "reason") if isinstance(rtsp.get(k), str)}
    out = {}
    for stream in RTSP_STREAMS:
        row = rtsp.get(stream)
        if not isinstance(row, Mapping):
            continue
        clean = {k: row[k] for k in ("state", "reason", "exit_code")
                 if isinstance(row.get(k), (str, int))}
        if isinstance(row.get("tracks"), list):
            clean["tracks"] = [
                {k: t[k] for k in _RTSP_TRACK_KEYS
                 if k in t and isinstance(t[k], (str, int, float))}
                for t in row["tracks"] if isinstance(t, Mapping)]
        out[stream] = clean
    return out


def resanitize(report):
    """An existing report passed through the current sanitizer, offline. Pure.

    Lets a report written by an older version (or edited by hand) be brought to what this
    version would write, without touching the camera: every value is cleaned again under
    the current allow-list, unknown fields are dropped and listed, and ``redacted_keys``
    is rebuilt. A value an older version withheld stays withheld: it is gone from the
    file, only a new run against the camera can fill it in. Idempotent.
    """
    if not isinstance(report, Mapping) or report.get("schema") != SCHEMA:
        raise ValueError(f"not a {SCHEMA} report")
    anon = Anonymizer()
    cam = report.get("camera") if isinstance(report.get("camera"), Mapping) else {}
    fields = {"device_model": cam.get("model"), "hw_version": cam.get("hw_version"),
              "sw_version": cam.get("fw_version"), "device_type": cam.get("device_type"),
              "device_info": cam.get("device_info")}
    block = anon.clean({k: v for k, v in fields.items() if v is not None}, "camera")
    profile = _profile_for_model(block.get("device_model"))
    channels = [c for c in (_int(c) for c in cam.get("lens_channels") or [])
                if c is not None]
    getters = [{k: row[k] for k in _GETTER_ROW_KEYS if k in row}
               for row in report.get("getters") or [] if isinstance(row, Mapping)]
    values = {}
    for group, names in (report.get("values") or {}).items():
        if not isinstance(names, Mapping) or not _safe_key_text(group):
            anon.redacted.add(f"values.{_key_label(str(group))}")
            continue
        values[group] = {name: anon.clean(value, f"values.{group}.{name}", leaf_key=name)
                         for name, value in names.items() if _safe_key_text(name)}
    events = report.get("events")
    if isinstance(events, Mapping):
        events = {**{k: events[k] for k in ("state", "window_hours", "total",
                                            "error_type", "error_code") if k in events},
                  "recent": [_resanitize_event(e, anon, "events.recent[]")
                             for e in events.get("recent") or []]}
        if not events["recent"] and "total" not in events:
            del events["recent"]
    watch = report.get("watch")
    if isinstance(watch, Mapping):
        watch = {**{k: watch[k] for k in ("seconds", "interval_s", "polls", "stopped_by")
                    if k in watch},
                 "errors": [{k: e[k] for k in ("at_s", "error_type", "error_code") if k in e}
                            for e in watch.get("errors") or [] if isinstance(e, Mapping)],
                 "events": [_resanitize_event(e, anon, "watch.events[]")
                            for e in watch.get("events") or []]}
    tool = report.get("tool") if isinstance(report.get("tool"), Mapping) else {}
    carried = {k for k in report.get("redacted_keys") or []
               if isinstance(k, str) and not k.startswith(("values.", "camera."))}
    return {
        "schema": SCHEMA,
        "tool": {k: tool[k] for k in ("name", "version") if isinstance(tool.get(k), str)},
        "camera": {
            "model": block.get("device_model"),
            "hw_version": block.get("hw_version"),
            "fw_version": block.get("sw_version"),
            "device_type": block.get("device_type"),
            "device_info": block.get("device_info"),
            "lens_channels": channels,
            "event_profile": profile.name if profile is not None else None,
        },
        "getters": getters,
        "values": values,
        "events": events if isinstance(events, Mapping) else None,
        "watch": watch if isinstance(watch, Mapping) else None,
        "rtsp": _resanitize_rtsp(report.get("rtsp")),
        "note": REVIEW_NOTE,
        "redacted_keys": sorted(anon.redacted | carried),
    }


# --------------------------------------------------------------------------- summarize

def _all_event_masks(report):
    """``(events_1, alarm_type)`` pairs from every event and lens in a report."""
    pairs = []
    blocks = []
    events = report.get("events")
    if isinstance(events, Mapping):
        blocks += list(events.get("recent") or [])
    watch = report.get("watch")
    if isinstance(watch, Mapping):
        blocks += list(watch.get("events") or [])
    for entry in blocks:
        if not isinstance(entry, Mapping):
            continue
        alarm = entry.get("alarm_type")
        if entry.get("events_1") is not None:
            pairs.append((entry["events_1"], alarm))
        for lens in (entry.get("chn_events") or {}).values():
            if isinstance(lens, Mapping) and lens.get("events_1") is not None:
                pairs.append((lens["events_1"], alarm))
    return pairs


def summarize(report):
    """Offline digest of a report: text lines. Pure."""
    if not isinstance(report, Mapping) or report.get("schema") != SCHEMA:
        raise ValueError(f"not a {SCHEMA} report")
    cam = report.get("camera") or {}
    profile = _profile_for_model(cam.get("model"))
    lines = [f"model {cam.get('model') or '?'}  hw {cam.get('hw_version') or '?'}  "
             f"fw {cam.get('fw_version') or '?'}"]
    lines.append("event profile: " + (profile.name if profile else
                                      "none matches (decoded with the default table)"))
    getters = [g for g in report.get("getters") or [] if isinstance(g, Mapping)]
    ok = [g for g in getters if g.get("state") == "available"]
    bad = [g for g in getters if g.get("state") == "error"]
    other = [g for g in getters if g.get("state") not in ("available", "error")]

    def label(g):
        base = g.get("method") or f"{g.get('group')}.{g.get('name')}"
        return f"{base}[chn]" if g.get("chn_id") else base

    lines.append(f"working getters ({len(ok)}): " + ", ".join(label(g) for g in ok))
    if bad:
        lines.append(f"failing getters ({len(bad)}): " + ", ".join(
            f"{label(g)} ({g.get('error_code') or g.get('error_type')})" for g in bad))
    if other:
        lines.append(f"not read ({len(other)}): " + ", ".join(
            f"{label(g)} ({g.get('reason')})" for g in other))
    pairs = _all_event_masks(report)
    unknown: set = set()
    alarms: set = set()
    masks: set = set()
    for events_1, alarm in pairs:
        flags = detection.decode_events_1(events_1, profile, alarm)
        unknown.update(flags["unknown_bits"])
        masks.add(flags["raw"])
        if alarm is not None:
            alarms.add(alarm)
    lines.append(f"events: {len(pairs)} mask(s); events_1 values {sorted(masks) or '-'}; "
                 f"alarm_type values {sorted(alarms) or '-'}")
    lines.append("unknown events_1 bits: " + (", ".join(
        f"{bit} (value {1 << bit})" for bit in sorted(unknown)) or "none"))
    redacted = report.get("redacted_keys") or []
    lines.append(f"redacted values: {len(redacted)}")
    return lines


# --------------------------------------------------------------------------- CLI

WARNING = """\
WARNING: this opens its OWN authenticated session to the camera.
  Stop tapo-monitor, Home Assistant and anything else polling the camera first:
  a second session can make the camera refuse logins (-40214) and lock this
  computer out for up to ~30 minutes. Only read-only getters are sent."""


def _credentials(env=os.environ, prompt=getpass.getpass, ask=input):
    user = env.get("TAPO_USER") or ask("Camera account user: ").strip()
    password = env.get("TAPO_PASSWORD") or prompt("Camera account password: ")
    return user, password


def main(argv=None, *, factory_for=None, connect=None, credentials=_credentials,
         confirm=None, clock=_time.time, sleep=_time.sleep, rtsp_probe=probe_rtsp):
    parser = argparse.ArgumentParser(
        prog="tapo-monitor report",
        description="Anonymized, shareable report of what a Tapo camera answers "
                    "(read-only, one session, nothing uploaded)")
    parser.add_argument("--host", help="camera IP address or hostname")
    parser.add_argument("--out", default="tapo-report.json", help="output JSON file")
    parser.add_argument("--watch", type=float, default=0, metavar="SECONDS",
                        help="poll getEvents this long while you walk past the camera")
    parser.add_argument("--interval", type=float, default=WATCH_DEFAULT_INTERVAL,
                        help=f"seconds between watch polls (min {WATCH_MIN_INTERVAL:g})")
    parser.add_argument("--events-hours", type=float, default=DEFAULT_EVENT_HOURS,
                        help="how far back to read the camera's event index")
    parser.add_argument("--rtsp", action="store_true",
                        help="also ffprobe stream1/stream2 (needs ffmpeg)")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--summarize", metavar="FILE",
                        help="print a digest of an existing report (offline)")
    args = parser.parse_args(argv)

    if args.summarize:
        try:
            with open(args.summarize, encoding="utf-8") as handle:
                data = json.load(handle)
            lines = summarize(data)
        except (OSError, ValueError) as exc:
            print(f"cannot summarize {args.summarize}: {exc}", file=sys.stderr)
            return 2
        print("\n".join(lines))
        return 0
    if not args.host:
        parser.error("--host is required (or --summarize FILE)")
    if args.watch < 0 or args.events_hours <= 0:
        parser.error("--watch must be >= 0 and --events-hours > 0")

    print(WARNING, file=sys.stderr)
    if not args.yes:
        confirm = confirm or (lambda: sys.stdin.isatty() and input(
            "Continue? [y/N] ").strip().lower() in ("y", "yes"))
        if not confirm():
            print("aborted (use --yes to skip the prompt)", file=sys.stderr)
            return 1
    user, password = credentials()
    if not user or not password:
        print("no credentials (set TAPO_USER / TAPO_PASSWORD or answer the prompt)",
              file=sys.stderr)
        return 2

    from . import camera as camera_mod

    factory_for = factory_for or camera_mod.tapo_factory
    connect = connect or camera_mod.connect
    # retries=1: a failed login counts toward the camera's lockout, never retry blind.
    client, error = connect(factory_for(args.host, user, password), retries=1)
    if client is None:
        code = capabilities.error_code(error) if error is not None else None
        print(f"connect failed: {type(error).__name__ if error else 'unknown'}"
              + (f" (error {code})" if code is not None else ""), file=sys.stderr)
        return 1
    try:
        def run_watch(anon):
            print(f"Watching events for {args.watch:g} s: walk past the camera now.",
                  file=sys.stderr)
            return watch_events(client, args.watch, interval=args.interval, anon=anon,
                                clock=clock, sleep=sleep,
                                say=lambda line: print(line, file=sys.stderr))
        rtsp = rtsp_probe(args.host, user, password) if args.rtsp else None
        report = build_report(client, now=clock(), event_hours=args.events_hours,
                              watch=run_watch if args.watch > 0 else None, rtsp=rtsp)
    finally:
        camera_mod.close_client(client)

    try:
        text = render(report, literals=(args.host, user))
    except SelfCheckError as exc:
        print(f"NOT written: {exc}. Please report this as a bug in the anonymizer.",
              file=sys.stderr)
        return 3
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {args.out}: {len(report['getters'])} getters, "
          f"{len(report['redacted_keys'])} redacted value path(s).", file=sys.stderr)
    print(REVIEW_NOTE, file=sys.stderr)
    return 0
