#!/usr/bin/env python3
"""Inspect safe-looking pytapo methods and selected read-only camera calls."""

import argparse
import inspect
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SENSITIVE_KEY_PARTS = (
    "account",
    "auth",
    "barcode",
    "dev_id",
    "face_id",
    "email",
    "hw_id",
    "latitude",
    "longitude",
    "mac",
    "oem_id",
    "pass",
    "password",
    "secret",
    "serial",
    "token",
)


def is_sensitive_key(key):
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def to_jsonable(value, depth=0):
    if depth > 6:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {
            str(k): "<redacted>" if is_sensitive_key(k) else to_jsonable(v, depth + 1)
            for k, v in value.items()
        }
    return repr(value)


def enrich_events(value):
    if not isinstance(value, list):
        return value
    enriched = []
    for event in value:
        if not isinstance(event, dict):
            enriched.append(event)
            continue
        row = dict(event)
        try:
            mask = int(row.get("events_1") or 0)
        except (TypeError, ValueError):
            mask = 0
        info = row.get("event_info")
        if isinstance(info, list):
            row.setdefault("event_info_count", len(info))
            row.setdefault("has_face_info", any(isinstance(x, dict) and "face_id" in x for x in info))
        if mask:
            bits = [idx for idx in range(mask.bit_length()) if mask & (1 << idx)]
            row.setdefault("event_bit_indexes", bits)
            row.setdefault("event_ids_one_based", [idx + 1 for idx in bits])
        enriched.append(row)
    return enriched


def public_methods(obj):
    rows = []
    for name in dir(obj):
        if name.startswith("_"):
            continue
        attr = getattr(obj, name)
        if not callable(attr):
            continue
        try:
            sig = str(inspect.signature(attr))
        except Exception:
            sig = "(?)"
        rows.append({"name": name, "signature": sig})
    return sorted(rows, key=lambda r: r["name"].lower())


def load_env_file(path):
    if not path:
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", help="optional KEY=VALUE env file to load before reading config")
    parser.add_argument("--out", default="/tmp/tapo_api_probe.json")
    parser.add_argument("--call", action="append", default=[], help="specific no-arg method to call")
    parser.add_argument("--call-safe-defaults", action="store_true", help="call conservative read-only getter candidates")
    parser.add_argument("--include-camera-ip", action="store_true", help="store the real camera IP in the output")
    args = parser.parse_args()
    load_env_file(args.env_file)

    camera_ip = os.getenv("TAPO_IP", "")
    email = os.getenv("TAPO_EMAIL", "")
    password = os.getenv("TAPO_PASSWORD", "")
    missing = [name for name, value in {"TAPO_IP": camera_ip, "TAPO_EMAIL": email, "TAPO_PASSWORD": password}.items() if not value]
    if missing:
        print(f"missing env: {', '.join(missing)}", file=sys.stderr)
        return 2

    from pytapo import Tapo

    cam = Tapo(camera_ip, email, password, email)
    methods = public_methods(cam)
    safe_candidates = [
        "getBasicInfo",
        "getAlertEventType",
        "getDeviceInfo",
        "getDetectionConfig",
        "getEvents",
        "getLensMaskConfig",
        "getMotionDetection",
        "getPersonDetection",
        "getPreset",
        "getPresets",
        "getPrivacyMode",
        "getSmartTrackConfig",
        "getStatus",
    ]
    calls = list(args.call)
    if args.call_safe_defaults:
        calls.extend(name for name in safe_candidates if hasattr(cam, name))
    calls = list(dict.fromkeys(calls))

    results = {}
    for name in calls:
        if not hasattr(cam, name):
            results[name] = {"error": "missing method"}
            continue
        method = getattr(cam, name)
        try:
            sig = inspect.signature(method)
            required = [p for p in sig.parameters.values() if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
            if required:
                results[name] = {"skipped": f"requires args: {sig}"}
                continue
        except Exception:
            pass
        try:
            data = method()
            if name == "getEvents":
                data = enrich_events(data)
            results[name] = {"ok": True, "data": to_jsonable(data)}
        except Exception as exc:
            results[name] = {"ok": False, "error": repr(exc)}

    data = {
        "ts": now_iso(),
        "camera_ip": camera_ip if args.include_camera_ip else "<redacted>",
        "pytapo_class": f"{cam.__class__.__module__}.{cam.__class__.__name__}",
        "methods": methods,
        "calls": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out}")
    print("methods containing face/person/detect/smart/event:")
    for row in methods:
        lname = row["name"].lower()
        if any(k in lname for k in ("face", "person", "detect", "smart", "event", "track", "preset")):
            print(f"  {row['name']}{row['signature']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
