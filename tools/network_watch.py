#!/usr/bin/env python3
"""Low-rate ICMP measurements, a bounded history, and comparable fleet summaries.

Standalone stdlib tool: it opens no camera session and needs no monitor restart.
"""

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PING_COUNT = 5
RETENTION_DAYS = 30
MAX_HISTORY_BYTES = 64 * 1024 * 1024
DEFAULT_STATE = Path.home() / ".local/state/tapo-network-watch"
DEFAULT_CONFIG = Path.home() / ".config/tapo-network-watch/targets.json"
REMOTE_SUMMARY = (
    'python3 "$HOME/.local/share/tapo-network-watch/network_watch.py" '
    'summary --state-dir "$HOME/.local/state/tapo-network-watch" --json --hours '
)


def parse_ping(output):
    """Loss comes from ping's counters; a failed invocation is never 100% loss."""
    counters = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", output)
    if not counters:
        return {"sent": None, "received": None, "rtt_ms": [], "jitter_ms": [],
                "error": "ping_no_counters"}
    sent, received = map(int, counters.groups())
    if sent <= 0 or received > sent:
        return {"sent": None, "received": None, "rtt_ms": [], "jitter_ms": [],
                "error": "ping_invalid_counters"}
    replies = {}
    for seq, value in re.findall(r"icmp_seq=(\d+).*?time[=<]([\d.]+)\s*ms", output):
        milliseconds = float(value)
        if math.isfinite(milliseconds) and milliseconds >= 0:
            replies.setdefault(int(seq), milliseconds)
    ordered = sorted(replies.items())
    jitter = [abs(b - a) for (seq_a, a), (seq_b, b) in zip(ordered, ordered[1:], strict=False)
              if seq_b == seq_a + 1]
    return {"sent": sent, "received": received,
            "rtt_ms": [value for _, value in ordered], "jitter_ms": jitter,
            "error": None}


def probe(target, *, run=subprocess.run):
    started = time.time()
    try:
        result = run(
            # With -w, iputils treats -c as a reply target and can send extra
            # requests under loss. The process timeout bounds the fixed-count burst.
            ["ping", "-n", "-c", str(PING_COUNT), "-i", "0.5", "-W", "1",
             target["host"]],
            capture_output=True, text=True, timeout=10, check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
        metrics = parse_ping(result.stdout)
        if result.returncode not in (0, 1):
            metrics = {"sent": None, "received": None, "rtt_ms": [], "jitter_ms": [],
                       "error": "ping_invocation_failed"}
    except (OSError, subprocess.TimeoutExpired) as exc:
        metrics = {"sent": None, "received": None, "rtt_ms": [], "jitter_ms": [],
                   "error": type(exc).__name__}
    return {"at": started, "name": target["name"], "host": target["host"], **metrics}


def validate_targets(targets):
    if not isinstance(targets, list):
        raise ValueError("targets must be a list")
    names = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("each target must have a name and host")
        name, host = target.get("name"), target.get("host")
        if not isinstance(name, str) or not re.fullmatch(r"[\w.:-]{1,80}", name):
            raise ValueError("target name must contain letters, numbers, dots, colons or dashes")
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:%_-]{0,252}", host):
            raise ValueError("target host must be an address or hostname, never a ping option")
        if name in names:
            raise ValueError("target names must be unique")
        names.add(name)
    if len(targets) > 16:
        raise ValueError("at most 16 targets are allowed")
    return targets


def gateway_targets(*, run=subprocess.run):
    result = run(["ip", "-j", "route", "show", "default"], capture_output=True, text=True,
                 timeout=5, check=True)
    routes = json.loads(result.stdout)
    targets, seen = [], set()
    for route in routes:
        gateway = route.get("gateway")
        if gateway and gateway not in seen:
            targets.append({"name": "router:" + route.get("dev", "default"), "host": gateway})
            seen.add(gateway)
    if not targets:
        raise ValueError("no default gateway found")
    return targets


def load_targets(path):
    cfg = json.loads(Path(path).read_text())
    targets = validate_targets(cfg.get("targets", []))
    gateway_error = None
    if cfg.get("default_gateway", True):
        try:
            targets = [*targets, *gateway_targets()]
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            gateway_error = {
                "at": time.time(), "name": "router", "host": "auto",
                "sent": None, "received": None, "rtt_ms": [], "jitter_ms": [],
                "error": "gateway_discovery_" + type(exc).__name__,
            }
    if not targets and gateway_error is None:
        raise ValueError("no measurement targets configured")
    return validate_targets(targets), gateway_error


def write_latest(directory, payload):
    fd, temporary = tempfile.mkstemp(prefix=".latest-", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, directory / "latest.json")
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def history_files(directory):
    files = []
    for candidate in directory.glob("*.jsonl"):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl", candidate.name):
            try:
                date = dt.date.fromisoformat(candidate.stem)
            except ValueError:
                continue
            files.append((date, candidate))
    return sorted(files)


def prune_history(directory, *, date):
    cutoff = date - dt.timedelta(days=RETENTION_DAYS - 1)
    files = []
    for file_date, path in history_files(directory):
        if file_date < cutoff:
            path.unlink()
        else:
            files.append(path)
    sizes = {path: path.stat().st_size for path in files}
    total = sum(sizes.values())
    while total > MAX_HISTORY_BYTES and len(files) > 1:
        oldest = files.pop(0)
        oldest.unlink()
        total -= sizes[oldest]
    if total > MAX_HISTORY_BYTES:
        # Even an accidentally rapid scheduler cannot fill the disk in one day.
        path = files[0]
        with path.open("rb") as handle:
            handle.seek(total - MAX_HISTORY_BYTES // 2)
            handle.readline()  # the first line may be incomplete
            tail = handle.read()
        fd, temporary = tempfile.mkstemp(prefix=".history-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(tail)
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


def append_records(directory, records, *, now):
    date = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    path = directory / (date.isoformat() + ".jsonl")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as handle:
        for record in records:
            handle.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")
    write_latest(directory, {"at": now, "targets": records})
    prune_history(directory, date=date)


def sample(config, directory, *, quiet=False):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(directory / ".sample.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("network-watch: previous measurement is still running", file=sys.stderr)
            return 0
        targets, gateway_error = load_targets(config)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            records = list(pool.map(probe, targets))
        if gateway_error is not None:
            records.append(gateway_error)
        append_records(directory, records, now=time.time())
    for record in records:
        if not quiet:
            print(json.dumps(record, allow_nan=False, separators=(",", ":")))
        if record["error"]:
            print(f"network-watch: {record['name']}: {record['error']}", file=sys.stderr)
    return int(any(record["error"] for record in records))


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summary(directory, hours, *, now=None):
    now = time.time() if now is None else now
    since = now - hours * 3600
    start_date = dt.datetime.fromtimestamp(since, dt.timezone.utc).date()
    buckets, skipped = {}, 0
    for date, path in history_files(directory):
        if date < start_date:
            continue
        with path.open() as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    at = float(record["at"])
                    if not math.isfinite(at) or not since <= at <= now:
                        continue
                    name, host = record["name"], record["host"]
                    sent, received = record.get("sent"), record.get("received")
                    rtts, jitters = record["rtt_ms"], record["jitter_ms"]
                    if not isinstance(name, str) or not isinstance(host, str):
                        raise ValueError("invalid target")
                    if not all(isinstance(values, list) for values in (rtts, jitters)):
                        raise ValueError("invalid measurements")
                    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0
                               for values in (rtts, jitters) for v in values):
                        raise ValueError("invalid latency")
                    if not record.get("error") and not (isinstance(sent, int) and sent > 0
                                                         and isinstance(received, int)
                                                         and 0 <= received <= sent):
                        raise ValueError("invalid counters")
                except (ValueError, TypeError, KeyError):
                    skipped += 1
                    continue
                bucket = buckets.setdefault((name, host), {
                    "name": name, "host": host, "probes": 0, "probe_errors": 0,
                    "sent": 0, "received": 0, "rtts": [], "jitters": [],
                    "first_at": at, "latest_at": at,
                    "latest_error": None, "last_measurement_at": None,
                })
                bucket["probes"] += 1
                bucket["first_at"] = min(bucket["first_at"], at)
                if at >= bucket["latest_at"]:
                    bucket["latest_at"] = at
                    bucket["latest_error"] = record.get("error")
                if record.get("error"):
                    bucket["probe_errors"] += 1
                else:
                    previous = bucket["last_measurement_at"]
                    bucket["last_measurement_at"] = at if previous is None else max(previous, at)
                    bucket["sent"] += sent
                    bucket["received"] += received
                    bucket["rtts"].extend(rtts)
                    bucket["jitters"].extend(jitters)
    rows = []
    for _, bucket in sorted(buckets.items()):
        rtts, jitters = bucket.pop("rtts"), bucket.pop("jitters")
        bucket.update({
            "loss_pct": 100 * (1 - bucket["received"] / bucket["sent"]) if bucket["sent"] else None,
            "avg_ms": statistics.mean(rtts) if rtts else None,
            "p95_ms": percentile(rtts, 0.95), "max_ms": max(rtts) if rtts else None,
            "stddev_ms": statistics.pstdev(rtts) if rtts else None,
            "jitter_ms": statistics.mean(jitters) if jitters else None,
            "rtt_samples": len(rtts), "jitter_pairs": len(jitters),
            "age_s": max(0, now - bucket["latest_at"]),
            "measurement_age_s": (None if bucket["last_measurement_at"] is None
                                  else max(0, now - bucket["last_measurement_at"])),
        })
        rows.append(bucket)
    return {"window_hours": hours, "at": now, "targets": rows, "skipped_records": skipped}


def fleet_summary(inventory, hours):
    hosts = json.loads(inventory.read_text())["hosts"]
    results = []
    # Configuration holds SSH argv, never a shell command or a credential.
    for host in hosts:
        try:
            command = host["command"]
            if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
                raise ValueError("command must be an argv list")
            result = subprocess.run([*command, REMOTE_SUMMARY + str(hours)],
                                    capture_output=True, text=True, timeout=30, check=True)
            data = json.loads(result.stdout)
            results.append({"name": host["name"], "ok": True, **data})
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            results.append({"name": host.get("name", "unknown"), "ok": False,
                            "error": type(exc).__name__, "targets": []})
    return {"hosts": results, "window_hours": hours}


def print_table(rows):
    print(f"{'HOST / TARGET':<37} {'RX/TX':>11} {'LOSS%':>7} {'AVG':>8} {'P95':>8} "
          f"{'MAX':>8} {'JITTER':>8} {'AGE':>7} {'ERR':>5}")
    print(" " * 57 + "RTT / jitter in ms; age in seconds")
    for host, row in rows:
        label = f"{host} / {row['name']}" if host else row["name"]
        def number(key, row=row):
            return f"{row[key]:.1f}" if row.get(key) is not None else "-"
        print(f"{label:<37} {str(row['received']) + '/' + str(row['sent']):>11} "
              f"{number('loss_pct'):>7} {number('avg_ms'):>8} {number('p95_ms'):>8} "
              f"{number('max_ms'):>8} {number('jitter_ms'):>8} {number('age_s'):>7} "
              f"{row['probe_errors']:>5}")
        if row.get("latest_error"):
            print(f"  Latest probe failed: {row['latest_error']}; last valid measurement "
                  f"{number('measurement_age_s')} seconds ago")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sampling = sub.add_parser("sample", help="take five ICMP samples per target")
    sampling.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sampling.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    sampling.add_argument("--quiet", action="store_true", help="write measurements to history; print only errors")
    for command in ("summary", "fleet"):
        reporting = sub.add_parser(command)
        reporting.add_argument("--hours", type=float, default=24)
        reporting.add_argument("--json", action="store_true")
        if command == "summary":
            reporting.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
        else:
            reporting.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command != "sample" and not (math.isfinite(args.hours) and 0 < args.hours <= 720):
        parser.error("--hours must be between 0 and 720")
    try:
        if args.command == "sample":
            return sample(args.config, args.state_dir, quiet=args.quiet)
        if args.command == "summary":
            data = summary(args.state_dir, args.hours)
            if args.json:
                print(json.dumps(data, allow_nan=False))
            else:
                print_table([("", row) for row in data["targets"]])
                if not data["targets"]:
                    print("No measurements in this window.")
                if data["skipped_records"]:
                    print(f"Skipped unreadable records: {data['skipped_records']}")
            return int(not data["targets"] or bool(data["skipped_records"]))
        data = fleet_summary(args.inventory, args.hours)
        if args.json:
            print(json.dumps(data, allow_nan=False))
        else:
            print_table([(host["name"], row) for host in data["hosts"] for row in host["targets"]])
            for host in data["hosts"]:
                if not host["ok"]:
                    print(f"{host['name']}: measurements unavailable ({host['error']})")
                elif not host["targets"]:
                    print(f"{host['name']}: no measurements in this window")
        return int(any(not host["ok"] or not host["targets"] for host in data["hosts"]))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"network-watch: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
