#!/usr/bin/env python3
"""Dump raw-ish ONVIF event payloads from a Tapo camera.

Reads TAPO_IP, ONVIF_USER, ONVIF_PASS, ONVIF_PORT from environment or an
EnvironmentFile sourced by the caller. Writes JSONL records that are safe to
inspect later without keeping a live ONVIF object around.
"""

import argparse
import ast
import json
import os
import sys
import time
from types import MethodType
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path

try:
    from zeep.helpers import serialize_object
except Exception:
    serialize_object = None

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


def env(name, default=""):
    return os.getenv(name, default)


def to_plain(value, depth=0):
    if depth > 8:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, dt_time, timedelta)):
        return str(value)
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, (list, tuple, set)):
        return [to_plain(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {
            str(k): "<redacted>" if is_sensitive_key(k) else to_plain(v, depth + 1)
            for k, v in value.items()
        }
    if serialize_object is not None:
        try:
            serialized = serialize_object(value, target_cls=dict)
            if serialized is not value:
                return to_plain(serialized, depth + 1)
        except Exception:
            pass
    data = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception:
            continue
        if callable(attr):
            continue
        if is_sensitive_key(name):
            data[name] = "<redacted>"
        else:
            try:
                json.dumps(attr)
                data[name] = attr
            except TypeError:
                data[name] = to_plain(attr, depth + 1)
    return data or repr(value)


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def message_attributes(msg):
    try:
        attrs = msg.Message._value_1.attrib
    except Exception:
        return {}
    try:
        mapped = dict(attrs)
        if mapped:
            return {str(k): str(v) for k, v in mapped.items()}
    except Exception:
        pass
    if isinstance(attrs, dict):
        return {str(k): str(v) for k, v in attrs.items()}
    if isinstance(attrs, str):
        try:
            parsed = ast.literal_eval(attrs)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            return {"raw_attrib": attrs}
    return {"raw_attrib": str(attrs)}


def extract_simple_items(msg):
    items = []
    try:
        data = msg.Message._value_1.Data
        for item in as_list(data.SimpleItem):
            name = str(item.Name)
            value = "<redacted>" if is_sensitive_key(name) else str(item.Value)
            items.append({"name": name, "value": value})
    except Exception:
        pass
    return items


def extract_message_summary(msg):
    topic = None
    try:
        topic = str(msg.Topic._value_1)
    except Exception:
        topic = str(getattr(msg, "Topic", ""))
    attrs = message_attributes(msg)
    return {
        "topic": topic,
        "property_operation": attrs.get("PropertyOperation"),
        "event_utc_time": attrs.get("UtcTime"),
        "simple_items": extract_simple_items(msg),
        "raw": to_plain(msg),
    }


def load_env_file(path):
    if not path:
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", help="optional KEY=VALUE env file to load before reading config")
    parser.add_argument("--duration", type=int, default=90, help="seconds to listen")
    parser.add_argument("--out", default="/tmp/tapo_onvif_events.jsonl")
    parser.add_argument("--message-limit", type=int, default=20)
    parser.add_argument("--timeout", default="PT5S")
    parser.add_argument("--capabilities", action="store_true", help="dump service capabilities/event properties before polling")
    parser.add_argument("--include-camera-ip", action="store_true", help="store the real camera IP in the output")
    parser.add_argument("--print-limit", type=int, default=50, help="maximum event lines to print to stdout; JSONL still stores all events")
    parser.add_argument("--soap-out", help="optional JSONL file containing raw SOAP response bodies from zeep Transport.post_xml")
    args = parser.parse_args()
    load_env_file(args.env_file)

    camera_ip = env("TAPO_IP")
    user = env("ONVIF_USER")
    password = env("ONVIF_PASS")
    port = int(env("ONVIF_PORT", "2020"))
    missing = [name for name, value in {"TAPO_IP": camera_ip, "ONVIF_USER": user, "ONVIF_PASS": password}.items() if not value]
    if missing:
        print(f"missing env: {', '.join(missing)}", file=sys.stderr)
        return 2

    from onvif import ONVIFCamera

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    soap_fh = None
    if args.soap_out:
        soap_out = Path(args.soap_out)
        soap_out.parent.mkdir(parents=True, exist_ok=True)
        soap_fh = soap_out.open("a", encoding="utf-8")

    cam = ONVIFCamera(camera_ip, port, user, password)
    events = cam.create_events_service()

    if soap_fh is not None:
        transport = events.zeep_client.transport
        original_post_xml = transport.post_xml

        def post_xml_with_capture(self, address, envelope, headers):
            response = original_post_xml(address, envelope, headers)
            body = getattr(response, "content", b"") or b""
            record = {
                "ts": now_iso(),
                "address": str(address),
                "status_code": getattr(response, "status_code", None),
                "content_type": getattr(response, "headers", {}).get("Content-Type"),
                "body": body.decode(errors="replace"),
            }
            soap_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            soap_fh.flush()
            return response

        transport.post_xml = MethodType(post_xml_with_capture, transport)

    with out.open("a", encoding="utf-8") as fh:
        def write(record):
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            fh.flush()

        output_camera_ip = camera_ip if args.include_camera_ip else "<redacted>"
        write({"ts": now_iso(), "kind": "start", "camera_ip": output_camera_ip, "port": port})

        if args.capabilities:
            for method in ("GetServiceCapabilities", "GetEventProperties"):
                try:
                    write({"ts": now_iso(), "kind": method, "data": to_plain(getattr(events, method)())})
                except Exception as exc:
                    write({"ts": now_iso(), "kind": method, "error": repr(exc)})

        sub = events.CreatePullPointSubscription({"InitialTerminationTime": "PT5M"})
        write({"ts": now_iso(), "kind": "subscription", "data": to_plain(sub)})
        pull = cam.create_pullpoint_service()

        deadline = time.monotonic() + args.duration
        count = 0
        while time.monotonic() < deadline:
            try:
                resp = pull.PullMessages({"MessageLimit": args.message_limit, "Timeout": args.timeout})
                messages = as_list(getattr(resp, "NotificationMessage", None))
                if not messages:
                    write({"ts": now_iso(), "kind": "poll", "messages": 0})
                for msg in messages:
                    count += 1
                    rec = extract_message_summary(msg)
                    rec.update({"ts": now_iso(), "kind": "event", "index": count})
                    write(rec)
                    if count <= args.print_limit:
                        print(
                            f"[{now_iso()}] event {count}: "
                            f"topic={rec['topic']} operation={rec.get('property_operation')} "
                            f"items={rec['simple_items']}"
                        )
                    elif count == args.print_limit + 1:
                        print(f"[{now_iso()}] print limit reached; continuing JSONL capture quietly")
            except Exception as exc:
                write({"ts": now_iso(), "kind": "error", "error": repr(exc)})
                print(f"[{now_iso()}] error: {exc}", file=sys.stderr)
                time.sleep(3)

        write({"ts": now_iso(), "kind": "done", "events": count})
    if soap_fh is not None:
        soap_fh.close()
        print(f"wrote {args.soap_out}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
