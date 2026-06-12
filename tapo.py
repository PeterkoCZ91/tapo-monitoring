#!/usr/bin/env python3
"""
tapo.py — command-line interface for Tapo C560WS monitoring stack.

Usage:
  python tapo.py events      [--hours N] [--limit N] [--person-only] [--json]
  python tapo.py clip TIME   [--duration N] [--out FILE]
  python tapo.py recordings  [--date YYYY-MM-DD] [--event-only] [--json]
  python tapo.py status
  python tapo.py face
  python tapo.py preset list
  python tapo.py preset set N

Credentials are read from environment variables or tapo-camera.env file:
  TAPO_IP, TAPO_EMAIL, TAPO_PASSWORD
"""

import argparse
import asyncio
import atexit
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

PERSON_BIT = 524288  # events_1 bit 19 = camera AI confirmed person


# ── Environment ───────────────────────────────────────────────────────────────

def _load_env_file():
    """Load tapo-camera.env without overriding already-set env vars."""
    for candidate in ["tapo-camera.env", str(Path(__file__).parent / "tapo-camera.env")]:
        p = Path(candidate)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
        break


def _connect():
    ip       = os.getenv("TAPO_IP", "")
    email    = os.getenv("TAPO_EMAIL", "")
    password = os.getenv("TAPO_PASSWORD", "")
    missing  = [n for n, v in {"TAPO_IP": ip, "TAPO_EMAIL": email, "TAPO_PASSWORD": password}.items() if not v]
    if missing:
        sys.exit(f"Error: missing env vars: {', '.join(missing)}\n"
                 "Set them directly or create tapo-camera.env (see tapo-camera.env.example)")
    from pytapo import Tapo
    try:
        cam = Tapo(ip, email, password, password)
    except Exception as e:
        sys.exit(f"Error: could not connect to {ip}: {e}")

    # Cleanly close pytapo's internal asyncio loop on exit to avoid fd cleanup noise
    def _close_loop():
        try:
            loop = cam.asyncHandler._loop
            if loop and not loop.is_closed():
                loop.close()
        except Exception:
            pass
    atexit.register(_close_loop)

    return cam


def _face_map():
    mapping = {}
    for pair in os.getenv("FACE_ID_NAMES", "").split(","):
        if ":" in pair:
            fid, _, name = pair.strip().partition(":")
            try:
                mapping[int(fid)] = name.strip()
            except ValueError:
                pass
    return mapping


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_dur(seconds):
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m {s}s" if s else f"{m}m"


def _parse_time(s):
    """Parse HH:MM, HH:MM:SS, or unix timestamp to local epoch seconds."""
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt)
            return int(datetime.combine(date.today(), t.time()).timestamp())
        except ValueError:
            continue
    sys.exit(f"Error: cannot parse time {s!r} — use HH:MM, HH:MM:SS, or unix timestamp")


def _parse_recordings(raw):
    clips = []
    for item in raw:
        for val in item.values():
            if isinstance(val, dict) and "startTime" in val:
                clips.append(val)
    return clips


def _best_clip(clips, raw_start, raw_end):
    event = continuous = None
    for c in clips:
        if c["startTime"] > raw_end or c["endTime"] < raw_start:
            continue
        vt = str(c.get("vedio_type", ""))
        if vt == "2" and event is None:
            event = c
        elif vt == "1" and continuous is None:
            continuous = c
    return event or continuous


def _face_label(event_info, mapping):
    ids = [f["face_id"] for f in (event_info or []) if "face_id" in f]
    if not ids:
        return ""
    labels  = [mapping.get(i) for i in ids]
    known   = [l for l in labels if l]
    unknown = labels.count(None)
    if known and not unknown:
        return "👤 " + ", ".join(known)
    if known and unknown:
        return "👤 " + ", ".join(known) + " + stranger"
    return "👤 stranger"


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_events(args):
    cam = _connect()
    fm  = _face_map()
    now = int(time.time())

    try:
        events = cam.getEvents(startTime=now - int(args.hours * 3600), endTime=now) or []
    except Exception as e:
        sys.exit(f"Error fetching events: {e}")

    if args.person_only:
        events = [e for e in events if e.get("events_1", 0) & PERSON_BIT]

    events = sorted(events, key=lambda e: e.get("start_time", 0), reverse=True)
    if args.limit:
        events = events[:args.limit]

    if args.json:
        print(json.dumps(events, indent=2, default=str))
        return

    if not events:
        print("No events found.")
        return

    print(f"{'TIME':<10} {'TYPE':<10} {'AI':<4} {'ALARM':<7} {'DUR':<8} FACE")
    print("─" * 60)
    for ev in events:
        t     = datetime.fromtimestamp(ev.get("start_time", 0)).strftime("%H:%M:%S")
        etype = (ev.get("event_type") or "motion").lower()
        ai    = "✓" if ev.get("events_1", 0) & PERSON_BIT else "·"
        alarm = str(ev.get("alarm_type", "·"))
        dur   = _fmt_dur(ev.get("end_time", ev.get("start_time", 0)) - ev.get("start_time", 0))
        face  = _face_label(ev.get("event_info"), fm)
        print(f"{t:<10} {etype:<10} {ai:<4} {alarm:<7} {dur:<8} {face}")

    print(f"\n{len(events)} event(s)  (last {args.hours:.0f}h{'  ·  person-only' if args.person_only else ''})")


def cmd_clip(args):
    cam = _connect()
    tc  = cam.getTimeCorrection()
    if tc is False:
        tc = 0

    clip_time = _parse_time(args.time)
    raw_time  = clip_time - tc
    time_str  = datetime.fromtimestamp(clip_time).strftime("%H:%M:%S")

    print(f"Searching recordings around {time_str} (tc={tc:+d}s)...")

    try:
        raw = cam.getRecordingsUTC(int(raw_time - 120), int(raw_time + 120))
    except Exception as e:
        sys.exit(f"Error: getRecordingsUTC failed: {e}")

    if not raw:
        sys.exit("No recordings found in ±2 minute window.")

    clips = _parse_recordings(raw)
    clip  = _best_clip(clips, raw_time - 120, raw_time + 120)

    if not clip:
        print(f"  Found {len(clips)} clips in window but none overlap {time_str}.")
        print("  Available clips:")
        for c in sorted(clips, key=lambda x: x["startTime"]):
            s = datetime.fromtimestamp(c["startTime"] + tc).strftime("%H:%M:%S")
            e = datetime.fromtimestamp(c["endTime"]   + tc).strftime("%H:%M:%S")
            vt = "event" if str(c.get("vedio_type")) == "2" else "continuous"
            print(f"    {s}–{e} ({vt})")
        sys.exit(1)

    vt       = str(clip.get("vedio_type", "?"))
    vt_label = "event" if vt == "2" else "continuous"
    dl_start = raw_time if vt == "1" else clip["startTime"]
    dl_end   = min(clip["endTime"], dl_start + args.duration)
    duration = dl_end - dl_start

    c_start = datetime.fromtimestamp(clip["startTime"] + tc).strftime("%H:%M:%S")
    c_end   = datetime.fromtimestamp(clip["endTime"]   + tc).strftime("%H:%M:%S")
    print(f"Found {vt_label} clip {c_start}–{c_end}, downloading {_fmt_dur(duration)}...")

    out      = args.out or f"clip_{datetime.fromtimestamp(clip_time).strftime('%H-%M')}.mp4"
    out_path = Path(out).resolve()
    out_dir  = str(out_path.parent) + "/"
    out_name = out_path.name

    async def _dl():
        from pytapo.media_stream.downloader import Downloader
        dl = Downloader(cam, int(dl_start), int(dl_end), tc,
                        out_dir, overwriteFiles=True, fileName=out_name)
        prev = None
        async for status in dl.download():
            action = status.get("currentAction", "")
            if action != prev:
                print(f"  {action}...")
                prev = action

    asyncio.run(_dl())

    if out_path.exists() and out_path.stat().st_size > 0:
        mb = out_path.stat().st_size / 1_000_000
        print(f"Saved: {out_path}  ({mb:.1f} MB)")
    else:
        sys.exit("Error: download failed or produced an empty file.")


def cmd_recordings(args):
    cam = _connect()
    tc  = cam.getTimeCorrection()
    if tc is False:
        tc = 0

    if args.date:
        try:
            d = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            sys.exit(f"Error: invalid date {args.date!r} — use YYYY-MM-DD")
    else:
        d = date.today()

    day_start = int(datetime(d.year, d.month, d.day).timestamp()) - tc
    day_end   = day_start + 86400

    print(f"Recordings for {d}:")

    try:
        raw = cam.getRecordingsUTC(day_start, day_end)
    except Exception as e:
        sys.exit(f"Error: {e}")

    if not raw:
        print("  No recordings.")
        return

    clips = _parse_recordings(raw)
    if args.event_only:
        clips = [c for c in clips if str(c.get("vedio_type")) == "2"]

    clips = sorted(clips, key=lambda c: c.get("startTime", 0))

    if args.json:
        print(json.dumps(clips, indent=2, default=str))
        return

    if not clips:
        print("  No recordings match filter.")
        return

    print(f"\n{'START':<10} {'END':<10} {'DURATION':<10} TYPE")
    print("─" * 42)
    for c in clips:
        s   = datetime.fromtimestamp(c["startTime"] + tc).strftime("%H:%M:%S")
        e   = datetime.fromtimestamp(c["endTime"]   + tc).strftime("%H:%M:%S")
        dur = _fmt_dur(c["endTime"] - c["startTime"])
        vt  = str(c.get("vedio_type", "?"))
        lbl = "event" if vt == "2" else "continuous" if vt == "1" else f"type={vt}"
        print(f"{s:<10} {e:<10} {dur:<10} {lbl}")

    ev_n  = sum(1 for c in clips if str(c.get("vedio_type")) == "2")
    cnt_n = len(clips) - ev_n
    print(f"\n{len(clips)} clips total  ({ev_n} event, {cnt_n} continuous)")


def cmd_status(args):
    cam = _connect()
    ip  = os.getenv("TAPO_IP", "?")

    # Basic device info
    try:
        info = cam.getBasicInfo().get("device_info", {}).get("basic_info", {})
    except Exception:
        info = {}

    model    = info.get("device_model") or info.get("model", "Tapo")
    fw       = info.get("sw_version") or info.get("firmware_version", "?")
    hw       = info.get("hw_version") or info.get("hardware_version", "?")
    alias    = info.get("device_alias") or info.get("nickname", "")
    name_str = f" ({alias})" if alias else ""
    print(f"Camera:    {model}{name_str}  [{ip}]")
    print(f"Firmware:  {fw}   Hardware: {hw}")

    # Time correction
    tc = cam.getTimeCorrection()
    if tc is not False:
        print(f"Time correction: {tc:+d}s")

    # Presets
    try:
        presets = cam.getPresets()
        if presets:
            plist = "  ".join(
                f"{pid}={name}"
                for pid, name in sorted(presets.items(), key=lambda x: int(x[0]))
            )
            print(f"Presets:   {plist}")
    except Exception:
        pass

    # Person detection
    try:
        pdet    = cam.getPersonDetection()
        det     = pdet.get("detection", {})
        enabled = det.get("enabled", "?")
        sens    = det.get("sensitivity", "?")
        print(f"Person detection: {enabled}  sensitivity: {sens}")
    except Exception:
        pass

    # SD card summary for today
    tc_val = tc if isinstance(tc, int) else 0
    today  = date.today()
    day_start = int(datetime(today.year, today.month, today.day).timestamp()) - tc_val
    try:
        raw    = cam.getRecordingsUTC(day_start, day_start + 86400) or []
        clips  = _parse_recordings(raw)
        ev_n   = sum(1 for c in clips if str(c.get("vedio_type")) == "2")
        cont_n = len(clips) - ev_n
        print(f"SD today:  {len(clips)} clips  ({ev_n} event, {cont_n} continuous)")
    except Exception:
        pass


def cmd_face(args):
    cam = _connect()
    fm  = _face_map()

    try:
        result = cam.executeFunction(
            "getFaceDetectionConfig",
            {"face_detection": {"name": ["detection"]}}
        )
        cfg = result.get("face_detection", {}).get("detection", {})
    except Exception as e:
        sys.exit(f"Error reading face detection config: {e}")

    enabled  = cfg.get("enabled", "?")
    sens     = cfg.get("sensitivity", "?")
    tags     = cfg.get("tags", [])
    max_fam  = cfg.get("max_familiar_face_num", "?")
    max_str  = cfg.get("max_stranger_face_num", "?")
    max_sub  = cfg.get("max_sub_face_img", "?")
    last_mac = cfg.get("last_device_mac", "")

    print(f"Face detection:  {enabled}  sensitivity: {sens}")
    print(f"Capacity:        {max_fam} familiar / {max_str} stranger  ({max_sub} images/profile)")
    if tags:
        print(f"Tags:            {', '.join(tags)}")
    if last_mac:
        mac_fmt = (
            ":".join(last_mac[i:i+2] for i in range(0, 12, 2))
            if len(last_mac) == 12 else last_mac
        )
        print(f"Last configured: {mac_fmt}  (MAC of last device that called setFaceDetectionConfig)")

    if fm:
        print("\nFACE_ID_NAMES (from env):")
        for fid, name in sorted(fm.items()):
            print(f"  {fid}  →  {name}")
    else:
        print("\nNo FACE_ID_NAMES set. Add to tapo-camera.env to label faces in alerts:")
        print("  FACE_ID_NAMES=1234567890:alice,9876543210:bob")
        print("  (run 'python tapo.py events' and look for '👤 stranger' to discover IDs)")


def cmd_preset(args):
    cam = _connect()

    if args.preset_cmd == "list":
        try:
            presets = cam.getPresets()
        except Exception as e:
            sys.exit(f"Error: {e}")
        if not presets:
            print("No presets configured.")
            return
        print(f"{'ID':<5} NAME")
        print("─" * 20)
        for pid, name in sorted(presets.items(), key=lambda x: int(x[0])):
            print(f"{pid:<5} {name}")

    elif args.preset_cmd == "set":
        n = str(args.preset_id)
        print(f"Moving to preset {n}...", end=" ", flush=True)
        try:
            cam.setPreset(n)
            print("OK")
        except Exception as e:
            print()
            sys.exit(f"Error: {e}")


# ── Argument parsing ──────────────────────────────────────────────────────────

def _build_parser():
    parser = argparse.ArgumentParser(
        prog="tapo.py",
        description="CLI for Tapo C560WS camera monitoring stack",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
credentials: TAPO_IP, TAPO_EMAIL, TAPO_PASSWORD  (env or tapo-camera.env)

examples:
  python tapo.py events --person-only --hours 2
  python tapo.py events --json | jq '.[0]'
  python tapo.py clip 14:12
  python tapo.py clip 14:12:45 --duration 30 --out /tmp/evidence.mp4
  python tapo.py recordings --event-only
  python tapo.py recordings --date 2026-06-03
  python tapo.py status
  python tapo.py face
  python tapo.py preset list
  python tapo.py preset set 1
""",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # events
    p = sub.add_parser("events", help="show recent camera events")
    p.add_argument("--hours", type=float, default=1.0, metavar="N",
                   help="look back N hours (default: 1)")
    p.add_argument("--limit", type=int, default=0, metavar="N",
                   help="show at most N results (default: all)")
    p.add_argument("--person-only", action="store_true",
                   help="only events with camera AI person detection (events_1 bit 19)")
    p.add_argument("--json", action="store_true", help="output raw JSON")

    # clip
    p = sub.add_parser("clip", help="download an SD card clip for a given time")
    p.add_argument("time", metavar="TIME",
                   help="time of clip: HH:MM, HH:MM:SS, or unix timestamp")
    p.add_argument("--duration", type=int, default=10, metavar="SEC",
                   help="clip length in seconds (default: 10)")
    p.add_argument("--out", metavar="FILE",
                   help="output filename (default: clip_HH-MM.mp4)")

    # recordings
    p = sub.add_parser("recordings", help="list SD card recordings")
    p.add_argument("--date", metavar="YYYY-MM-DD",
                   help="date to list (default: today)")
    p.add_argument("--event-only", action="store_true",
                   help="only event-triggered clips (vedio_type=2)")
    p.add_argument("--json", action="store_true", help="output raw JSON")

    # status
    sub.add_parser("status", help="camera status and info")

    # face
    sub.add_parser("face", help="face detection config and enrolled IDs")

    # preset
    p = sub.add_parser("preset", help="list or activate camera presets")
    psub = p.add_subparsers(dest="preset_cmd", metavar="ACTION")
    psub.required = True
    psub.add_parser("list", help="list all presets")
    ps = psub.add_parser("set", help="move camera to a preset")
    ps.add_argument("preset_id", metavar="N", help="preset ID")

    return parser


def main():
    _load_env_file()
    args = _build_parser().parse_args()
    {
        "events":     cmd_events,
        "clip":       cmd_clip,
        "recordings": cmd_recordings,
        "status":     cmd_status,
        "face":       cmd_face,
        "preset":     cmd_preset,
    }[args.command](args)


if __name__ == "__main__":
    main()
