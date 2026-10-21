#!/usr/bin/env python3
"""Record a real ``getLastAlarmInfo`` answer from one camera (read-only).

Sends ONLY ``getLastAlarmInfo`` (no motor, lamp, privacy or setting calls), spaced
>= 1.5 s apart, prints the redacted raw answer plus what
``alarminfo.parse_last_alarm`` makes of it. Credentials and hosts are never printed.

Exit codes: 0 parsed (or empty but valid), 1 shape not recognised, 2 usage/connect error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tapo_monitor import alarminfo, capabilities  # noqa: E402

MIN_INTERVAL_S = 1.5
_SERIAL_RE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_-]{12,}$")
_SERIAL_KEYS = ("serial", "sn", "mac", "host", "ip", "dev_id", "device_id", "hwid")


def make_client(cfg):
    """Build the client the way the daemon does (module-level so tests can replace it)."""
    from tapo_monitor import camera as camera_mod
    from tapo_monitor import cli

    cli._use_saved_host(cfg)
    factory = camera_mod.tapo_factory(
        camera_mod.active_host(cfg),
        os.environ.get(cfg.user_env or "", ""),
        os.environ.get(cfg.password_env or "", ""),
        os.environ.get(cfg.cloud_password_env or "", "") or None,
    )
    client, error = camera_mod.connect(factory, retries=1)
    if client is None:
        raise RuntimeError(type(error).__name__ if error else "unknown")
    return client


def _extra_redact(value, key=""):
    if isinstance(value, dict):
        return {k: _extra_redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_extra_redact(v, key) for v in value]
    if isinstance(value, str) and (
            _SERIAL_RE.match(value) or any(s in key.lower() for s in _SERIAL_KEYS)):
        return capabilities.REDACTED
    return value


def redact(answer):
    return _extra_redact(capabilities.redact(answer))


def main(argv=None, sleep=time.sleep):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default="cameras.yaml")
    p.add_argument("--camera", required=True)
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--out")
    p.add_argument("--i-stopped-the-daemon", action="store_true", dest="stopped")
    args = p.parse_args(argv)

    if not args.stopped:
        print("WARNING: the daemon must not hold a second pytapo session on this camera "
              "at the same moment (lockout risk). Stop it, then pass "
              "--i-stopped-the-daemon.", file=sys.stderr)
        return 2
    if args.count < 1 or args.interval < MIN_INTERVAL_S:
        print(f"--count must be >= 1 and --interval >= {MIN_INTERVAL_S}", file=sys.stderr)
        return 2

    try:
        from tapo_monitor.config import load_config
        cams = [c for c in load_config(args.config).cameras if c.name == args.camera]
        if not cams:
            print("camera not found in config", file=sys.stderr)
            return 2
        client = make_client(cams[0])
    except Exception as exc:  # noqa: BLE001 - class name only, messages carry hosts
        print(f"setup failed: {type(exc).__name__}", file=sys.stderr)
        return 2

    answers, recognised = [], True
    try:
        for i in range(args.count):
            if i:
                sleep(args.interval)
            try:
                raw = client.executeFunction("getLastAlarmInfo", alarminfo.ALARM_PARAMS)
            except Exception as exc:  # noqa: BLE001
                print(f"request failed: {type(exc).__name__}", file=sys.stderr)
                return 2
            red = redact(raw)
            answers.append(red)
            events = alarminfo.parse_last_alarm(raw, max_age_s=10 * 365 * 86400.0)
            valid = alarminfo._find_info(raw) is not None
            print(f"--- answer {i + 1}/{args.count} (redacted) ---")
            print(json.dumps(red, indent=2, sort_keys=True, default=str))
            print(f"parse_last_alarm: {json.dumps(events, default=str)}")
            if events:
                print(f"verdict: parsed {len(events)} event(s)")
            elif valid:
                print("verdict: parsed 0 event(s) (valid shape, no usable alarm)")
            else:
                print("verdict: shape not recognised: send this redacted answer")
                recognised = False
    finally:
        try:
            from tapo_monitor import camera as camera_mod
            camera_mod.close_client(client)
        except Exception:  # noqa: BLE001
            pass

    if args.out:
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(answers, fh, indent=2, sort_keys=True, default=str)
        os.chmod(args.out, 0o600)
    return 0 if recognised else 1


if __name__ == "__main__":
    sys.exit(main())
