#!/usr/bin/env python3
"""Conservative executeFunction method enumerator for Tapo firmware research."""

import argparse
import json
import os
import re
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


SENSITIVE_KEY_PARTS = (
    "account",
    "auth",
    "barcode",
    "cookie",
    "dev_id",
    "device_id",
    "email",
    "face_id",
    "hw_id",
    "latitude",
    "longitude",
    "mac",
    "oem_id",
    "pass",
    "password",
    "secret",
    "serial",
    "session",
    "stok",
    "token",
    "uuid",
)

READ_PREFIXES = ("get", "search", "query", "list", "find", "fetch", "read")

TOPICS = (
    "face",
    "person",
    "people",
    "ai",
    "smart",
    "detection",
    "detect",
    "event",
    "recognition",
    "alarm",
    "alert",
    "playback",
)

QUALIFIERS = (
    "",
    "config",
    "configuration",
    "setting",
    "settings",
    "status",
    "info",
    "list",
    "history",
    "record",
    "records",
    "result",
    "results",
    "management",
    "manager",
    "database",
    "db",
    "image",
    "thumbnail",
    "snapshot",
    "capability",
    "capabilities",
)

KNOWN_METHODS = (
    "getFaceDetectionConfig",
    "getFaceRecognitionConfig",
    "getFaceManagement",
    "getFaceDB",
    "getFaceList",
    "getFaceInfo",
    "getFaceImage",
    "getFamiliarFaceList",
    "getStrangerFaceList",
    "searchFaceList",
    "searchFaceDetectionList",
    "searchFaceRecognitionList",
    "getPersonDetectionConfig",
    "getPeopleDetectionConfig",
    "getSmartDetectConfig",
    "getSmartDetectionConfig",
    "getDetectionConfig",
    "searchDetectionList",
    "getEvents",
    "getLastAlarmInfo",
    "getAlertEventType",
    "getPlayback",
)


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def pascal(parts):
    rendered = []
    for part in parts:
        if not part:
            continue
        if part.lower() == "ai":
            rendered.append("AI")
            continue
        rendered.append(part[:1].upper() + part[1:])
    return "".join(rendered)


def load_env_file(path):
    if not path:
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            try:
                parsed = shlex.split(value, comments=True, posix=True)
                value = parsed[0] if parsed else ""
            except ValueError:
                value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def is_sensitive_key(key):
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def secret_values():
    values = []
    for key, value in os.environ.items():
        if value and len(value) >= 4 and is_sensitive_key(key):
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact_string(value, secrets):
    text = str(value)
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    return text


def redact(value, secrets=(), depth=0):
    if depth > 8:
        return redact_string(repr(value), secrets)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return redact_string(value, secrets)
    if isinstance(value, bytes):
        return redact_string(value.decode(errors="replace"), secrets)
    if isinstance(value, (list, tuple, set)):
        return [redact(item, secrets, depth + 1) for item in value]
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text):
                clean[key_text] = "<redacted>"
            else:
                clean[key_text] = redact(item, secrets, depth + 1)
        return clean
    return redact_string(repr(value), secrets)


def json_key(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def unique_rows(rows):
    seen = set()
    out = []
    for row in rows:
        key = json_key(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def generate_methods(extra_methods=()):
    methods = list(KNOWN_METHODS)
    for prefix in READ_PREFIXES:
        for topic in TOPICS:
            for qualifier in QUALIFIERS:
                methods.append(prefix + pascal((topic, qualifier)))

        for first in TOPICS:
            for second in ("detection", "detect", "recognition", "event", "alarm", "playback"):
                if first == second:
                    continue
                methods.append(prefix + pascal((first, second)))
                methods.append(prefix + pascal((first, second, "config")))
                methods.append(prefix + pascal((first, second, "list")))

    compound_nouns = (
        ("face", "detection"),
        ("face", "recognition"),
        ("face", "management"),
        ("face", "database"),
        ("face", "db"),
        ("face", "image"),
        ("familiar", "face"),
        ("stranger", "face"),
        ("person", "detection"),
        ("people", "detection"),
        ("ai", "detection"),
        ("smart", "detect"),
        ("smart", "detection"),
        ("alert", "event", "type"),
        ("last", "alarm", "info"),
        ("detection", "list"),
        ("playback", "detection"),
    )
    for prefix in READ_PREFIXES:
        for noun in compound_nouns:
            methods.append(prefix + pascal(noun))
            methods.append(prefix + pascal((*noun, "config")))
            methods.append(prefix + pascal((*noun, "list")))

    methods.extend(extra_methods)
    deduped = list(dict.fromkeys(methods))
    known = [name for name in KNOWN_METHODS if name in deduped]
    generated = sorted((name for name in deduped if name not in KNOWN_METHODS), key=str.lower)
    return known + generated


def playback_search_params(hours):
    end_ts = int(time.time())
    start_ts = end_ts - int(hours * 3600)
    return {
        "playback": {
            "search_detection_list": {
                "start_index": 0,
                "channel": 0,
                "start_time": start_ts,
                "end_time": end_ts,
                "end_index": 99,
            }
        }
    }


def param_templates(method, hours):
    lower = method.lower()
    rows = [{"label": "none", "params": None}, {"label": "empty_object", "params": {}}]

    if "face" in lower and "detect" in lower:
        rows.append({"label": "face_detection_name", "params": {"face_detection": {"name": ["detection"]}}})
    if "face" in lower and "recogn" in lower:
        rows.append({"label": "face_recognition_name", "params": {"face_recognition": {"name": ["recognition", "detection"]}}})
    if "person" in lower or "people" in lower:
        rows.append({"label": "people_detection_name", "params": {"people_detection": {"name": ["detection"]}}})
    if "smart" in lower:
        rows.append({"label": "smart_detection_name", "params": {"smart_detection": {"name": ["detection"]}}})
        rows.append({"label": "smart_detect_name", "params": {"smart_detect": {"name": ["detection"]}}})
    if "ai" in lower:
        rows.append({"label": "ai_detection_name", "params": {"ai_detection": {"name": ["detection"]}}})
    if "alarm" in lower or "alert" in lower:
        rows.append({"label": "msg_alarm_table", "params": {"msg_alarm": {"table": "msg_alarm_type"}}})
        rows.append({"label": "last_alarm_info", "params": {"msg_alarm": {"name": ["chn1_msg_alarm_info"]}}})
    if "playback" in lower or ("search" in lower and ("detect" in lower or "event" in lower)):
        rows.append({"label": "playback_search_detection_list", "params": playback_search_params(hours)})
    if "detection" in lower and "face" not in lower and "person" not in lower and "people" not in lower:
        rows.append({"label": "motion_detection_name", "params": {"motion_detection": {"name": ["motion_det"]}}})

    return unique_rows(rows)


def build_call_plan(methods, hours):
    plan = []
    for method in methods:
        for template in param_templates(method, hours):
            plan.append(
                {
                    "call_index": len(plan) + 1,
                    "method": method,
                    "params_label": template["label"],
                    "params": template["params"],
                }
            )
    return plan


def find_error_code(value, depth=0):
    if depth > 6:
        return None
    if isinstance(value, dict):
        for key in ("error_code", "errorCode", "code"):
            if key not in value:
                continue
            try:
                code = int(value[key])
            except (TypeError, ValueError):
                continue
            if code:
                return code
        for item in value.values():
            code = find_error_code(item, depth + 1)
            if code:
                return code
    if isinstance(value, (list, tuple)):
        for item in value:
            code = find_error_code(item, depth + 1)
            if code:
                return code
    return None


def extract_error_code_from_text(text):
    for match in re.finditer(r"(?<!\d)-\d{4,6}(?!\d)", text):
        try:
            return int(match.group(0))
        except ValueError:
            pass
    return None


def classify_error(error_text="", error_code=None):
    lower = error_text.lower()
    if error_code == -40210 or "method_do_not_exist" in lower or "method do not exist" in lower:
        return "method_missing"
    if error_code == -40101 or "wrong params" in lower or "invalid param" in lower:
        return "wrong_params"
    if error_code == -40209 or "invalid_credentials" in lower or "invalid credentials" in lower:
        return "invalid_credentials"
    if error_code == -40106 or "unsupported_method" in lower or "unsupported method" in lower:
        return "unsupported_method"
    if (
        "connection aborted" in lower
        or "remotedisconnected" in lower
        or "connectionreseterror" in lower
        or "remote end closed connection" in lower
        or "badstatusline" in lower
        or "protocolerror" in lower
    ):
        return "connection_aborted"
    return "other"


def classify_data(data):
    code = find_error_code(data)
    if code:
        return classify_error(error_code=code), code
    return "ok", None


def is_empty_payload(data):
    return data is None or data == "" or data == [] or data == {}


def make_client(camera_ip, email, password):
    from pytapo import Tapo

    return Tapo(camera_ip, email, password, email)


def require_env():
    required = {
        "TAPO_IP": os.getenv("TAPO_IP", ""),
        "TAPO_EMAIL": os.getenv("TAPO_EMAIL", ""),
        "TAPO_PASSWORD": os.getenv("TAPO_PASSWORD", ""),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        print("missing env: " + ", ".join(missing), file=sys.stderr)
        return None
    return required


def read_methods_file(path):
    if not path:
        return []
    methods = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            methods.append(line.split()[0])
    return methods


def select_plan(plan, start_call, max_calls):
    selected = [row for row in plan if row["call_index"] >= start_call]
    if max_calls:
        selected = selected[:max_calls]
    return selected


def execute_plan(plan, env, delay, secrets):
    cam = None
    results = []
    for row in plan:
        if cam is None:
            cam = make_client(env["TAPO_IP"], env["TAPO_EMAIL"], env["TAPO_PASSWORD"])

        started = now_iso()
        try:
            data = cam.executeFunction(row["method"], row["params"])
            category, code = classify_data(data)
            result = {
                "call_index": row["call_index"],
                "method": row["method"],
                "params_label": row["params_label"],
                "category": category,
                "ok": category == "ok",
                "empty": category == "ok" and is_empty_payload(data),
                "error_code": code,
                "started_at": started,
                "data": redact(data, secrets),
            }
        except Exception as exc:
            raw_error = repr(exc)
            clean_error = redact_string(raw_error, secrets)
            code = extract_error_code_from_text(clean_error)
            category = classify_error(clean_error, code)
            result = {
                "call_index": row["call_index"],
                "method": row["method"],
                "params_label": row["params_label"],
                "category": category,
                "ok": False,
                "error_code": code,
                "started_at": started,
                "error": clean_error,
            }
            if category == "connection_aborted":
                cam = None

        result["params"] = redact(row["params"], secrets)
        results.append(result)

        if delay > 0:
            time.sleep(delay)
    return results


def summarize(results):
    summary = {
        "ok": [],
        "method_missing": [],
        "wrong_params": [],
        "invalid_credentials": [],
        "unsupported_method": [],
        "connection_aborted": [],
        "other": [],
    }
    for result in results:
        category = result.get("category", "other")
        method = result.get("method")
        if category not in summary:
            category = "other"
        if method not in summary[category]:
            summary[category].append(method)
    counts = {key: len(value) for key, value in summary.items()}
    return {"counts_by_method": counts, "methods_by_category": summary}


def write_json(path, payload):
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Generate and optionally execute conservative Tapo executeFunction method probes."
    )
    parser.add_argument("--env-file", help="optional KEY=VALUE env file")
    parser.add_argument("--out", default="/tmp/tapo_method_bruteforce.json", help="JSON output path")
    parser.add_argument("--execute", action="store_true", help="actually call the camera; default is dry-run only")
    parser.add_argument("--allow-all", action="store_true", help="with --execute, allow running the full generated plan")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between executeFunction calls")
    parser.add_argument("--max-calls", type=int, default=0, help="maximum planned calls to include; 0 means all")
    parser.add_argument("--start-call", type=int, default=1, help="1-based planned call index to start from")
    parser.add_argument("--hours", type=float, default=2.0, help="lookback window for playback search params")
    parser.add_argument("--method", action="append", default=[], help="extra method name to include")
    parser.add_argument("--methods-file", help="file containing extra method names, one per line")
    parser.add_argument("--include-camera-ip", action="store_true", help="include real TAPO_IP in JSON output")
    args = parser.parse_args()

    if args.start_call < 1:
        print("--start-call must be >= 1", file=sys.stderr)
        return 2
    if args.max_calls < 0:
        print("--max-calls must be >= 0", file=sys.stderr)
        return 2
    if args.delay < 0:
        print("--delay must be >= 0", file=sys.stderr)
        return 2
    if args.execute and args.max_calls == 0 and not args.allow_all:
        print("--execute requires --max-calls N, or --allow-all for the full plan", file=sys.stderr)
        return 2

    load_env_file(args.env_file)
    secrets = secret_values()
    extra_methods = list(args.method) + read_methods_file(args.methods_file)
    methods = generate_methods(extra_methods)
    plan = build_call_plan(methods, args.hours)
    selected = select_plan(plan, args.start_call, args.max_calls)

    env = None
    results = []
    if args.execute:
        env = require_env()
        if env is None:
            return 2
        results = execute_plan(selected, env, args.delay, secrets)

    payload = {
        "ts": now_iso(),
        "mode": "execute" if args.execute else "dry_run",
        "camera_ip": os.getenv("TAPO_IP", "<unset>") if args.include_camera_ip else "<redacted>",
        "generated_methods": len(methods),
        "generated_calls": len(plan),
        "start_call": args.start_call,
        "max_calls": args.max_calls,
        "selected_calls": len(selected),
        "delay_seconds": args.delay,
        "lookback_hours": args.hours,
        "plan": redact(selected, secrets),
        "results": results,
        "summary": summarize(results) if results else None,
    }
    out = write_json(args.out, payload)
    print(f"wrote {out}")
    print(f"mode: {payload['mode']}; selected calls: {len(selected)} of {len(plan)}")
    if results:
        counts = payload["summary"]["counts_by_method"]
        print("categories: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
