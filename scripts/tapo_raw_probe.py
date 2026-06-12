#!/usr/bin/env python3
import argparse, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
SENSITIVE = ("account", "auth", "barcode", "dev_id", "face_id", "email", "hw_id", "latitude", "longitude", "mac", "oem_id", "pass", "password", "secret", "serial", "token")
def is_sensitive_key(key):
    key = str(key).lower().replace("-", "_")
    return any(part in key for part in SENSITIVE)
def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
def load_env_file(path):
    if not path:
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip(chr(34)).strip(chr(39)))
def clean(value, depth=0):
    if depth > 8:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, (list, tuple, set)):
        return [clean(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): ("<redacted>" if is_sensitive_key(k) else clean(v, depth + 1)) for k, v in value.items()}
    return repr(value)
def add_bits(event):
    if not isinstance(event, dict):
        return event
    row = dict(event)
    try:
        mask = int(row.get("events_1") or 0)
    except (TypeError, ValueError):
        mask = 0
    info = row.get("event_info")
    if isinstance(info, list):
        row.setdefault("event_info_count", len(info))
        row.setdefault("has_face_info", any(isinstance(x, dict) and "face_id" in x for x in info))
        row["event_info"] = "<redacted>" if info else []
    if mask:
        bits = [i for i in range(mask.bit_length()) if mask & (1 << i)]
        row.setdefault("event_bit_indexes", bits)
        row.setdefault("event_ids_one_based", [i + 1 for i in bits])
    return row
def calls(hours):
    end_ts = int(time.time())
    start_ts = end_ts - int(hours * 3600)
    return [
        ("getFaceDetectionConfig", {"face_detection": {"name": ["detection"]}}),
        ("getAlertEventType", {"msg_alarm": {"table": "msg_alarm_type"}}),
        ("getLastAlarmInfo", {"msg_alarm": {"name": ["chn1_msg_alarm_info"]}}),
        ("searchDetectionList", {"playback": {"search_detection_list": {"start_index": 0, "channel": 0, "start_time": start_ts, "end_time": end_ts, "end_index": 999}}}),
        ("getPersonDetectionConfig", {"people_detection": {"name": ["detection"]}}),
        ("getDetectionConfig", {"motion_detection": {"name": ["motion_det"]}}),
        ("getVehicleDetectionConfig", {"vehicle_detection": {"name": ["detection"]}}),
        ("getPetDetectionConfig", {"pet_detection": {"name": ["detection"]}}),
        ("getPackageDetectionConfig", {"package_detection": {"name": ["detection"]}}),
        ("getTamperDetectionConfig", {"tamper_detection": {"name": ["tamper_det"]}}),
        ("getLinecrossingDetectionConfig", {"linecrossing_detection": {"name": ["detection", "arming_schedule"]}}),
        ("getFaceRecognitionConfig", None),
        ("getFamiliarFaceList", None),
        ("getStrangerFaceList", None),
        ("getFaceList", None),
        ("getFaceInfo", None),
        ("getFaceImage", None),
        ("searchFaceDetectionList", None),
        ("searchFaceRecognitionList", None),
    ]
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file")
    parser.add_argument("--out", default="/tmp/tapo_raw_probe.json")
    parser.add_argument("--hours", type=float, default=2.0)
    parser.add_argument("--delay", type=float, default=2.0, help="seconds to wait between raw API calls")
    parser.add_argument("--max-calls", type=int, default=0, help="limit number of raw API calls for gentle probing; 0 means all")
    parser.add_argument("--start-call", type=int, default=1, help="1-based call index to start from")
    parser.add_argument("--include-camera-ip", action="store_true")
    args = parser.parse_args()
    load_env_file(args.env_file)
    camera_ip = os.getenv("TAPO_IP", "")
    email = os.getenv("TAPO_EMAIL", "")
    password = os.getenv("TAPO_PASSWORD", "")
    missing = [name for name, value in {"TAPO_IP": camera_ip, "TAPO_EMAIL": email, "TAPO_PASSWORD": password}.items() if not value]
    if missing:
        print("missing env: " + ", ".join(missing), file=sys.stderr)
        return 2
    from pytapo import Tapo
    cam = Tapo(camera_ip, email, password, email)
    results = {}
    executed = 0
    for idx, (method, params) in enumerate(calls(args.hours), start=1):
        if idx < args.start_call:
            continue
        if args.max_calls and executed >= args.max_calls:
            break
        executed += 1
        try:
            data = clean(cam.executeFunction(method, params))
            if method == "searchDetectionList":
                try:
                    events = data["playback"]["search_detection_list"]
                    if isinstance(events, list):
                        data["playback"]["search_detection_list"] = [add_bits(e) for e in events]
                except Exception:
                    pass
            results[method] = {"ok": True, "params": clean(params), "data": data}
        except Exception as exc:
            results[method] = {"ok": False, "params": clean(params), "error": repr(exc)}
        if args.delay > 0:
            time.sleep(args.delay)
    payload = {"ts": now_iso(), "camera_ip": camera_ip if args.include_camera_ip else "<redacted>", "lookback_hours": args.hours, "results": results}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote " + str(out))
    for name, result in results.items():
        if result.get("ok"):
            print(name + ": ok")
        else:
            print(name + ": error: " + result.get("error", "")[:120])
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
