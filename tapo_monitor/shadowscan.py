"""Independent nightly audit of local recorder segments (Phase 3 shadow worker).

Scans a date's 15-minute recorder segments without any camera involvement, scores
motion-candidate frames through the shared scorer, records shadow observations in the
event ledger, and archives miss-candidate frames to the review log. Observation-only:
it never contacts a camera and never changes configuration. See
docs/superpowers/specs/2026-08-13-shadow-worker-design.md.
"""

import argparse
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import recclip, scorer, sentlog

log = logging.getLogger(__name__)

DEFAULT_SCENE = 0.04
DEFAULT_SEGMENT_CAP = 8
DEFAULT_BUDGET = 1500
DEFAULT_RATE = 1.5
DEFAULT_CLUSTER_GAP = 180.0
DEFAULT_MATCH_WINDOW = 120.0
SCORER_ABORT_AFTER = 3
SUMMARY_NAME = ".shadow-scan.json"
SUMMARY_MAX_AGE = 36 * 3600.0

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def resolve_date(arg, now=None):
    """Normalize the --date argument to a local YYYY-MM-DD string."""
    if arg in (None, "", "yesterday"):
        now = time.time() if now is None else now
        return time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    if not _DATE_RE.match(arg):
        raise ValueError(f"not a date: {arg!r}")
    return arg


def segments_for_date(base_dir, host, date_str, lister=None):
    """(mkv_path, seg_start) for every segment of the date, sorted by start."""
    lister = lister or (lambda d: sorted(glob.glob(os.path.join(d, "zaznam_*.mkv"))))
    day_dir = os.path.join(base_dir, host, date_str)
    found = []
    for hour in range(24):
        for path in lister(os.path.join(day_dir, f"{hour:02d}")):
            try:
                found.append((path, recclip.parse_segment_start(path)))
            except ValueError:
                continue
    return sorted(found, key=lambda item: item[1])


def parse_showinfo_times(stderr_text):
    """Selected-frame offsets (seconds) from ffmpeg showinfo output. Pure."""
    return [float(m) for m in _PTS_RE.findall(stderr_text or "")]


def _run_ffmpeg(args):  # pragma: no cover - subprocess I/O
    proc = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          timeout=120)
    return proc.stderr.decode("utf-8", "replace")


def extract_candidates(mkv, seg_start, out_dir, base, *, runner=None,
                       scene=DEFAULT_SCENE, cap=DEFAULT_SEGMENT_CAP):
    """Scene-change candidate JPEGs (+1 uniform mid-segment frame) with epoch stamps.

    The uniform frame exists so a slow or static subject cannot slip through a
    scene-change-only filter. Any ffmpeg failure degrades to fewer frames, never raises.
    """
    runner = runner or _run_ffmpeg
    out = []
    pattern = os.path.join(out_dir, f"{base}_sc_%02d.jpg")
    try:
        stderr = runner([
            "ffmpeg", "-hide_banner", "-y", "-i", mkv,
            "-vf", f"select='gt(scene,{scene})',showinfo,scale=1280:-2",
            "-vsync", "vfr", "-frames:v", str(int(cap)), "-q:v", "2", pattern,
        ])
        times = parse_showinfo_times(stderr)
        for k, offset in enumerate(times[:cap], start=1):
            path = pattern % k
            if os.path.exists(path) and os.path.getsize(path) > 0:
                out.append((path, seg_start + offset))
    except Exception:  # noqa: BLE001 - a bad segment must not end the batch
        log.warning("shadow scan: scene extraction failed for %s", mkv, exc_info=True)
    mid = os.path.join(out_dir, f"{base}_mid.jpg")
    mid_offset = recclip.SEGMENT_SECONDS // 2
    try:
        runner(["ffmpeg", "-hide_banner", "-y", "-ss", str(mid_offset), "-i", mkv,
                "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "2", "-update", "1",
                mid])
        if os.path.exists(mid) and os.path.getsize(mid) > 0:
            out.append((mid, seg_start + float(mid_offset)))
    except Exception:  # noqa: BLE001
        log.warning("shadow scan: mid-frame extraction failed for %s", mkv, exc_info=True)
    return out


def score_candidates(candidates, url, threshold, *, rate=DEFAULT_RATE, budget,
                     score=None, sleep=None):
    """Score candidate frames through the shared scorer, politely and boundedly.

    The gap between requests keeps a batch from starving the live pipeline and other
    scorer consumers; the budget bounds the night's total work; three consecutive
    failures mean the scorer is down and the batch should stop pretending otherwise.
    """
    score = score or scorer.score_image
    sleep = sleep or time.sleep
    hits, scored, failures = [], 0, 0
    trimmed = aborted = False
    for index, (path, ts) in enumerate(candidates):
        if scored >= budget:
            trimmed = True
            break
        if index:
            sleep(rate)
        result = score(url, path, tiles=1)
        subject = scorer.subject_score(result) if result is not None else None
        if subject is None:
            failures += 1
            if failures >= SCORER_ABORT_AFTER:
                aborted = True
                break
            continue
        failures = 0
        scored += 1
        if float(subject) >= threshold:
            hits.append({"ts": ts, "path": path, "person": float(subject),
                         "box": scorer.subject_box(result)})
    return {"hits": hits, "scored": scored, "aborted": aborted, "trimmed": trimmed}


def cluster_hits(hits, gap=DEFAULT_CLUSTER_GAP):
    """Merge time-adjacent hits into observations (span + peak + best frame). Pure."""
    observations = []
    for hit in sorted(hits, key=lambda h: h["ts"]):
        if observations and hit["ts"] - observations[-1]["end"] <= gap:
            current = observations[-1]
            current["end"] = hit["ts"]
            if hit["person"] > current["peak"]:
                current["peak"] = hit["person"]
                current["frame"] = hit["path"]
        else:
            observations.append({"start": hit["ts"], "end": hit["ts"],
                                 "peak": hit["person"], "frame": hit["path"]})
    return observations


def write_summary(review_dir, summary):
    """Best-effort .shadow-scan.json beside the review log; never raises."""
    if not review_dir:
        return None
    try:
        os.makedirs(review_dir, exist_ok=True)
        path = os.path.join(review_dir, SUMMARY_NAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, sort_keys=True, separators=(",", ":"))
            f.write("\n")
        return path
    except OSError:
        log.warning("shadow scan: could not write summary", exc_info=True)
        return None


def run_scan(app, date_str, *, env=None, out_dir, budget=DEFAULT_BUDGET,
             rate=DEFAULT_RATE, match_window=DEFAULT_MATCH_WINDOW,
             ledger_factory=None, score=None, runner=None, now=None):
    """One observation-only pass over a date's recorder segments. Never raises."""
    from . import ledger as ledger_mod

    env = os.environ if env is None else env
    now = time.time() if now is None else now
    started = time.monotonic()
    root = (env.get("RECORDING_ROOT") or "").strip()
    if not root:
        log.warning("shadow scan: RECORDING_ROOT not set; nothing to scan")
    review_dir = (env.get(sentlog.ENV_REVIEW_DIR) or "").strip() or None
    summary = {"date": date_str, "generated_at": now, "duration_s": 0.0,
               "aborted": False, "trimmed": False, "cameras": {}}
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        log.warning("shadow scan: could not create out_dir %s", out_dir, exc_info=True)
    try:
        events = (ledger_factory or (lambda: ledger_mod.EventLedger(
            ledger_mod.default_ledger_path())))()
    except Exception:  # noqa: BLE001 - a broken ledger must not crash the batch
        log.warning("shadow scan: could not open event ledger", exc_info=True)
        summary["aborted"] = True
        summary["duration_s"] = round(time.monotonic() - started, 1)
        write_summary(review_dir, summary)
        return summary
    budget_left = int(budget)
    archive_counter = 0
    for cfg in app.cameras:
        if not cfg.scorer.url or not root:
            continue
        if not os.path.isdir(os.path.join(root, cfg.host)):
            continue
        segments = segments_for_date(root, cfg.host, date_str)
        candidates, per_cam = [], {"segments": len(segments), "frames_scored": 0,
                                   "observations": 0, "matched": 0, "shadow_only": 0}
        try:
            for index, (mkv, seg_start) in enumerate(segments):
                candidates.extend(extract_candidates(
                    mkv, seg_start, out_dir, f"{cfg.name}_{index:03d}", runner=runner))
            scored = score_candidates(candidates, cfg.scorer.url, cfg.scorer.threshold,
                                      rate=rate, budget=budget_left, score=score)
            per_cam["frames_scored"] = scored["scored"]
            budget_left -= scored["scored"]
            summary["aborted"] = summary["aborted"] or scored["aborted"]
            summary["trimmed"] = summary["trimmed"] or scored["trimmed"]
            for obs in cluster_hits(scored["hits"]):
                per_cam["observations"] += 1
                events.record_shadow_event(camera=cfg.name, event_type="person",
                                           event_at=obs["start"],
                                           confidence=obs["peak"],
                                           adapter="local_scorer")
                seen = events.camera_events_between(
                    cfg.name, obs["start"] - match_window, obs["end"] + match_window)
                if seen:
                    per_cam["matched"] += 1
                else:
                    per_cam["shadow_only"] += 1
                    meta = sentlog.review_meta(cfg.name, "shadow", "person", obs["peak"])
                    meta["event_ts"] = obs["start"]
                    # Every archived observation in this run must land on a distinct
                    # filename/index ts: `_stamp` is only unique to the microsecond, and
                    # two observations sharing a 2-decimal score would otherwise collide
                    # and silently overwrite each other's JPEG. A millisecond bump per
                    # observation keeps every entry safely inside the digest's 24h window.
                    sentlog.archive_review_if_configured(
                        obs["frame"], meta, now=now + archive_counter * 0.001, env=env)
                    archive_counter += 1
        except Exception:  # noqa: BLE001 - one camera must not end the batch
            log.warning("shadow scan: %s failed", cfg.name, exc_info=True)
        finally:
            for path, _ts in candidates:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        summary["cameras"][cfg.name] = per_cam
        if summary["aborted"]:
            break
    summary["duration_s"] = round(time.monotonic() - started, 1)
    write_summary(review_dir, summary)
    return summary


def main(argv):
    """Argparse entry point: nightly shadow-scan batch over one date's segments."""
    from .config import load_config

    parser = argparse.ArgumentParser(
        prog="tapo-monitor shadow-scan",
        description="Independent nightly audit of local recorder segments",
    )
    parser.add_argument("config", nargs="?", default="cameras.yaml")
    parser.add_argument("--date")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE)
    parser.add_argument("--match-window", type=float, default=DEFAULT_MATCH_WINDOW)
    parser.add_argument("--out-dir")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    try:
        date_str = resolve_date(args.date)
    except ValueError as exc:
        parser.error(str(exc))

    app = load_config(args.config)
    out_dir = args.out_dir or tempfile.mkdtemp(prefix="shadowscan-")
    try:
        summary = run_scan(app, date_str, out_dir=out_dir, budget=args.budget,
                           rate=args.rate, match_window=args.match_window)
    finally:
        if not args.out_dir:
            shutil.rmtree(out_dir, ignore_errors=True)

    cameras = summary.get("cameras", {})
    observations = sum(cam.get("observations", 0) for cam in cameras.values())
    log.info("shadow scan %s: %d camera(s), %d observation(s), %.1fs%s%s",
             summary.get("date"), len(cameras), observations,
             summary.get("duration_s", 0.0),
             " aborted" if summary.get("aborted") else "",
             " trimmed" if summary.get("trimmed") else "")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
