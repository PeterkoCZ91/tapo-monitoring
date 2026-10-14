#!/usr/bin/env python3
"""Dump the SD clips of recent SD follow-up alerts as dense frames, for offline evaluation.

Step 1 of "Evaluating the follow-up frame pick" (docs/operations.md). Runs ON a camera
host, next to the deployed package, because only that host reaches the camera and has
its credentials. It reads the sent-photo archive (``TAPO_SENT_LOG_DIR``), takes the
unique ``event_start`` of every frame the SD follow-up sent (``path == "sd"``) within
the last ``--days``, and downloads each event's clip from the card at ``--every``
seconds per frame into ``OUT/<event_start>/``.

Events are fetched one after another with ``--pause`` seconds between them: parallel
media sessions make the camera answer -40214 and can disturb the running daemon. An
event directory that already holds frames is skipped, so a rerun only fills the gaps;
an error is logged and the next event tried.

Credentials come only from the environment variables the config names for the camera
(``user_env``/``password_env``/``cloud_password_env``), never from the command line::

    cd ~/tapo-monitor/current
    set -a; . ~/tapo-monitor/tapo.env; set +a
    .venv/bin/python tools/sd_frame_dump.py ~/tapo-monitor/cameras.yaml front \\
        --sent-log ~/tapo-monitor/sent-log --days 3 --out ~/tapo-monitor/frame-eval

The frames are private camera images: copy them to the machine that scores them and
keep them out of the repository.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

from tapo_monitor import config as config_mod
from tapo_monitor import sdclip, sentlog

SEND_PATH = "sd"


def read_index(path):
    """Records of a sent-log ``index.jsonl``; unreadable lines are skipped."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def event_starts(rows, now, days, camera=None, send_path=SEND_PATH):
    """Unique ``event_start`` values (ascending) of recent frames sent by ``send_path``. Pure.

    A row counts when its ``path`` is ``send_path``, its ``ts`` lies within ``days`` of
    ``now`` and it carries an integer ``event_start``. With ``camera`` set, rows naming
    another camera are left out; rows without a camera name (a one-camera host) stay.
    """
    since = now - days * 86400
    starts = set()
    for row in rows:
        ts, start = row.get("ts"), row.get("event_start")
        if row.get("path") != send_path:
            continue
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts < since:
            continue
        if isinstance(start, bool) or not isinstance(start, int):
            continue
        if camera and row.get("camera") and row["camera"] != camera:
            continue
        starts.add(start)
    return sorted(starts)


def has_frames(directory):
    """True when ``directory`` already holds extracted JPEG frames."""
    return bool(glob.glob(os.path.join(directory, "*.jpg")))


def find_camera(app, name):
    """The camera called ``name`` in a loaded config, or None."""
    return next((c for c in app.cameras if c.name == name), None)


def camera_login(cam):
    """``(host, user, password, cloud_password)`` with secrets from the env vars ``cam`` names."""
    user, password, cloud = config_mod.resolve_camera_credentials(cam)
    return cam.host, user, password, cloud


def dump(cam, starts, out, span, every, pause, fetch=None, client_for=None, sleep=time.sleep,
         log=print):
    """Fetch each event in ``starts`` into ``out/<start>``; returns ``{start: frame count}``.

    Sequential by design. Skips events whose directory already has frames (count None).
    ``fetch``/``client_for`` default to the real SD download; tests pass fakes.
    """
    fetch = fetch or sdclip.fetch_sd_frames
    host, user, password, cloud = camera_login(cam)
    client_for = client_for or (lambda: sdclip.build_client(host, user, password, cloud))
    done = {}
    fetched = False
    for start in starts:
        target = os.path.join(out, str(start))
        if has_frames(target):
            log(f"{start}: frames already present, skipped")
            done[start] = None
            continue
        if fetched and pause:
            sleep(pause)
        fetched = True
        os.makedirs(target, exist_ok=True)
        try:
            frames = fetch(client_for(), start, out_dir=target, span=span, every=every,
                           rotate=getattr(cam, "rotate", 0))
        except Exception as exc:  # noqa: BLE001 - log and go on with the next event
            log(f"{start}: error {exc!r}")
            done[start] = 0
            continue
        done[start] = len(frames)
        log(f"{start}: {len(frames)} frames")
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("config", help="cameras.yaml of this host")
    ap.add_argument("camera", help="camera name as in the config")
    ap.add_argument("--sent-log", default=None,
                    help="sent-photo archive dir (default: $TAPO_SENT_LOG_DIR)")
    ap.add_argument("--days", type=float, default=2.0, help="look back this many days (2)")
    ap.add_argument("--out", required=True, help="output dir; one subdir per event")
    ap.add_argument("--span", type=int, default=30, help="seconds of clip per event (30)")
    ap.add_argument("--every", type=int, default=1, help="seconds between frames (1)")
    ap.add_argument("--pause", type=float, default=5.0, help="seconds between events (5)")
    args = ap.parse_args(argv)

    sent_dir = args.sent_log or sentlog.archive_dir_from_env()
    if not sent_dir:
        print("no sent-log dir: pass --sent-log or set TAPO_SENT_LOG_DIR", file=sys.stderr)
        return 2
    cam = find_camera(config_mod.load_config(args.config), args.camera)
    if cam is None:
        print(f"camera {args.camera!r} is not in {args.config}", file=sys.stderr)
        return 2
    _host, user, password, _cloud = camera_login(cam)
    if not user or not password:
        print(f"credentials missing: set the env vars {cam.user_env} and {cam.password_env}",
              file=sys.stderr)
        return 2
    rows = read_index(os.path.join(sent_dir, sentlog.INDEX_NAME))
    starts = event_starts(rows, time.time(), args.days, camera=args.camera)
    print(f"{len(starts)} SD events in the last {args.days:g} days", flush=True)
    dump(cam, starts, args.out, args.span, args.every, args.pause,
         log=lambda m: print(m, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
