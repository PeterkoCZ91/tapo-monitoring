"""Replay a recorded window of camera events through the daemon's alert gates.

``tapo-monitor replay cameras.yaml --start ... --end ...`` reads the camera detections
the daemon wrote to the event ledger and pushes them, in the order the daemon saw them,
through the same gate functions the live getEvents path uses:

1. the camera must exist and have ``getevents`` among its detection sources;
2. :func:`tapo_monitor.daemon.camera_muted` (``night_only`` / ``quiet_hours``);
3. :func:`tapo_monitor.daemon.alert_gate` (per-camera, per-type cooldown);
4. :meth:`tapo_monitor.scene.SceneCoordinator.allows` (overlapping-camera group gate).

An event that clears all four is reported ``would_alert`` and committed to the gates as
a delivery, exactly as a successful Telegram send would. Media, the scorer and Telegram
are out of scope: nothing here grabs a frame, contacts a camera or opens a socket, and
the ledger is opened read-only. That makes the result an upper bound on alerts for the
gate policy under test — a frame the scorer would have dropped still counts.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from . import daemon, ledger, scheduling

WOULD_ALERT = "would_alert"
SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class ReplayEvent:
    """One recorded camera detection: what the daemon saw and when it processed it."""

    camera: str
    event_type: str
    event_at: float
    observed_at: float
    recorded: str | None = None   # the live-path action production actually logged


@dataclass(frozen=True)
class Decision:
    event: ReplayEvent
    outcome: str                  # WOULD_ALERT or SUPPRESSED
    reason: str | None            # why it was suppressed; None when it would alert
    night: bool | None = None     # the night flag the mute gate was given

    def as_dict(self) -> dict:
        return {
            "camera": self.event.camera,
            "event_type": self.event.event_type,
            "event_at": self.event.event_at,
            "observed_at": self.event.observed_at,
            "outcome": self.outcome,
            "reason": self.reason,
            "night": self.night,
            "recorded": self.event.recorded,
        }


def load_events(path, start, end, cameras=None) -> list[ReplayEvent]:
    """Camera detections in ``[start, end]`` from the ledger at ``path``, read-only.

    Each event carries the action the live path recorded for it (``send``,
    ``cooldown``, ``scene_duplicate``, ``drop`` ...), when there is one, so a replay under
    the unchanged config can be checked against what production really did.
    """
    observations, decisions = ledger.read_camera_window(path, start=start, end=end,
                                                        cameras=cameras)
    recorded = {}
    for row in decisions:  # ordered by id: the last live action for an event wins
        recorded[(row["camera"], row["event_type"], row["event_at"])] = row["action"]
    return [
        ReplayEvent(camera=obs.camera, event_type=obs.event_type, event_at=obs.event_at,
                    observed_at=obs.observed_at,
                    recorded=recorded.get((obs.camera, obs.event_type, obs.event_at)))
        for obs in observations
    ]


def default_is_night(app):
    """``ts -> bool`` using the daemon's :func:`scheduling.is_night`, cached per minute.

    The timestamp is placed in the site's timezone (``location.tz`` or ``NIGHT_TZ``) so a
    replay on a workstation in another zone still asks about the site's night. The astral
    fallback warning is printed once, not once per event.
    """
    tz = None
    tz_name = getattr(app.location, "tz", None) or os.getenv("NIGHT_TZ")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 - fall back to host-local time, as the daemon would
            tz = None
    cache: dict[int, bool] = {}
    warned = []

    def night(ts):
        key = int(ts // 60)
        if key not in cache:
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                cache[key] = bool(scheduling.is_night(datetime.fromtimestamp(ts, tz),
                                                      location=app.location))
            if captured.getvalue() and not warned:
                warned.append(True)
                sys.stderr.write(captured.getvalue())
        return cache[key]

    return night


def _mute_reason(cfg):
    # Labels only: whether the camera is muted is decided by daemon.camera_muted.
    return "night_only" if cfg.night_only else "quiet_hours"


def replay(app, events, *, is_night=None) -> list[Decision]:
    """Run ``events`` through the production gates under ``app``; one Decision each.

    Events are processed by ``observed_at`` (when the daemon actually handled them), which
    is also the ``now`` every gate is asked with, as in the live pass.
    """
    is_night = is_night or default_is_night(app)
    cameras = {cam.name: cam for cam in app.cameras}
    state = daemon.MonitorState()
    decisions = []
    for ev in sorted(events, key=lambda e: (e.observed_at, e.event_at)):
        cfg = cameras.get(ev.camera)
        if cfg is None:
            decisions.append(Decision(ev, SUPPRESSED, "unknown_camera"))
            continue
        if "getevents" not in cfg.detection.sources:
            decisions.append(Decision(ev, SUPPRESSED, "source_disabled"))
            continue
        now = ev.observed_at
        night = is_night(now)
        if daemon.camera_muted(cfg, night, now):
            decisions.append(Decision(ev, SUPPRESSED, _mute_reason(cfg), night))
            continue
        event = {"start_time": ev.event_at}
        can_alert, on_alert = daemon.alert_gate(state, cfg.name, app.alerts.cooldown, now)
        if not can_alert(ev.event_type, event):
            decisions.append(Decision(ev, SUPPRESSED, "cooldown", night))
            continue
        group, window = cfg.coordinator.group, cfg.coordinator.scene_window
        if not state.scene_coordinator.allows(group, cfg.name, ev.event_type, event, now,
                                              window=window):
            decisions.append(Decision(ev, SUPPRESSED, "scene_duplicate", night))
            continue
        # Treated as delivered: the same two commits a successful live send makes.
        on_alert(ev.event_type, event)
        state.scene_coordinator.record_delivery(group, cfg.name, ev.event_type, event, now,
                                                window=window)
        decisions.append(Decision(ev, WOULD_ALERT, None, night))
    return decisions


def summarize(decisions) -> dict:
    """Per camera: event count, would-alert count and suppressions by reason."""
    summary: dict[str, dict] = {}
    for d in decisions:
        entry = summary.setdefault(d.event.camera,
                                   {"events": 0, "would_alert": 0, "suppressed": Counter()})
        entry["events"] += 1
        if d.outcome == WOULD_ALERT:
            entry["would_alert"] += 1
        else:
            entry["suppressed"][d.reason] += 1
    for entry in summary.values():
        entry["suppressed"] = dict(sorted(entry["suppressed"].items()))
    return summary


def compare(base, other) -> list[tuple[Decision, Decision]]:
    """Pairs of decisions for the same event whose outcome or reason differ."""
    by_event = {d.event: d for d in other}
    return [(b, by_event[b.event]) for b in base
            if b.event in by_event
            and (b.outcome, b.reason) != (by_event[b.event].outcome, by_event[b.event].reason)]


# ── command line ──────────────────────────────────────────────────────────────

def _parse_time(text):
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is neither a Unix timestamp nor an ISO date/time") from None


def _fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_outcome(d):
    return d.outcome if d.reason is None else f"{d.outcome}({d.reason})"


def _print_text(decisions, summary, differences, compare_path):
    width = max([len(d.event.camera) for d in decisions] + [6])
    for d in decisions:
        recorded = f"  [recorded: {d.event.recorded}]" if d.event.recorded else ""
        print(f"{_fmt_time(d.event.event_at)}  {d.event.camera:<{width}}  "
              f"{d.event.event_type:<7} {_fmt_outcome(d)}{recorded}")
    if not decisions:
        print("no camera events in this window")
    print()
    for camera, entry in sorted(summary.items()):
        suppressed = ", ".join(f"{k}={v}" for k, v in entry["suppressed"].items()) or "none"
        print(f"{camera}: {entry['events']} events, {entry['would_alert']} would alert; "
              f"suppressed: {suppressed}")
    if differences is not None:
        print()
        print(f"differences vs {compare_path}: {len(differences)}")
        for base, other in differences:
            print(f"  {_fmt_time(base.event.event_at)}  {base.event.camera:<{width}}  "
                  f"{base.event.event_type:<7} {_fmt_outcome(base)} -> {_fmt_outcome(other)}")


def main(argv=None, *, now=None) -> int:
    from .config import ConfigError, load_config

    parser = argparse.ArgumentParser(
        prog="tapo-monitor replay",
        description="Replay recorded camera events from the ledger through the alert "
                    "gates (mute, cooldown, scene group) under a config. Read-only.",
    )
    parser.add_argument("config", nargs="?", default="cameras.yaml")
    parser.add_argument("--ledger", dest="ledger_path",
                        help="event ledger (default: TAPO_LEDGER_FILE or the XDG state path)")
    parser.add_argument("--start", type=_parse_time,
                        help="window start: Unix timestamp or ISO local time")
    parser.add_argument("--end", type=_parse_time,
                        help="window end: Unix timestamp or ISO local time (default: now)")
    parser.add_argument("--hours", type=float, default=12.0,
                        help="window length when --start is omitted (default 12)")
    parser.add_argument("--camera", action="append", dest="cameras",
                        help="only this camera (repeatable)")
    parser.add_argument("--compare", metavar="OTHER_YAML",
                        help="also replay under this config and list events that differ")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    end = args.end if args.end is not None else (time.time() if now is None else now)
    if args.start is None and args.hours <= 0:
        parser.error("--hours must be greater than zero")
    start = args.start if args.start is not None else max(0.0, end - args.hours * 3600)
    if end < start:
        parser.error("--end must not precede --start")

    try:
        app = load_config(args.config)
        other = load_config(args.compare) if args.compare else None
    except (OSError, ConfigError) as exc:
        print(f"replay: config: {exc}", file=sys.stderr)
        return 1
    path = args.ledger_path or ledger.default_ledger_path()
    try:
        events = load_events(path, start, end, cameras=args.cameras)
    except (OSError, ValueError) as exc:
        print(f"replay: ledger: {exc}", file=sys.stderr)
        return 1

    decisions = replay(app, events)
    summary = summarize(decisions)
    differences = compare(decisions, replay(other, events)) if other is not None else None

    if args.json_output:
        report = {
            "config": args.config,
            "start": start,
            "end": end,
            "decisions": [d.as_dict() for d in decisions],
            "summary": summary,
        }
        if differences is not None:
            report["compare"] = {
                "config": args.compare,
                "differences": [
                    {"camera": b.event.camera, "event_type": b.event.event_type,
                     "event_at": b.event.event_at,
                     "base": {"outcome": b.outcome, "reason": b.reason},
                     "other": {"outcome": o.outcome, "reason": o.reason}}
                    for b, o in differences
                ],
            }
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    _print_text(decisions, summary, differences, args.compare)
    return 0
