#!/usr/bin/env python3
"""Capture low-level pytapo HTTP responses for firmware API research.

This script intentionally monkeypatches pytapo only in the current process. It is
meant for methods that abort or fail before pytapo can return normal JSON.
"""

import argparse
import copy
import json
import os
import re
import shlex
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path


SENSITIVE_KEY_PARTS = (
    "account",
    "auth",
    "barcode",
    "cookie",
    "cnonce",
    "dev_id",
    "device_id",
    "digest",
    "email",
    "face_id",
    "hw_id",
    "latitude",
    "longitude",
    "mac",
    "nonce",
    "oem_id",
    "pass",
    "password",
    "secret",
    "serial",
    "session",
    "stok",
    "tag",
    "token",
    "uuid",
)

DEFAULT_CALLS = (
    ("getFaceManagement", None),
    ("searchFaceList", None),
    ("getFaceDB", None),
    ("getFaceRecognitionConfig", None),
    ("getSmartDetectConfig", None),
    ("getSmartAIConfig", None),
    ("getAIDetectConfig", None),
)


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


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
    text = re.sub(r"/stok=[^/\s]+", "/stok=<redacted>", text)
    text = re.sub(r"(stok=)[^&\s]+", r"\1<redacted>", text)
    text = re.sub(r'("stok"\s*:\s*")[^"]+', r'\1<redacted>', text)
    text = re.sub(r'("token"\s*:\s*")[^"]+', r'\1<redacted>', text, flags=re.IGNORECASE)
    text = re.sub(r'("Tapo_tag"\s*:\s*")[^"]+', r'\1<redacted>', text)
    return text


def redact(value, secrets=(), depth=0):
    if depth > 8:
        return redact_string(repr(value), secrets)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return redact_string(value, secrets)
    if isinstance(value, bytes):
        return {
            "len": len(value),
            "hex_prefix": value[:512].hex(),
            "text_prefix": redact_string(value[:512].decode(errors="replace"), secrets),
        }
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


def parse_method_spec(value):
    if ":" not in value:
        return value, None
    method, params_text = value.split(":", 1)
    return method, json.loads(params_text)


def infer_logical_method(request):
    if not isinstance(request, dict):
        return None
    method = request.get("method")
    if method != "multipleRequest":
        return method
    try:
        inner = request.get("params", {}).get("requests", [])
        names = [item.get("method") for item in inner if isinstance(item, dict) and item.get("method")]
        if names:
            return "multipleRequest:" + ",".join(names)
    except Exception:
        pass
    return method


def install_capture(out_dir, secrets, include_request_body=False):
    from pytapo.transport.pytapo import pytapo as transport_module

    out_dir.mkdir(parents=True, exist_ok=True)
    original_send = transport_module.pyTapo.send
    original_request = transport_module.pyTapo._request
    counter = {"n": 0}

    async def capture_send(self, request, retry=0):
        previous = getattr(self, "_raw_capture_request", None)
        self._raw_capture_request = copy.deepcopy(request)
        try:
            return await original_send(self, request, retry=retry)
        finally:
            self._raw_capture_request = previous

    def capture_request(self, method, url, transientRetryCount=0, **kwargs):
        counter["n"] += 1
        index = counter["n"]
        logical_request = getattr(self, "_raw_capture_request", None)
        logical_method = infer_logical_method(logical_request)

        request_data = kwargs.get("data")
        low_level_method = None
        if isinstance(request_data, str):
            try:
                low_level_method = json.loads(request_data).get("method")
            except Exception:
                low_level_method = None

        record = {
            "ts": now_iso(),
            "index": index,
            "logical_method": redact(logical_method, secrets),
            "low_level_method": redact(low_level_method, secrets),
            "http_method": method,
            "url": redact_string(url, secrets),
            "transient_retry_count": transientRetryCount,
        }
        if include_request_body:
            record["request_kwargs"] = redact(kwargs, secrets)
        else:
            headers = kwargs.get("headers") or {}
            record["request_headers"] = redact(headers, secrets)
            record["request_body_len"] = len(request_data or "")

        try:
            response = original_request(
                self,
                method,
                url,
                transientRetryCount=transientRetryCount,
                **kwargs,
            )
            body = response.content or b""
            record.update(
                {
                    "ok": True,
                    "status_code": response.status_code,
                    "response_headers": redact(dict(response.headers), secrets),
                    "response_body_len": len(body),
                    "response_body_hex_prefix": body[:1024].hex(),
                    "response_body_text_prefix": redact_string(body[:1024].decode(errors="replace"), secrets),
                }
            )
            return response
        except Exception as exc:
            response = getattr(exc, "response", None)
            record.update(
                {
                    "ok": False,
                    "exception_type": type(exc).__name__,
                    "exception": redact_string(repr(exc), secrets),
                }
            )
            if response is not None:
                try:
                    body = response.content or b""
                except Exception:
                    body = b""
                record.update(
                    {
                        "status_code": getattr(response, "status_code", None),
                        "response_headers": redact(dict(getattr(response, "headers", {}) or {}), secrets),
                        "response_body_len": len(body),
                        "response_body_hex_prefix": body[:1024].hex(),
                        "response_body_text_prefix": redact_string(body[:1024].decode(errors="replace"), secrets),
                    }
                )
            raise
        finally:
            path = out_dir / f"{index:04d}-{logical_method or low_level_method or 'request'}.json"
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.name)
            path = out_dir / safe
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    transport_module.pyTapo.send = capture_send
    transport_module.pyTapo._request = capture_request


def run_call(cam, method, params):
    if params is None:
        return cam.executeFunction(method, None)
    return cam.executeFunction(method, params)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file")
    parser.add_argument("--out-dir", default="/tmp/tapo-pytapo-raw")
    parser.add_argument("--method", action="append", help="method or method:{json params}; can be repeated")
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--max-calls", type=int, default=0)
    parser.add_argument("--include-request-body", action="store_true", help="private local debugging only; may include encrypted payloads/tags")
    parser.add_argument("--summary", default="/tmp/tapo-pytapo-raw-summary.json")
    args = parser.parse_args()

    load_env_file(args.env_file)
    camera_ip = os.getenv("TAPO_IP", "")
    email = os.getenv("TAPO_EMAIL", "")
    password = os.getenv("TAPO_PASSWORD", "")
    cloud_password = os.getenv("TAPO_CLOUD_PASSWORD", email)
    missing = [name for name, value in {"TAPO_IP": camera_ip, "TAPO_EMAIL": email, "TAPO_PASSWORD": password}.items() if not value]
    if missing:
        print("missing env: " + ", ".join(missing), file=sys.stderr)
        return 2

    calls = [parse_method_spec(value) for value in args.method] if args.method else list(DEFAULT_CALLS)
    if args.max_calls:
        calls = calls[: args.max_calls]

    secrets = secret_values()
    out_dir = Path(args.out_dir)
    install_capture(out_dir, secrets, include_request_body=args.include_request_body)

    from pytapo import Tapo

    cam = Tapo(camera_ip, email, password, cloud_password)
    results = []
    for index, (method, params) in enumerate(calls, start=1):
        row = {"index": index, "method": method, "params": redact(params, secrets), "ts": now_iso()}
        try:
            data = run_call(cam, method, params)
            row["ok"] = True
            row["result_type"] = type(data).__name__
            row["result"] = redact(data, secrets)
            print(f"{method}: ok")
        except Exception as exc:
            row["ok"] = False
            row["exception_type"] = type(exc).__name__
            row["exception"] = redact_string(repr(exc), secrets)
            row["traceback_tail"] = redact_string("\n".join(traceback.format_exc().splitlines()[-6:]), secrets)
            print(f"{method}: error: {row['exception'][:160]}")
        results.append(row)
        if args.delay > 0 and index < len(calls):
            time.sleep(args.delay)

    summary = {
        "ts": now_iso(),
        "camera_ip": "<redacted>",
        "out_dir": str(out_dir),
        "calls": results,
    }
    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote " + str(summary_path))
    print("wrote raw captures under " + str(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
