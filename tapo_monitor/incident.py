"""Incident identity: one ID from the camera event to the delivered photo.

An alert leaves traces in several places — audit lines in the journal, observation and
decision rows in the ledger, a frame in the sent log — and until this existed they could
only be matched by camera and a timestamp that differs a little in each. The incident
ID is derived, not issued: ``<camera>-<event start, whole seconds>``. Every path that
handles an event already carries the event (live, SD follow-up, sampler group, hub
retry), so they all compute the same ID without any shared state, and it survives a
restart for free.

``tapo-monitor incident <id>`` prints the chain for one ID from the ledger and the sent
log. Read-only; it never touches a camera or the network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def incident_id(camera, event):
    """``<camera>-<start>`` for an event, or None without a usable start time. Pure."""
    try:
        start = int(float(event.get("start_time")))
    except (AttributeError, TypeError, ValueError):
        return None
    if start <= 0:
        return None
    return f"{camera}-{start}"


def parse(value):
    """``(camera, start)`` from an incident ID. Raises ValueError when malformed. Pure."""
    camera, sep, start = str(value).rpartition("-")
    if not sep or not camera or not start.isdigit():
        raise ValueError(f"not an incident id: {value!r} (expected <camera>-<start>)")
    return camera, int(start)


def index_fields(value):
    """``incident`` and ``event_start`` for an archive index record. Pure, never raises.

    ``event_start`` is the whole-second start the ID is named after, stored on its own so
    a reader can group and time frames without parsing IDs. No ID (a frame with no camera
    event behind it) gives no fields; an ID that does not parse keeps just the ID.
    """
    if not value:
        return {}
    try:
        _camera, start = parse(value)
    except ValueError:
        return {"incident": value}
    return {"incident": value, "event_start": start}


def _sent_entries(sent_dir, value):
    if not sent_dir:
        return []
    from .sentlog import INDEX_NAME
    try:
        with open(os.path.join(sent_dir, INDEX_NAME), encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("incident") == value:
            out.append(record)
    return out


def chain(value, event_ledger, sent_dir=None):
    """Everything recorded for one incident, as plain dicts in time order."""
    camera, start = parse(value)
    # The ledger keeps the camera's float start; the ID keeps whole seconds.
    window = (start, start + 0.999999)
    observations = [
        {"source": o.source, "adapter": o.adapter, "event_type": o.event_type,
         "event_at": o.event_at, "observed_at": o.observed_at, "confidence": o.confidence}
        for o in event_ledger.observations(camera=camera, start=window[0], end=window[1])
    ]
    decisions = event_ledger.decisions(camera=camera, start=window[0], end=window[1])
    return {"incident": value, "camera": camera, "start": start,
            "observations": observations, "decisions": decisions,
            "sent": _sent_entries(sent_dir, value)}


def _offset(at, start):
    return f"{at - start:+.1f}s"


def format_chain(found):
    start = found["start"]
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start))
    lines = [f"incident {found['incident']}: camera {found['camera']}, event at {when}"]
    for o in found["observations"]:
        lines.append(f"  {_offset(o['observed_at'], start):>8}  observed {o['source']} "
                     f"{o['event_type']} ({o['adapter']})")
    for d in found["decisions"]:
        extra = []
        if d.get("score") is not None:
            extra.append(f"score={d['score']:.2f}")
        if d.get("telegram") is not None:
            extra.append(f"telegram={'yes' if d['telegram'] else 'no'}")
        if d.get("reason"):
            extra.append(f"reason={d['reason']}")
        lines.append(f"  {_offset(d['observed_at'], start):>8}  {d['path']} {d['action']} "
                     f"{d['event_type']} {' '.join(extra)}".rstrip())
    for s in found["sent"]:
        state = "delivered" if s.get("delivered") else "NOT delivered"
        lines.append(f"  {_offset(s.get('ts', start), start):>8}  sent log {s.get('file')} "
                     f"({state}): {s.get('caption', '')}")
    if len(lines) == 1:
        lines.append("  nothing recorded (ledger off, or older than its retention)")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tapo-monitor incident",
        description="Show everything recorded for one incident ID")
    parser.add_argument("incident", help="<camera>-<event start>, as in audit lines")
    parser.add_argument("--ledger", help="ledger path (default: TAPO_LEDGER_FILE / XDG)")
    parser.add_argument("--sent-log", default=os.environ.get("TAPO_SENT_LOG_DIR"),
                        help="sent-log directory (default: TAPO_SENT_LOG_DIR)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    try:
        parse(args.incident)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    from .ledger import EventLedger
    found = chain(args.incident, EventLedger(args.ledger), args.sent_log)
    print(json.dumps(found, sort_keys=True) if args.json else format_chain(found))
    return 0
