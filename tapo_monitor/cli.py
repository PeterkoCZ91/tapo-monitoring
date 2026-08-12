"""Command-line entry point for tapo-monitor.

Usage:
  tapo-monitor run [cameras.yaml]       # start the config-driven daemon
  tapo-monitor check [cameras.yaml]     # validate the config and print a summary
  tapo-monitor status [health.json]     # show observed camera uptime/outages
  tapo-monitor twin-status [twin.json]  # show layered health and configuration drift
  tapo-monitor probe [cameras.yaml]     # one-shot camera probe (own authenticated session)
  tapo-monitor shadow-record ...        # ingest an independent local observation
  tapo-monitor shadow-report ...        # compare camera and shadow observations
  tapo-monitor audit-log [logfile|-]    # summarize scorer/Telegram audit lines
"""

import argparse
import json
import os
import sys
import time
import types


def _check(path):
    from .config import load_config

    app = load_config(path)
    print(f"OK: {len(app.cameras)} camera(s)")
    for cam in app.cameras:
        print(f"  - {cam.name} @ {cam.host} "
              f"[{cam.role}, schedule={cam.schedule}, weather={cam.weather.strategy}]")
    return 0


def _status(path=None, *, now=None):
    from . import health, notify

    path = path or health.default_state_path()
    state = types.SimpleNamespace(**{name: {} for name in health.PERSISTED_FIELDS})
    if not health.load_state(path, state):
        print(f"No readable health state at {path}", file=sys.stderr)
        return 1
    rows = health.status_rows(state, time.time() if now is None else now)
    if not rows:
        print("No camera health observations yet")
        return 0

    rendered = []
    for row in rows:
        current_for = ("unknown" if row["current_for"] is None
                       else notify.format_duration(row["current_for"]))
        last_outage = ("-" if row["last_outage"] is None
                       else notify.format_duration(row["last_outage"]))
        availability = ("-" if row["availability"] is None
                        else f'{row["availability"]:.2f}%')
        rendered.append((row["camera"], row["state"], current_for,
                         availability, last_outage, str(row["reconnects"])))
    headers = ("camera", "state", "current", "availability", "last outage", "reconnects")
    widths = [max(len(headers[i]), *(len(row[i]) for row in rendered))
              for i in range(len(headers))]

    def line(values):
        return "  ".join(value.ljust(widths[i]) for i, value in enumerate(values)).rstrip()

    print(line(headers))
    print(line(tuple("-" * width for width in widths)))
    for row in rendered:
        print(line(row))
    return 0


def _twin_status(path=None, *, json_output=False):
    from . import twin

    path = path or twin.default_state_path()
    fleet = twin.load_state(path)
    if not fleet and not os.path.isfile(path):
        print(f"No readable digital twin state at {path}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(
            {"version": twin.SCHEMA_VERSION, "cameras": fleet},
            sort_keys=True,
            separators=(",", ":"),
        ))
        return 0
    if not fleet:
        print("No camera digital twin observations yet")
        return 0

    headers = ("camera", "health", "network", "api", "events", "rtsp", "storage",
               "drift", "unknown")
    rendered = []
    for camera in sorted(fleet):
        entry = fleet[camera]
        health = entry.get("health", {})
        layers = health.get("layers", {})
        counts = entry.get("drift", {}).get("counts", {})
        rendered.append((
            camera,
            str(health.get("status", "unknown")),
            *(str(layers.get(layer, "unknown"))
              for layer in ("network", "api", "events", "rtsp", "storage")),
            str(counts.get("drift", 0)),
            str(counts.get("unknown", 0)),
        ))
    _print_table(headers, rendered)
    return 0


def _probe_camera(cfg, night):
    """Open ONE explicit session to a camera and return its twin evaluation.

    Separate from the daemon on purpose: this is the only path that creates an extra
    authenticated session, and the C560WS locks out a source IP after failed logins, so it
    has to be something an operator asks for rather than a background behaviour.
    """
    from . import camera as camera_mod
    from . import capabilities, daemon, twin

    factory = camera_mod.tapo_factory(
        cfg.host,
        os.environ.get(cfg.user_env or "", ""),
        os.environ.get(cfg.password_env or "", ""),
        os.environ.get(cfg.cloud_password_env or "", "") or None,
    )
    client, error = camera_mod.connect(factory, retries=1)
    if client is None:
        raise RuntimeError(f"connect failed: {type(error).__name__ if error else 'unknown'}")
    snapshot = capabilities.collect_snapshot(client)
    plan = daemon.plan_camera(cfg, daemon.effective_night(cfg, night), False)
    evaluation = twin.evaluate_snapshot(cfg.name, plan, snapshot)
    return twin.fleet_entry(
        captured_at=time.time(),
        snapshot=snapshot,
        health=capabilities.derive_health(snapshot),
        evaluation=evaluation,
    )


def _probe(argv):
    from . import scheduling, twin
    from .config import load_config

    parser = argparse.ArgumentParser(
        prog="tapo-monitor probe",
        description="One-shot explicit camera probe (opens its own authenticated session)",
    )
    parser.add_argument("config", nargs="?", default="cameras.yaml")
    parser.add_argument("--camera", dest="camera")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    app = load_config(args.config)
    cameras = app.cameras
    if args.camera:
        cameras = [cam for cam in cameras if cam.name == args.camera]
        if not cameras:
            print(f"No camera named {args.camera!r} in {args.config}", file=sys.stderr)
            return 2
    print(f"Probing {len(cameras)} camera(s); each opens one additional "
          f"authenticated session.", file=sys.stderr)

    night = scheduling.is_night(location=app.location)
    fleet, failed = {}, []
    for cam in cameras:
        try:
            fleet[cam.name] = _probe_camera(cam, night)
        except Exception as exc:  # noqa: BLE001 - one unreachable camera must not hide the rest
            failed.append(cam.name)
            print(f"{cam.name}: probe failed: {exc}", file=sys.stderr)

    if args.json_output:
        print(json.dumps({"version": twin.SCHEMA_VERSION, "cameras": fleet},
                         sort_keys=True, separators=(",", ":")))
        return 1 if failed and not fleet else 0
    if fleet:
        headers = ("camera", "health", "network", "api", "events", "rtsp", "storage",
                   "drift", "unknown")
        rows = []
        for name in sorted(fleet):
            entry = fleet[name]
            layers = entry.get("health", {}).get("layers", {})
            counts = entry.get("drift", {}).get("counts", {})
            rows.append((
                name,
                str(entry.get("health", {}).get("status", "unknown")),
                *(str(layers.get(layer, "unknown"))
                  for layer in ("network", "api", "events", "rtsp", "storage")),
                str(counts.get("drift", 0)),
                str(counts.get("unknown", 0)),
            ))
        _print_table(headers, rows)
    return 1 if failed and not fleet else 0


def _shadow_record(argv):
    from . import ledger

    parser = argparse.ArgumentParser(
        prog="tapo-monitor shadow-record",
        description="Store one media-free independent detector observation",
    )
    parser.add_argument("camera")
    parser.add_argument("event_type")
    parser.add_argument("event_at", type=float, help="Unix event timestamp")
    parser.add_argument("--confidence", type=float)
    parser.add_argument("--adapter", choices=("local_scorer", "recorder"),
                        default="local_scorer")
    parser.add_argument("--ledger", dest="ledger_path")
    args = parser.parse_args(argv)
    events = ledger.EventLedger(args.ledger_path)
    row_id = events.record_shadow_event(
        camera=args.camera,
        event_type=args.event_type,
        event_at=args.event_at,
        confidence=args.confidence,
        adapter=args.adapter,
    )
    print(f"Recorded shadow observation {row_id}")
    return 0


def _shadow_report(argv, *, now=None):
    from . import ledger

    parser = argparse.ArgumentParser(
        prog="tapo-monitor shadow-report",
        description="Correlate camera events with independent local observations",
    )
    parser.add_argument("camera")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--end", type=float)
    parser.add_argument("--window", type=float, default=20.0)
    parser.add_argument("--event-type")
    parser.add_argument("--ledger", dest="ledger_path")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    if args.hours <= 0:
        parser.error("--hours must be greater than zero")
    end = args.end if args.end is not None else (time.time() if now is None else now)
    start = max(0.0, end - args.hours * 3600)
    events = ledger.EventLedger(args.ledger_path)
    report = events.correlation_report(
        camera=args.camera,
        start=start,
        end=end,
        window_seconds=args.window,
        event_type=args.event_type,
    )
    if args.json_output:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    print(
        f"{report['camera']}: camera={report['camera_events']} "
        f"shadow={report['shadow_events']} matched={report['matched']} "
        f"camera_only={report['camera_only']} shadow_only={report['shadow_only']}"
    )
    print(
        "  precision-like={} recall-like={} f1-like={} window={}s".format(
            _metric(report["precision_like"]),
            _metric(report["recall_like"]),
            _metric(report["f1_like"]),
            report["window_seconds"],
        )
    )
    return 0


def _metric(value):
    return "n/a" if value is None else f"{value:.3f}"


def _print_table(headers, rows):
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows))
              for i in range(len(headers))]

    def line(values):
        return "  ".join(value.ljust(widths[i]) for i, value in enumerate(values)).rstrip()

    print(line(headers))
    print(line(tuple("-" * width for width in widths)))
    for row in rows:
        print(line(row))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "run"
    path = argv[1] if len(argv) > 1 else "cameras.yaml"

    if cmd == "check":
        return _check(path)
    if cmd == "run":
        from .daemon import main as daemon_main
        daemon_main([path])
        return 0
    if cmd == "audit-log":
        from .audit import main as audit_main
        return audit_main(argv[1:])
    if cmd == "status":
        return _status(argv[1] if len(argv) > 1 else None)
    if cmd == "twin-status":
        args = argv[1:]
        json_output = "--json" in args
        args = [arg for arg in args if arg != "--json"]
        if len(args) > 1:
            print(__doc__)
            return 2
        return _twin_status(args[0] if args else None, json_output=json_output)
    if cmd == "probe":
        return _probe(argv[1:])
    if cmd == "shadow-record":
        return _shadow_record(argv[1:])
    if cmd == "shadow-report":
        return _shadow_report(argv[1:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
