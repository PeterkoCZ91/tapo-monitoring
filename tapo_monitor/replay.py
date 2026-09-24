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
the ledger is opened read-only.

Two things from the ledger's ``decisions`` table narrow that upper bound:

* **Recorded non-live deliveries.** An SD follow-up, sampler or hub-clip ``send`` that
  reached Telegram (``telegram=1``) is replayed at its ``observed_at`` as a
  ``delivered[<path>]`` line and commits to the gates the way that path's production code
  does (``on_alert``; the sampler also records its scene delivery), so later live events
  see the cooldown it armed. These are recorded facts, not re-decisions: replay does not
  ask whether the delivery would still happen under another config (only the mute gate
  is re-applied).
* **Threshold what-if.** Where the live decision for an event carries a recorded scorer
  confidence, the replayed config's ``scorer.threshold`` — or its ``night_threshold``
  when the camera's night was on at ``observed_at``, as the daemon picks it per tick — is
  applied to it (see :func:`_threshold_outcome`). Frames that were never scored — a
  snapshot failure, a scorer outage, a camera without a scorer, frames the daemon never
  grabbed — cannot be re-thresholded and keep the gates-only answer.
* **Corroboration holds and their expiry.** A live frame recorded as ``hold`` never
  alerts by itself (see :func:`_hold_outcome`); the sampler's ``hold_expired`` drop for
  its group is replayed at the time it was recorded and asks the replayed config's
  ``sampler.hold_expiry`` whether the held frame would have been sent after all (see
  :func:`_replay_hold_expiry`), so ``--compare`` estimates what that policy adds.
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

from . import daemon, ledger, sampler, scheduling

WOULD_ALERT = "would_alert"
SUPPRESSED = "suppressed"
DELIVERED = "delivered"       # a recorded non-live delivery, replayed as a fact

# Paths whose recorded Telegram sends arm the alert gate in production (``on_alert``).
DELIVERY_PATHS = ("sd", "sampler", "hubpoll")
# Replay-only path of an expired corroboration hold: re-decided, not replayed as a fact.
HOLD_EXPIRY = "hold_expiry"
# Sampler decisions that end a hold by expiry, by (action, reason); the policy's own send
# and observe lines are the same expiry under another config, so they are re-decided too.
_EXPIRY_ROWS = {("drop", "hold_expired"), ("would_send", "hold_expiry_observe"),
                ("send", "hold_expiry_send")}
# Live actions whose recorded score was compared against ``scorer.threshold``.
_THRESHOLD_ACTIONS = ("send", "drop", "defer")


@dataclass(frozen=True)
class ReplayEvent:
    """One recorded camera detection: what the daemon saw and when it processed it.

    With ``path`` other than ``live`` it is instead a recorded non-live delivery (an SD
    follow-up, sampler or hub-clip send that reached Telegram) for that camera event, or
    with ``path`` :data:`HOLD_EXPIRY` the expiry of a held frame of the sampler group that
    event started, ``score`` then being the held frame's.
    """

    camera: str
    event_type: str
    event_at: float
    observed_at: float
    recorded: str | None = None   # the action production actually logged for this path
    path: str = "live"            # "live" camera event, or one of DELIVERY_PATHS
    score: float | None = None    # recorded scorer confidence of that decision, if any
    recorded_reason: str | None = None


@dataclass(frozen=True)
class Decision:
    event: ReplayEvent
    outcome: str                  # WOULD_ALERT, SUPPRESSED or DELIVERED
    reason: str | None            # why it was suppressed; None otherwise
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
            "path": self.event.path,
            "score": self.event.score,
        }


def load_events(path, start, end, cameras=None) -> list[ReplayEvent]:
    """Camera detections and recorded non-live deliveries in ``[start, end]``, read-only.

    Each camera event carries the action the live path recorded for it (``send``,
    ``cooldown``, ``scene_duplicate``, ``drop`` ...) and that decision's scorer
    confidence, when there is one, so a replay under the unchanged config can be checked
    against what production really did and a threshold change can be asked about.

    Every SD follow-up, sampler or hub-clip ``send`` that reached Telegram is returned as
    well, as a ``ReplayEvent`` whose ``path`` names that delivery path, and every expired
    sampler hold (``hold_expired``, or the expiry policy's ``hold_expiry_observe`` and
    ``hold_expiry_send`` lines, one event per group) as a :data:`HOLD_EXPIRY` event. The
    window selects by camera event time, like the camera events themselves.
    """
    observations, decisions = ledger.read_camera_window(path, start=start, end=end,
                                                        cameras=cameras, decision_paths=None)
    recorded = {}
    deliveries = []
    expiries: dict = {}
    for row in decisions:  # ordered by id: the last live action for an event wins
        key = (row["camera"], row["event_type"], row["event_at"])
        if row["path"] == "live":
            recorded[key] = row
        elif row["path"] == "sampler" and (row["action"], row["reason"]) in _EXPIRY_ROWS:
            # observe writes would_send and then drop for one expiry: keep the first.
            expiries.setdefault(key, ReplayEvent(
                camera=row["camera"], event_type=row["event_type"], event_at=row["event_at"],
                observed_at=row["observed_at"], recorded=row["action"], path=HOLD_EXPIRY,
                score=row["score"], recorded_reason=row["reason"]))
        elif row["path"] in DELIVERY_PATHS and row["action"] == "send" and row["telegram"]:
            deliveries.append(ReplayEvent(
                camera=row["camera"], event_type=row["event_type"], event_at=row["event_at"],
                observed_at=row["observed_at"], recorded="send", path=row["path"],
                score=row["score"], recorded_reason=row["reason"]))
    events = []
    for obs in observations:
        row = recorded.get((obs.camera, obs.event_type, obs.event_at)) or {}
        events.append(ReplayEvent(
            camera=obs.camera, event_type=obs.event_type, event_at=obs.event_at,
            observed_at=obs.observed_at, recorded=row.get("action"),
            score=row.get("score"), recorded_reason=row.get("reason")))
    return events + deliveries + list(expiries.values())


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


def _threshold_outcome(cfg, ev, night):
    """What the replayed ``scorer.threshold`` makes of a live event's recorded score.

    ``night`` is the astral night at the event's ``observed_at``; the threshold is the one
    :func:`tapo_monitor.daemon.scorer_threshold` picks from it, ``night_threshold`` during
    the camera's night, so a replay follows the tick the daemon handled the event in.

    Returns ``None`` when the threshold has no say (no recorded score, no scorer in the
    replayed config, a tamper event, or a recorded action the threshold did not decide —
    ``hold``, ``cooldown``, a ``drop`` for another reason ...), so the event keeps the
    gates-only answer. Otherwise one of:

    * ``"send"`` — the score clears the threshold: a live send (``would_alert``);
    * ``"drop"`` — bare motion under the threshold: no live send and nothing armed, as the
      live path's drop or recorder-look defer;
    * ``"defer"`` — a confirmed type under the threshold with ``sd_snapshot`` on: the live
      path hands it to the SD follow-up and arms the cooldown without sending.

    A confirmed type under the threshold without an SD path is still sent by the live
    path's always-send safety net, so it is ``"send"`` too.
    """
    if ev.score is None or not cfg.scorer.url or ev.event_type == "tamper":
        return None
    if ev.recorded not in _THRESHOLD_ACTIONS:
        return None
    if ev.recorded != "send" and ev.recorded_reason != "below_threshold":
        return None
    if ev.score >= daemon.scorer_threshold(cfg, night):
        return "send"
    if ev.event_type == "motion":
        return "drop"
    return "defer" if cfg.sd_snapshot else "send"


def _hold_outcome(cfg, ev, night):
    """What the replayed config makes of a live frame recorded as a corroboration ``hold``.

    A held frame sends nothing by itself — what became of it is recorded apart, as a later
    live or sampler send once corroborated, a ``hold_rescue_recall`` delivery, or a
    ``hold_expired`` expiry — so under a config that still holds it the event is
    ``"hold"``: suppressed, nothing armed. Its recorded score is re-judged like any other:
    under the replayed threshold it is ``"drop"``; at or over ``motion_send_threshold``, or
    with corroboration off (no ``motion_send_threshold``, or the sampler disabled), it is a
    plain ``"send"``. ``None`` when the event was not recorded as a scored hold.
    """
    if ev.recorded != "hold" or ev.score is None or not cfg.scorer.url:
        return None
    if ev.score < daemon.scorer_threshold(cfg, night):
        return "drop"
    send_now = cfg.scorer.motion_send_threshold
    if cfg.sampler.enabled and send_now is not None and ev.score < send_now:
        return "hold"
    return "send"


def _replay_hold_expiry(app, cfg, state, ev, night, *, scene_gate=True):
    """Re-decide one recorded hold expiry under the replayed ``sampler.hold_expiry``.

    Mirrors ``daemon._expire_hold`` after the pan-limit rescue (a rescued hold never
    expires, its send is a recorded delivery): with the policy ``off`` — or no
    corroboration at all — the hold stays ``hold_expired``; otherwise the held score must
    reach the floor (``hold_expiry_min_score``, else the threshold the daemon would apply
    at that time) and the motion cooldown and the scene group must allow it, asked the way
    a sampler send asks them. ``observe`` stops there as ``hold_expiry_observe`` and arms
    nothing; ``send`` is ``would_alert`` and commits like a sampler delivery. Whether the
    review log still had the frame is not in the ledger and is assumed.
    """
    now = ev.observed_at
    policy = cfg.sampler.hold_expiry
    if (not cfg.sampler.enabled or cfg.scorer.motion_send_threshold is None
            or policy == "off"):
        return Decision(ev, SUPPRESSED, "hold_expired", night)
    floor = sampler.hold_expiry_floor(cfg.sampler, daemon.scorer_threshold(cfg, night))
    if ev.score is None or ev.score < floor:
        return Decision(ev, SUPPRESSED, "hold_expiry_floor", night)
    event = {"start_time": ev.event_at}
    can_alert, on_alert = daemon.alert_gate(state, cfg.name, app.alerts.cooldown, now)
    if not can_alert("motion"):
        return Decision(ev, SUPPRESSED, "cooldown", night)
    group, window = cfg.coordinator.group, cfg.coordinator.scene_window
    if scene_gate and not state.scene_coordinator.allows(group, cfg.name, "motion", event,
                                                         now, window=window):
        return Decision(ev, SUPPRESSED, "scene_duplicate", night)
    if policy == "observe":
        return Decision(ev, SUPPRESSED, "hold_expiry_observe", night)
    on_alert("motion")
    state.scene_coordinator.record_delivery(group, cfg.name, "motion", event, now,
                                            window=window)
    return Decision(ev, WOULD_ALERT, None, night)


def _replay_delivery(app, cfg, state, ev, night):
    """Commit a recorded non-live delivery to the gates the way its path does."""
    now = ev.observed_at
    event = {"start_time": ev.event_at}
    _, on_alert = daemon.alert_gate(state, cfg.name, app.alerts.cooldown, now)
    if ev.path == "sampler":
        # process_sampler arms the gate without the event and records its scene delivery.
        on_alert(ev.event_type)
        state.scene_coordinator.record_delivery(
            cfg.coordinator.group, cfg.name, ev.event_type, event, now,
            window=cfg.coordinator.scene_window)
    else:
        on_alert(ev.event_type, event)   # SD follow-up and hub clip
    return Decision(ev, DELIVERED, None, night)


def replay(app, events, *, is_night=None, scene_gate=True) -> list[Decision]:
    """Run ``events`` through the production gates under ``app``; one Decision each.

    Events are processed by ``observed_at`` (when the daemon actually handled them), which
    is also the ``now`` every gate is asked with, as in the live pass. Recorded non-live
    deliveries (``path`` other than ``live``) are replayed in the same timeline and only
    arm the gates. ``scene_gate=False`` skips the overlapping-camera group check, which is
    how :func:`scene_reach` measures what that gate removes.
    """
    is_night = is_night or default_is_night(app)
    cameras = {cam.name: cam for cam in app.cameras}
    state = daemon.MonitorState()
    decisions = []
    # At one timestamp the live event goes first: its record precedes any follow-up.
    for ev in sorted(events, key=lambda e: (e.observed_at, e.path != "live", e.event_at)):
        cfg = cameras.get(ev.camera)
        if cfg is None:
            decisions.append(Decision(ev, SUPPRESSED, "unknown_camera"))
            continue
        if ev.path != "live":
            night = is_night(ev.observed_at)
            if daemon.camera_muted(cfg, night, ev.observed_at):
                decisions.append(Decision(ev, SUPPRESSED, _mute_reason(cfg), night))
            elif ev.path == HOLD_EXPIRY:
                decisions.append(_replay_hold_expiry(app, cfg, state, ev, night,
                                                     scene_gate=scene_gate))
            else:
                decisions.append(_replay_delivery(app, cfg, state, ev, night))
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
        if scene_gate and not state.scene_coordinator.allows(group, cfg.name, ev.event_type,
                                                             event, now, window=window):
            decisions.append(Decision(ev, SUPPRESSED, "scene_duplicate", night))
            continue
        # The live path scores after both gates, so the threshold comes last here too.
        verdict = _threshold_outcome(cfg, ev, night) or _hold_outcome(cfg, ev, night)
        if verdict == "hold":
            decisions.append(Decision(ev, SUPPRESSED, "hold", night))
            continue
        if verdict == "drop":
            decisions.append(Decision(ev, SUPPRESSED, "threshold", night))
            continue
        if verdict == "defer":
            on_alert(ev.event_type, event)   # the live defer arms the cooldown, sends nothing
            decisions.append(Decision(ev, SUPPRESSED, "threshold_defer", night))
            continue
        # Treated as delivered: the same two commits a successful live send makes.
        on_alert(ev.event_type, event)
        state.scene_coordinator.record_delivery(group, cfg.name, ev.event_type, event, now,
                                                window=window)
        decisions.append(Decision(ev, WOULD_ALERT, None, night))
    return decisions


def summarize(decisions) -> dict:
    """Per camera: live event count, would-alert count, suppressions by reason, and the
    recorded non-live deliveries replayed, by path. A muted delivery is not a live event;
    it counts as a suppression keyed ``<path>:<reason>``. Hold expiries count apart as
    well: ``hold_expiry`` is how many the replayed policy would send, and the rest are
    suppressions keyed ``hold_expiry:<reason>``."""
    summary: dict[str, dict] = {}
    for d in decisions:
        entry = summary.setdefault(d.event.camera,
                                   {"events": 0, "would_alert": 0, "suppressed": Counter(),
                                    "delivered": Counter(), "hold_expiry": 0})
        if d.event.path != "live":
            if d.outcome == WOULD_ALERT:
                entry["hold_expiry"] += 1
            elif d.outcome == DELIVERED:
                entry["delivered"][d.event.path] += 1
            else:
                entry["suppressed"][f"{d.event.path}:{d.reason}"] += 1
            continue
        entry["events"] += 1
        if d.outcome == WOULD_ALERT:
            entry["would_alert"] += 1
        else:
            entry["suppressed"][d.reason] += 1
    for entry in summary.values():
        entry["suppressed"] = dict(sorted(entry["suppressed"].items()))
        entry["delivered"] = dict(sorted(entry["delivered"].items()))
    return summary


def scene_reach(app, events, *, is_night=None, decisions=None) -> dict:
    """Per camera: live ``would_alert`` without and with the scene gate, and the gap.

    This is the gate's reach on the recorded window — how many alerts it removed once its
    knock-on effect on cooldowns is included — rather than the raw ``scene_duplicate``
    count. ``decisions`` may pass an already computed gated replay of the same input.
    """
    is_night = is_night or default_is_night(app)
    gated = summarize(decisions if decisions is not None
                      else replay(app, events, is_night=is_night))
    ungated = summarize(replay(app, events, is_night=is_night, scene_gate=False))
    reach = {}
    for camera in sorted(set(gated) | set(ungated)):
        without = ungated.get(camera, {}).get("would_alert", 0)
        with_gate = gated.get(camera, {}).get("would_alert", 0)
        reach[camera] = {"without_gate": without, "with_gate": with_gate,
                         "removed": without - with_gate}
    return reach


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
    text = d.outcome if d.reason is None else f"{d.outcome}({d.reason})"
    return text if d.event.path == "live" else f"{text}[{d.event.path}]"


def _fmt_score(d):
    return f"  score={d.event.score:.2f}" if d.event.score is not None else ""


def _print_text(decisions, summary, differences, compare_path, *, summary_only=False,
                reach=None):
    width = max([len(d.event.camera) for d in decisions] + [6])
    if not summary_only:
        for d in decisions:
            recorded = f"  [recorded: {d.event.recorded}]" if d.event.recorded else ""
            print(f"{_fmt_time(d.event.event_at)}  {d.event.camera:<{width}}  "
                  f"{d.event.event_type:<7} {_fmt_outcome(d)}{recorded}{_fmt_score(d)}")
    if not decisions:
        print("no camera events in this window")
    if not summary_only:
        print()
    totals = Counter()
    for camera, entry in sorted(summary.items()):
        suppressed = ", ".join(f"{k}={v}" for k, v in entry["suppressed"].items()) or "none"
        delivered = ", ".join(f"{k}={v}" for k, v in entry["delivered"].items())
        line = (f"{camera}: {entry['events']} events, {entry['would_alert']} would alert; "
                f"suppressed: {suppressed}")
        if delivered:
            line += f"; recorded non-live deliveries: {delivered}"
        if entry["hold_expiry"]:
            line += f"; held frames sent on expiry: {entry['hold_expiry']}"
        print(line)
        totals["events"] += entry["events"]
        totals["would_alert"] += entry["would_alert"]
        totals["hold_expiry"] += entry["hold_expiry"]
        totals["delivered"] += sum(entry["delivered"].values())
        totals["scene_duplicate"] += entry["suppressed"].get("scene_duplicate", 0)
    if len(summary) > 1:
        print(f"total: {totals['events']} events, {totals['would_alert']} would alert, "
              f"{totals['delivered']} recorded non-live deliveries, "
              f"{totals['scene_duplicate']} scene_duplicate"
              + (f", {totals['hold_expiry']} held frames sent on expiry"
                 if totals["hold_expiry"] else ""))
    if reach is not None:
        print()
        print("scene gate reach (live would_alert without -> with the gate):")
        for camera, entry in reach.items():
            print(f"  {camera}: {entry['without_gate']} -> {entry['with_gate']} "
                  f"(removed {entry['removed']})")
        print(f"  total removed: {sum(e['removed'] for e in reach.values())}")
    if differences is not None:
        print()
        print(f"differences vs {compare_path}: {len(differences)}")
        if summary_only:
            return
        for base, other in differences:
            print(f"  {_fmt_time(base.event.event_at)}  {base.event.camera:<{width}}  "
                  f"{base.event.event_type:<7} {_fmt_outcome(base)} -> {_fmt_outcome(other)}")


def main(argv=None, *, now=None) -> int:
    from .config import ConfigError, load_config

    parser = argparse.ArgumentParser(
        prog="tapo-monitor replay",
        description="Replay recorded camera events from the ledger through the alert "
                    "gates (mute, cooldown, scene group, recorded scorer threshold) under "
                    "a config, with recorded SD/sampler/hub deliveries arming the "
                    "cooldown. Read-only.",
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
    parser.add_argument("--summary-only", action="store_true",
                        help="print only the per-camera summary (and difference count)")
    parser.add_argument("--scene-reach", action="store_true",
                        help="also replay without the scene gate and report how many "
                             "alerts the gate removed per camera")
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

    is_night = default_is_night(app)
    decisions = replay(app, events, is_night=is_night)
    summary = summarize(decisions)
    differences = compare(decisions, replay(other, events)) if other is not None else None
    reach = (scene_reach(app, events, is_night=is_night, decisions=decisions)
             if args.scene_reach else None)

    if args.json_output:
        report = {
            "config": args.config,
            "start": start,
            "end": end,
            "summary": summary,
        }
        if not args.summary_only:
            report["decisions"] = [d.as_dict() for d in decisions]
        if reach is not None:
            report["scene_reach"] = reach
        if differences is not None:
            report["compare"] = {"config": args.compare, "count": len(differences)}
            if not args.summary_only:
                report["compare"]["differences"] = [
                    {"camera": b.event.camera, "event_type": b.event.event_type,
                     "event_at": b.event.event_at, "path": b.event.path,
                     "base": {"outcome": b.outcome, "reason": b.reason},
                     "other": {"outcome": o.outcome, "reason": o.reason}}
                    for b, o in differences
                ]
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    _print_text(decisions, summary, differences, args.compare,
                summary_only=args.summary_only, reach=reach)
    return 0
