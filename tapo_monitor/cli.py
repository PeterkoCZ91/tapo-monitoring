"""Command-line entry point for tapo-monitor.

Usage:
  tapo-monitor run [cameras.yaml]       # start the config-driven daemon
  tapo-monitor check [cameras.yaml]     # validate the config and print a summary
  tapo-monitor audit-log [logfile|-]    # summarize scorer/Telegram audit lines
"""

import sys


def _check(path):
    from .config import load_config

    app = load_config(path)
    print(f"OK: {len(app.cameras)} camera(s)")
    for cam in app.cameras:
        print(f"  - {cam.name} @ {cam.host} "
              f"[{cam.role}, schedule={cam.schedule}, weather={cam.weather.strategy}]")
    return 0


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
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
