"""One-shot internal scene scoring — grab a live frame, score it, never send it.

A calibration/diagnosis aid: the daemon only scores frames behind camera events and,
for ``night_only`` cameras, only at night. This probe grabs a current RTSP frame from
named cameras on demand, scores it through the same YOLO service, prints person/animal
plus the best-tile score, and saves the frame with its scores in the filename. It has
no Telegram path and does not touch the running daemon — it only reads config + env and
talks to the scorer, exactly like the daemon's own snapshot/score helpers.

Run it (credentials come from the same env vars the daemon reads — TAPO_USER,
RTSP_USER, ...):

    python -m tapo_monitor.scene_probe cameras.yaml front yard
"""

import argparse
import json
import logging
import os
import sys
import time

from . import config, scorer, sentlog, snapshot
from .config import resolve_rtsp_credentials

log = logging.getLogger(__name__)

DEFAULT_ARCHIVE_DIR = os.path.expanduser("~/tapo-monitor/probe-log")
DEFAULT_TILES = 2
DEFAULT_PRUNE_DAYS = 7.0


def select_cameras(config, names):
    """Return the CameraConfigs for ``names`` (in that order), or all when empty.

    Raises ValueError naming any camera that is not in the config, so a typo fails
    loudly instead of silently probing nothing.
    """
    by_name = {cam.name: cam for cam in config.cameras}
    if not names:
        return list(config.cameras)
    unknown = [n for n in names if n not in by_name]
    if unknown:
        known = ", ".join(sorted(by_name))
        raise ValueError(f"unknown camera(s): {', '.join(unknown)} (known: {known})")
    return [by_name[n] for n in names]


def summarize(result):
    """Fold a scorer response into the fields worth eyeballing. Pure.

    ``person``/``animal`` are the full-frame decision scores; ``tile_person`` is the best
    tile (only present when scored with tiles>1) — the value the old tile-max bug alerted
    on. ``top_classes`` lists the strongest non-person COCO classes so an animal/vehicle
    misread is visible.
    """
    def num(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    classes = result.get("classes") if isinstance(result, dict) else None
    top = []
    if isinstance(classes, dict):
        ranked = sorted(((k, num(v)) for k, v in classes.items() if k != "person"),
                        key=lambda kv: kv[1], reverse=True)
        top = [[k, v] for k, v in ranked[:3]]
    tile = result.get("tile_person") if isinstance(result, dict) else None
    box = result.get("box") if isinstance(result, dict) else None
    return {
        "person": num(result.get("person")),
        "animal": num(result.get("animal")),
        "tile_person": None if tile is None else num(tile),
        "top_classes": top,
        "box": box if isinstance(box, list) else None,
    }


def format_line(name, summary):
    """One compact human-readable line for a probed camera. Pure."""
    parts = [f"{name:<10} person={summary['person']:.2f}  animal={summary['animal']:.2f}"]
    if summary["tile_person"] is not None:
        parts.append(f"tile2={summary['tile_person']:.2f}")
    if summary["top_classes"]:
        top = ", ".join(f"{n}:{s:.2f}" for n, s in summary["top_classes"])
        parts.append(f"top=[{top}]")
    if summary["box"]:
        parts.append(f"box={summary['box']}")
    return "  ".join(parts)


def probe_filename(name, summary, now):
    """Archive filename with scores baked in, from a filesystem-safe camera name. Pure."""
    safe = "".join(c if (c.isalnum() or c in "-.") else "_" for c in name).strip("_.") or "cam"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"{safe}_p{summary['person']:.2f}_a{summary['animal']:.2f}_{stamp}.jpg"


def _archive(archive_dir, name, image_path, summary, now, prune_days):
    """Copy the probed frame + a structured index line; prune old frames. Best-effort."""
    try:
        os.makedirs(archive_dir, exist_ok=True)
        with open(image_path, "rb") as f:
            data = f.read()
        fname = probe_filename(name, summary, now)
        with open(os.path.join(archive_dir, fname), "wb") as f:
            f.write(data)
        record = {"ts": now, "camera": name, "file": fname, **summary}
        with open(os.path.join(archive_dir, "index.jsonl"), "a", encoding="utf-8") as idx:
            idx.write(json.dumps(record, ensure_ascii=False) + "\n")
        sentlog.prune_old(archive_dir, now, prune_days)
        return fname
    except OSError:
        log.warning("probe: could not archive frame for %s", name, exc_info=True)
        return None


def run(config, names, *, capture, score, archive_dir=None, now=None,
        prune_days=DEFAULT_PRUNE_DAYS):
    """Grab + score each selected camera. Returns one record per camera.

    ``capture(cfg) -> image_path|None`` and ``score(cfg, image_path) -> result|None`` are
    injected so the orchestration is testable without ffmpeg or the network. The grabbed
    frame is ephemeral: it is archived (when ``archive_dir`` is set) and then unlinked.
    """
    now = time.time() if now is None else now
    records = []
    for cfg in select_cameras(config, names):
        image_path = capture(cfg)
        if not image_path:
            records.append({"camera": cfg.name, "ok": False, "error": "grab_failed"})
            continue
        try:
            result = score(cfg, image_path)
            if result is None:
                records.append({"camera": cfg.name, "ok": False, "error": "scorer_unavailable"})
                continue
            summary = summarize(result)
            saved = _archive(archive_dir, cfg.name, image_path, summary, now, prune_days) \
                if archive_dir else None
            records.append({"camera": cfg.name, "ok": True, "file": saved, **summary})
        finally:
            try:
                os.unlink(image_path)
            except OSError:
                pass
    return records


def _real_capture(cfg):
    user, password = resolve_rtsp_credentials(cfg)
    url = snapshot.rtsp_url(cfg.host, user, password, stream=cfg.rtsp_stream, port=cfg.rtsp_port)
    # Same rotation as the detection pipeline: a probe of a mis-mounted camera must score
    # the upright frame the daemon would score, not an upside-down one.
    return (snapshot.capture_rtsp(url, timeout=cfg.rtsp_timeout, rotate=cfg.rotate)
            or snapshot.capture_rtsp(url, timeout=cfg.rtsp_timeout, rotate=cfg.rotate))


def _real_score(tiles):
    def score(cfg, image_path):
        if not cfg.scorer.url:
            return None
        return scorer.score_image(cfg.scorer.url, image_path,
                                  timeout=cfg.scorer.timeout, tiles=tiles)
    return score


def main(argv=None):
    parser = argparse.ArgumentParser(description="Grab + internally score live frames (no Telegram).")
    parser.add_argument("config", help="path to cameras.yaml")
    parser.add_argument("cameras", nargs="*", help="camera names to probe (default: all)")
    parser.add_argument("--tiles", type=int, default=DEFAULT_TILES,
                        help="score full-frame + tiles×tiles grid (default 2, shows tile_person)")
    parser.add_argument("--archive-dir", default=DEFAULT_ARCHIVE_DIR,
                        help=f"where to save probed frames (default {DEFAULT_ARCHIVE_DIR})")
    parser.add_argument("--no-archive", action="store_true", help="print scores only, save nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    cfg = config.load_config(args.config)
    archive_dir = None if args.no_archive else args.archive_dir
    records = run(cfg, args.cameras, capture=_real_capture, score=_real_score(args.tiles),
                  archive_dir=archive_dir)
    for rec in records:
        if rec["ok"]:
            print(format_line(rec["camera"], rec))
        else:
            print(f"{rec['camera']:<10} FAILED: {rec['error']}")
    if archive_dir and any(r["ok"] for r in records):
        print(f"\nframes saved to {archive_dir}")
    return 0 if all(r["ok"] for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
