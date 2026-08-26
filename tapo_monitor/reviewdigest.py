"""Opt-in daily Telegram digest of the corroboration review log.

The review log (``TAPO_REVIEW_LOG_DIR``) quietly accumulates the frames corroboration
suppressed — exactly the evidence needed to judge whether holds are dropping ghosts or
people, and exactly the folder nobody opens. When ``TAPO_REVIEW_DIGEST_TIME`` is set to
a local ``HH:MM``, the daemon sends one summary message per day at that time, plus the
few highest-scoring suppressed frames, so the day's holds walk into Telegram on their
own. A digest goes out even on a quiet day: silence must mean "nothing suppressed",
never "the digest broke".

Best-effort like the rest of the sent-log family: a failure logs and returns, it never
raises into the daemon loop, and a failed send retries on the next tick rather than
being marked done.
"""

import json
import logging
import os
import time

from . import notify, sentlog
from .shadowscan import SUMMARY_MAX_AGE, SUMMARY_NAME

log = logging.getLogger(__name__)

ENV_TIME = "TAPO_REVIEW_DIGEST_TIME"
ENV_MAX_PHOTOS = "TAPO_REVIEW_DIGEST_MAX_PHOTOS"
DEFAULT_MAX_PHOTOS = 4
WINDOW_SECONDS = 86400.0
STATE_NAME = ".digest-sent"


def digest_time_from_env(env=None):
    """Configured local send time as ``(hour, minute)``, or None when off/garbage."""
    env = os.environ if env is None else env
    raw = (env.get(ENV_TIME) or "").strip()
    try:
        hour, minute = (int(part) for part in raw.split(":"))
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def max_photos_from_env(env=None):
    """Photo cap per digest; falls back to the default on missing/garbage input."""
    env = os.environ if env is None else env
    try:
        return max(0, int(env[ENV_MAX_PHOTOS]))
    except (KeyError, TypeError, ValueError):
        return DEFAULT_MAX_PHOTOS


def _day(now):
    return time.strftime("%Y-%m-%d", time.localtime(now))


def due(now, hhmm, last_day):
    """True when today's digest time has passed and today's digest is unsent. Pure."""
    local = time.localtime(now)
    if (local.tm_hour, local.tm_min) < tuple(hhmm):
        return False
    return last_day != _day(now)


def last_sent_day(review_dir):
    """Day stamp of the last delivered digest, or None."""
    try:
        with open(os.path.join(review_dir, STATE_NAME), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def mark_sent(review_dir, now):
    """Record that today's digest went out (best-effort)."""
    try:
        os.makedirs(review_dir, exist_ok=True)
        with open(os.path.join(review_dir, STATE_NAME), "w", encoding="utf-8") as f:
            f.write(_day(now))
    except OSError:
        log.debug("review digest: could not write state", exc_info=True)


def collect(review_dir, now, window=WINDOW_SECONDS):
    """Review-log index entries from the last ``window`` seconds, oldest first."""
    entries = []
    try:
        with open(os.path.join(review_dir, sentlog.INDEX_NAME), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return entries
    for line in lines:
        try:
            record = json.loads(line)
            ts = float(record["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - ts <= window and record.get("file"):
            entries.append(record)
    return entries


def _camera_stats(entries):
    cameras = {}
    for e in entries:
        cam = str(e.get("camera", "cam"))
        best = cameras.setdefault(cam, {"count": 0, "max": 0.0})
        best["count"] += 1
        try:
            best["max"] = max(best["max"], float(e.get("person", 0.0)))
        except (TypeError, ValueError):
            pass
    return cameras


def _camera_lines(cameras):
    lines = []
    for cam in sorted(cameras, key=lambda c: -cameras[c]["count"]):
        stats = cameras[cam]
        lines.append(f"{cam}: {stats['count']} (max p{stats['max']:.2f})")
    return lines


def build_summary(entries):
    """One-message digest text: total plus per-camera counts with the max score.

    Entries with ``verdict == "shadow"`` (the shadow scan's own miss candidates) are
    broken out into a separate section below the regular hold counts; everything else
    (including entries with no verdict at all) keeps today's rendering unchanged. Pure.
    """
    if not entries:
        return "\U0001f4cb Review digest: no suppressed frames in the last 24h"
    holds = [e for e in entries if e.get("verdict") != "shadow"]
    shadow = [e for e in entries if e.get("verdict") == "shadow"]
    lines = [f"\U0001f4cb Review digest: {len(holds)} suppressed frame(s) in the last 24h"]
    lines.extend(_camera_lines(_camera_stats(holds)))
    if shadow:
        lines.append(f"shadow: {len(shadow)} miss candidate(s)")
        lines.extend(_camera_lines(_camera_stats(shadow)))
    return "\n".join(lines)


def alert_lines(sent_dir, now, window=WINDOW_SECONDS):
    """What actually reached the phone in the window, from the sent-log index.

    The index is the only durable record: an in-memory counter resets on every restart,
    and a restart is exactly when you most want to know what the day looked like. A
    refused delivery is counted apart, because an alert Telegram rejected did not happen.
    Returns [] when there is no sent log — silence beats an invented zero.
    """
    if not sent_dir:
        return []
    delivered, refused = {}, 0
    try:
        with open(os.path.join(sent_dir, sentlog.INDEX_NAME), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    for line in lines:
        try:
            record = json.loads(line)
            ts = float(record["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - ts > window or not record.get("file"):
            continue
        if record.get("delivered", True):
            camera = str(record.get("camera", "cam"))
            delivered[camera] = delivered.get(camera, 0) + 1
        else:
            refused += 1
    total = sum(delivered.values())
    if not total and not refused:
        return ["alerts 24h: none"]
    detail = ", ".join(f"{cam} {delivered[cam]}"
                       for cam in sorted(delivered, key=lambda c: -delivered[c]))
    line = f"alerts 24h: {total} sent"
    if detail:
        line += f" ({detail})"
    if refused:
        line += f", {refused} undelivered"
    return [line]


def fleet_lines(health):
    """Render a fleet-health snapshot for the digest. Pure.

    Two rules decide everything here. It may only claim "OK" for what it actually looked
    at, so a subsystem the caller could not check (no recorder on this host, no scorer
    metrics) produces no line at all rather than a reassuring one. And any failed check
    takes the headline away: a heartbeat that says OK while a camera is unreachable is
    worse than no heartbeat, because it converts a silence you might notice into a
    confirmation you will trust.
    """
    if not isinstance(health, dict):
        return []
    problems, detail = [], []

    cameras = health.get("cameras") or {}
    # Three states, not two. Before the first control pass reachability is unknown, and
    # neither "unreachable" (cries wolf) nor silence (lets OK cover it) is honest.
    reachable = [n for n, s in sorted(cameras.items()) if (s or {}).get("reachable") is True]
    unreachable = [n for n, s in sorted(cameras.items())
                   if (s or {}).get("reachable") is False]
    unchecked = [n for n, s in sorted(cameras.items())
                 if (s or {}).get("reachable") is None]
    no_events = [n for n, s in sorted(cameras.items())
                 if (s or {}).get("reachable") is True and (s or {}).get("events") is False]
    if unreachable:
        problems.append(f"{', '.join(unreachable)} unreachable")
    if no_events:
        problems.append(f"{', '.join(no_events)} event API down")
    if unchecked:
        detail.append(f"not checked yet: {', '.join(unchecked)}")

    tick = health.get("tick") or {}
    if tick.get("ok") is False:
        stalled = tick.get("stalled_for")
        problems.append("daemon tick stalled"
                        + (f" for {notify.format_duration(stalled)}" if stalled else ""))

    scorer = health.get("scorer")
    if isinstance(scorer, dict):
        if scorer.get("ok"):
            detail.append(
                f"scorer {scorer.get('failed', 0)} failed / "
                f"{scorer.get('requests', 0)} req, p95 {float(scorer.get('p95', 0.0)):.2f}s")
            if scorer.get("failed"):
                problems.append(f"scorer {scorer['failed']} failed request(s)")
        else:
            problems.append(f"scorer unreachable ({scorer.get('error', 'no answer')})")

    recorder = health.get("recorder")
    if isinstance(recorder, dict):
        age = recorder.get("age_s")
        shown = f"{int(age)}s old" if age is not None else "age unknown"
        detail.append(f"recorder newest file {shown}")
        if recorder.get("status") != "ok":
            problems.append(f"recorder {recorder.get('status', 'unknown')}")

    repairs = health.get("repairs") or {}
    refused = {name: count for name, count in sorted(repairs.items()) if count}
    if refused:
        problems.append("self-heal refused: "
                        + ", ".join(f"{name} {count}\u00d7" for name, count in refused.items()))

    if problems:
        head = "\U0001f7e0 Fleet degraded — " + "; ".join(problems)
    elif not cameras:
        return []
    else:
        head = "\U0001f49a Fleet OK — " + ", ".join(reachable) + " reachable"
    return [head] + [f"   {line}" for line in detail]


def scan_context_line(review_dir, now):
    """One-line shadow-scan status for the digest, or None to stay silent.

    None when the summary file is absent (hosts without a recorder never mention the
    scan) or on any read/parse failure. A stale ``generated_at`` (older than
    ``SUMMARY_MAX_AGE``) renders a plain "no recent run" notice instead of numbers that
    might be a day or more out of date; a fresh summary renders the totals across all
    cameras. Best-effort: never raises.
    """
    try:
        with open(os.path.join(review_dir, SUMMARY_NAME), encoding="utf-8") as f:
            summary = json.load(f)
        generated_at = float(summary["generated_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if now - generated_at > SUMMARY_MAX_AGE:
        return "shadow scan: no recent run"
    cameras = summary.get("cameras")
    if not isinstance(cameras, dict):
        return None
    segments = frames = matched = candidates = skipped = 0
    for stats in cameras.values():
        try:
            segments += int(stats.get("segments", 0) or 0)
            frames += int(stats.get("frames_scored", 0) or 0)
            matched += int(stats.get("matched", 0) or 0)
            candidates += int(stats.get("shadow_only", 0) or 0)
            skipped += int(stats.get("segments_skipped", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return None
    # Covered, not merely present: a run that spent its decode budget reports the same
    # segment total as a complete one, so reading only that total made a scan that had
    # left 74 % of a camera's day untouched look finished.
    line = (f"shadow scan {summary.get('date')}: {segments - skipped} of {segments} "
            f"segments, {frames} frames, {matched} matched, {candidates} candidate(s)")
    if skipped:
        line += f" — {skipped} skipped (decode budget spent)"
    return line


def pick_photos(entries, review_dir, limit):
    """The ``limit`` highest-scoring entries whose frame still exists on disk. Pure-ish."""
    def score(e):
        try:
            return float(e.get("person", 0.0))
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted(entries, key=score, reverse=True)
    picked = []
    for e in ranked:
        if len(picked) >= limit:
            break
        if os.path.isfile(os.path.join(review_dir, str(e["file"]))):
            picked.append(e)
    return picked


def photo_caption(entry):
    """Skimmable per-photo caption: verdict, camera, score, local event time. Pure.

    Shadow entries record ``ts`` as the (much later) scan-run time; the true observation
    time lives in ``event_ts`` when the shadow worker set it, so that takes priority.
    """
    when = time.strftime(
        "%H:%M", time.localtime(float(entry.get("event_ts", entry.get("ts", 0.0)))))
    try:
        score = f"p{float(entry.get('person', 0.0)):.2f}"
    except (TypeError, ValueError):
        score = "p?"
    return f"{entry.get('verdict', 'hold')} {entry.get('camera', 'cam')} {score} {when}"


def run_if_due(*, env=None, now=None, send_text, send_photo, health=None):
    """Send the daily digest when configured and due. Returns True when it went out.

    A failed text send leaves the day unmarked so the next tick retries; photos are
    best-effort on top of a delivered summary. Never raises.

    ``health`` is an optional fleet snapshot from the caller. With it, this one message a
    day is also the only positive "everything is alive" signal the fleet sends: every
    other Telegram message is a transition, so without it a dead host and a quiet night
    look identical. Omitting it keeps the previous message shape exactly.
    """
    env = os.environ if env is None else env
    hhmm = digest_time_from_env(env)
    review_dir = (env.get(sentlog.ENV_REVIEW_DIR) or "").strip() or None
    if hhmm is None or review_dir is None:
        return False
    now = time.time() if now is None else now
    try:
        if not due(now, hhmm, last_sent_day(review_dir)):
            return False
        entries = collect(review_dir, now)
        text = build_summary(entries)
        context = scan_context_line(review_dir, now)
        if context is not None:
            text = f"{text}\n{context}"
        fleet = fleet_lines(health)
        alerts = alert_lines(sentlog.archive_dir_from_env(env), now)
        # Alerts belong under the fleet header when there is one: the indent is what says
        # these numbers describe the fleet the header just vouched for.
        sections = fleet + [f"   {line}" for line in alerts] if fleet else alerts
        if sections:
            text = text + "\n\n" + "\n".join(sections)
        if not send_text(text):
            log.warning("review digest delivery failed; will retry next tick")
            return False
        photos = 0
        for entry in pick_photos(entries, review_dir, max_photos_from_env(env)):
            send_photo(os.path.join(review_dir, str(entry["file"])), photo_caption(entry))
            photos += 1
        mark_sent(review_dir, now)
        # Say it out loud. Only failures used to log, so a working digest was
        # indistinguishable in the journal from one that had quietly stopped running, and
        # the only evidence either way was the state file.
        log.info("review digest sent: %d suppressed frame(s), %d photo(s)",
                 len(entries), photos)
        return True
    except Exception:  # noqa: BLE001 - telemetry must never break the daemon loop
        log.warning("review digest failed", exc_info=True)
        return False
