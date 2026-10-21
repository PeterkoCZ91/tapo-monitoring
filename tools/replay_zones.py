#!/usr/bin/env python3
"""Offline replay helpers for alert zones (V1) and loitering (V3).

Read-only: it scores saved frames against a scoring service you point it at, and reads
journal text. It never talks to a camera or a host.

Subcommands:

  score    FILES/DIRS... --url URL --out boxes.jsonl
           Score JPEGs once and cache ``person_scores``/``person_boxes`` (resumable).
  feet     boxes.jsonl [--min-score S]
           Foot position (bottom centre of the box, fractions of the frame) as a grid
           histogram per camera (camera = first path component).
  zones    boxes.jsonl --zones zones.json [--min-score S]
           Run ``scorer.apply_alert_zones`` and list the frames that lose a person or
           all of them. ``zones.json`` maps camera -> list of polygons of [x, y] points.
  loiter   audit.txt [--frames 4,6] [--tolerance 0,1] [--interval 30]
           Replay ``path=sampler`` audit lines through ``sampler.note_loiter``.
"""

import argparse
import collections
import json
import os
import re
import sys
import types
import urllib.request

from tapo_monitor import sampler, scorer


def _images(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in sorted(os.walk(p)):
                for name in sorted(files):
                    if name.lower().endswith(".jpg"):
                        yield os.path.join(root, name)
        else:
            yield p


def _camera(path, root):
    rel = os.path.relpath(path, root)
    return rel.split(os.sep)[0]


def _post(url, path, timeout, token=None):
    with open(path, "rb") as f:
        headers = {"Content-Type": "image/jpeg", **scorer.auth_headers(token)}
        req = scorer.keep_auth_off_redirects(
            urllib.request.Request(url, data=f.read(), headers=headers))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def cmd_score(args):
    done = set()
    if os.path.exists(args.out):
        with open(args.out) as f:
            done = {json.loads(line)["path"] for line in f if line.strip()}
    root = os.path.abspath(args.root)
    token = scorer.resolve_token(args.token_env)
    n = 0
    with open(args.out, "a") as out:
        for path in _images(args.inputs):
            rel = os.path.relpath(os.path.abspath(path), root)
            if rel in done:
                continue
            try:
                res = _post(args.url, path, args.timeout, token)
            except Exception as e:  # noqa: BLE001 - report and continue
                print(f"skip {rel}: {e}", file=sys.stderr)
                continue
            rec = {"path": rel, "w": res.get("w"), "h": res.get("h"),
                   "person_scores": res.get("person_scores", []),
                   "person_boxes": res.get("person_boxes", []),
                   "vehicle_scores": res.get("vehicle_scores", []),
                   "vehicle_boxes": res.get("vehicle_boxes", [])}
            out.write(json.dumps(rec) + "\n")
            n += 1
            if n % 200 == 0:
                out.flush()
                print(f"{n} scored", file=sys.stderr)
    print(f"scored {n} new frames -> {args.out}")


def _load(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def feet_of(rec, min_score):
    """Foot points ``(score, fx, fy)`` of the people at or above ``min_score``. Pure."""
    w, h = rec.get("w"), rec.get("h")
    if not w or not h:
        return []
    return [(s, (b[0] + b[2]) / 2 / w, b[3] / h)
            for s, b in zip(rec["person_scores"], rec["person_boxes"], strict=False)
            if s >= min_score]


def cmd_feet(args):
    by_cam = collections.defaultdict(list)
    for rec in _load(args.boxes):
        cam = rec["path"].split("/")[0]
        by_cam[cam].extend(feet_of(rec, args.min_score))
    for cam, pts in sorted(by_cam.items()):
        print(f"== {cam}: {len(pts)} people (score >= {args.min_score})")
        grid = collections.Counter((min(int(fx * args.cols), args.cols - 1),
                                    min(int(fy * args.rows), args.rows - 1)) for _, fx, fy in pts)
        print("   feet grid (rows = y top->bottom, cols = x left->right), counts:")
        for r in range(args.rows):
            print("   " + " ".join(f"{grid.get((c, r), 0):4d}" for c in range(args.cols)))
        xs = sorted(p[1] for p in pts)
        ys = sorted(p[2] for p in pts)
        if pts:
            def q(v, p):
                return v[min(int(p * len(v)), len(v) - 1)]
            print("   x p5/p50/p95: " + "/".join(f"{q(xs, p):.2f}" for p in (.05, .5, .95))
                  + "   y p5/p50/p95: " + "/".join(f"{q(ys, p):.2f}" for p in (.05, .5, .95)))


def cmd_zones(args):
    with open(args.zones) as f:
        zones_cfg = json.load(f)
    stats = collections.defaultdict(lambda: collections.Counter())
    listed = []
    for rec in _load(args.boxes):
        cam = rec["path"].split("/")[0]
        zones = zones_cfg.get(cam)
        if not zones:
            continue
        # Only people the scorer would count (>= min_score) take part, like the live gate.
        keep = [(s, b) for s, b in zip(rec["person_scores"], rec["person_boxes"], strict=False)
                if s >= args.min_score]
        if not keep:
            continue
        res = {"person_scores": [s for s, _ in keep], "person_boxes": [b for _, b in keep],
               "person": keep[0][0], "box": keep[0][1], "w": rec["w"], "h": rec["h"]}
        out = scorer.apply_alert_zones(res, [[tuple(p) for p in poly] for poly in zones])
        dropped = out.get("outside_alert_zones", 0)
        st = stats[cam]
        st["frames_with_person"] += 1
        st["people"] += len(keep)
        st["people_dropped"] += dropped
        if dropped == len(keep):
            st["frames_lost"] += 1
            listed.append((cam, "LOST", rec["path"], keep))
        elif dropped:
            st["frames_partial"] += 1
            listed.append((cam, "PARTIAL", rec["path"], keep))
    for cam, st in sorted(stats.items()):
        print(f"== {cam}: " + ", ".join(f"{k}={v}" for k, v in st.items()))
    print("\n# cases to check by eye (LOST = no person left, an alert would vanish)")
    for _cam, kind, path, keep in listed:
        feet = ", ".join(f"p{s:.2f}@({(b[0] + b[2]) / 2:.0f},{b[3]:.0f})" for s, b in keep)
        print(f"{kind}\t{path}\t{feet}")


_AUDIT = re.compile(r"(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*audit camera=(?P<cam>\S+) "
                    r"path=sampler action=(?P<action>\S+) .*?start=(?P<start>\d+) "
                    r"event_age_s=(?P<age>[\d.]+) .*?person_score=(?P<ps>[\d.]+) "
                    r".*?threshold=(?P<thr>[\d.]+)")


def parse_audit(lines):
    """Group sampler audit lines into ``{(camera, start): [frame, ...]}`` by frame age. Pure."""
    groups = collections.defaultdict(list)
    for line in lines:
        m = _AUDIT.search(line)
        if not m or m["action"] == "early_exit":
            continue
        groups[(m["cam"], m["start"])].append(
            {"ts": m["ts"], "age": float(m["age"]), "score": float(m["ps"]),
             "thr": float(m["thr"]), "action": m["action"]})
    for frames in groups.values():
        frames.sort(key=lambda f: f["age"])
    return groups


def replay_loiter(frames, k, tolerance, interval):
    """Feed one group through ``note_loiter``; return the frame index that fires, or None."""
    scfg = types.SimpleNamespace(loiter_frames=k, loiter_miss_tolerance=tolerance,
                                 interval=interval)
    group = {}
    fired = []
    for i, f in enumerate(frames):
        if sampler.note_loiter(group, f["score"] >= f["thr"], f["age"], scfg):
            fired.append(i)
    if len(fired) > 1:
        raise AssertionError("note_loiter fired twice for one group")
    return (fired[0], sampler.loiter_minutes(group, frames[fired[0]]["age"], scfg)) \
        if fired else None


def cmd_loiter(args):
    with open(args.audit) as f:
        groups = parse_audit(f)
    n_frames = sum(len(v) for v in groups.values())
    print(f"{len(groups)} sampler groups, {n_frames} scored frames")
    for k in (int(x) for x in args.frames.split(",")):
        for tol in (int(x) for x in args.tolerance.split(",")):
            hits = []
            for key, frames in sorted(groups.items()):
                r = replay_loiter(frames, k, tol, args.interval)
                if r:
                    hits.append((key, frames, r))
            print(f"\n== K={k} tolerance={tol}: {len(hits)} loitering notices "
                  f"(max 1 per group asserted)")
            for (cam, start), frames, (idx, minutes) in hits:
                seq = " ".join(f"{x['score']:.2f}{'*' if x['score'] >= x['thr'] else ''}"
                               for x in frames)
                print(f"  {cam} start={start} {frames[0]['ts']} fires at frame {idx + 1}, "
                      f"~{minutes} min; scores: {seq}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("score")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--url", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--root", default=".", help="paths in the cache are relative to this")
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--token-env", default="TAPO_SCORER_TOKEN", metavar="NAME",
                   help="NAME of the env var with the service's shared token "
                        "(NAME_FILE may name a file); default TAPO_SCORER_TOKEN")
    p.set_defaults(fn=cmd_score)
    p = sub.add_parser("feet")
    p.add_argument("boxes")
    p.add_argument("--min-score", type=float, default=0.30)
    p.add_argument("--cols", type=int, default=10)
    p.add_argument("--rows", type=int, default=8)
    p.set_defaults(fn=cmd_feet)
    p = sub.add_parser("zones")
    p.add_argument("boxes")
    p.add_argument("--zones", required=True)
    p.add_argument("--min-score", type=float, default=0.45)
    p.set_defaults(fn=cmd_zones)
    p = sub.add_parser("loiter")
    p.add_argument("audit")
    p.add_argument("--frames", default="4,6")
    p.add_argument("--tolerance", default="0")
    p.add_argument("--interval", type=int, default=30)
    p.set_defaults(fn=cmd_loiter)
    args = ap.parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
