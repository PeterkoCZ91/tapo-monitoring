#!/usr/bin/env python3
"""Estimate detection completeness from the continuous local recordings (not from kept frames).

The alert pipeline only ever keeps the frames it already found interesting, so its archive
cannot say how many people it missed. The local recorder (``RECORDING_ROOT``) holds the
unfiltered stream: this tool samples it without bias, scores every sampled frame with one or
two scorer services and builds a labeling dataset in which a human settles the open cases.

Three subcommands, run in this order:

  plan     pick the windows (seeded, stratified day/night, spread over distinct days) from a
           file listing and print how many bytes must be copied:
             ssh HOST "cd RECORDING_ROOT/CAMERA && find . -type f -printf '%P\\t%s\\n'" > listing.txt
             recording_audit.py plan --listing listing.txt --out plan.json --files-from files.txt
             rsync -a --files-from=files.txt HOST:RECORDING_ROOT/CAMERA/ raw/
  score    cut a frame every N seconds out of each window (ffmpeg), score it (at most 2
           frames/s) and write scores.jsonl, one row per frame:
             {"frame","window","ts","stratum","yolox","rfdetr"}
           stratum: ``cand`` any model >= 0.25; ``lowrand`` both < 0.25, a seeded random
           sample of 150; ``lowrest`` the other low frames; ``error`` no model answered.
  dataset  copy the 400 highest candidates and the lowrand sample into DATASET/audit/ with
           an index.jsonl that ``tapo-monitor label DATASET`` opens (verdict = stratum).

Labeled candidates give the precision of each model; the labeled lowrand sample gives, with
its sampling weight (``lowrest`` size / 150), the persons both models missed.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

THRESHOLD = 0.25
SEGMENT_NAME = re.compile(r"(\d{8})T(\d{6})")
NAMES = ("yolox", "rfdetr")


def segment_start(name):
    """Local start time of a recorder segment, from ``zaznam_YYYYMMDDTHHMMSS.mkv``."""
    m = SEGMENT_NAME.search(str(name))
    if not m:
        raise ValueError(f"no timestamp in segment name: {name}")
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")


def parse_listing(lines):
    """``[(relative path, bytes)]`` from ``path<TAB>size`` lines; other lines are skipped."""
    out = []
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        try:
            segment_start(parts[0])
        except ValueError:
            continue
        out.append((parts[0], int(parts[1])))
    return out


def _in_hours(dt, lo, hi):
    return lo <= dt.hour < hi if lo < hi else (dt.hour >= lo or dt.hour < hi)


def plan_windows(listing, seed, per_stratum=6, window=600, segment_seconds=900,
                 day=(9, 16), night=(21, 5), days=7):
    """Seeded, stratified windows, each inside ONE segment so one file per window is enough.

    Only complete days of the retention window are used: the newest day is still being
    written and the two oldest are about to be pruned. Windows of a stratum come from
    distinct days while days last.
    """
    if not listing:
        raise ValueError("empty listing")
    dates = sorted({segment_start(p).date() for p, _ in listing})
    usable = set(dates[2:-1][-days:]) if len(dates) > 3 else set(dates)
    rng = random.Random(seed)
    plan = []
    for stratum, (lo, hi) in (("day", day), ("night", night)):
        by_day = defaultdict(list)
        for path, size in listing:
            dt = segment_start(path)
            if dt.date() in usable and _in_hours(dt, lo, hi) and size > 0:
                by_day[dt.date()].append((path, size))
        if not by_day:
            raise ValueError(f"no {stratum} segments in the listing")
        order = []
        while len(order) < per_stratum:
            batch = sorted(by_day)
            rng.shuffle(batch)
            order.extend(batch)
        for i, d in enumerate(order[:per_stratum], 1):
            path, size = rng.choice(sorted(by_day[d]))
            start = rng.randint(0, max(0, segment_seconds - window))
            plan.append({"window": f"{stratum[0]}{i:02d}", "stratum": stratum, "file": path,
                         "start": start, "duration": window, "bytes": size})
    return plan


def plan_bytes(plan):
    """Bytes to copy: every distinct segment once."""
    return sum({w["file"]: w["bytes"] for w in plan}.values())


def assign_strata(rows, seed, lowrand_n=150, threshold=THRESHOLD):
    """Set ``stratum`` on every row. Deterministic for a seed (rows are ordered by frame)."""
    rows = sorted((dict(r) for r in rows), key=lambda r: r["frame"])
    low = []
    for r in rows:
        scores = [r.get(n) for n in NAMES if r.get(n) is not None]
        if not scores:
            r["stratum"] = "error"
        elif max(scores) >= threshold:
            r["stratum"] = "cand"
        else:
            r["stratum"] = "lowrest"
            low.append(r)
    for r in random.Random(seed).sample(low, min(lowrand_n, len(low))):
        r["stratum"] = "lowrand"
    return rows


class Limiter:
    """Blocks so that ``wait()`` returns at most ``rate`` times per second."""

    def __init__(self, rate, clock=time.monotonic, sleep=time.sleep):
        self.gap = 1.0 / rate
        self.clock, self.sleep = clock, sleep
        self.next = None

    def wait(self):
        now = self.clock()
        if self.next is not None and now < self.next:
            self.sleep(self.next - now)
            now = self.next
        self.next = now + self.gap


def extract_frames(video, start, duration, every, out_dir, width=0):
    """JPEG every ``every`` seconds of ``video[start:start+duration]``: ``[(path, offset)]``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    vf = f"fps=1/{every}" + (f",scale={width}:-2" if width else "")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(duration),
                    "-i", str(video), "-an", "-vf", vf, "-q:v", "2", "-start_number", "0",
                    str(out_dir / "%05d.jpg")], check=True)
    return [(p, i * every) for i, p in enumerate(sorted(out_dir.glob("*.jpg")))]


def send_token(url):
    """The token goes to remote scorers only, never to a loopback one."""
    return urlparse(url).hostname not in ("127.0.0.1", "localhost", "::1")


def score_jpeg(url, path, token=None, force_token=False, timeout=60):
    headers = {"Content-Type": "image/jpeg"}
    if token and (force_token or send_token(url)):
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=Path(path).read_bytes(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return float(json.load(resp)["person"])


def parse_scorers(specs):
    """``[(name, url)]``; plain URLs are named yolox, rfdetr in order."""
    if len(specs) > len(NAMES):
        raise ValueError("at most two scorers")
    out = []
    for i, spec in enumerate(specs):
        name, sep, url = spec.partition("=")
        out.append((name, url) if sep and "://" not in name else (NAMES[i], spec))
    return out


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def score_frames(meta, frames_dir, out_path, scorers, rate=2.0):
    """Append one row per unscored frame; a failing scorer leaves its column ``None``."""
    out_path = Path(out_path)
    done = {r["frame"] for r in read_jsonl(out_path)}
    limiter = Limiter(rate) if rate else None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as fh:
        for m in meta:
            if m["frame"] in done:
                continue
            if limiter:
                limiter.wait()
            row = {"frame": m["frame"], "window": m["window"], "ts": m["ts"], "stratum": None}
            for name in NAMES:
                value = None
                if name in scorers:
                    try:
                        value = scorers[name](Path(frames_dir) / m["frame"])
                    except Exception as exc:  # noqa: BLE001 - one bad frame must not end the run
                        print(f"{name} failed on {m['frame']}: {exc}", file=sys.stderr)
                row[name] = value
            fh.write(json.dumps(row) + "\n")
            fh.flush()


def _best(row):
    return max((row[n] for n in NAMES if row.get(n) is not None), default=0.0)


def build_dataset(rows, frames_dir, out_dir, camera="audit", max_cand=400, max_lowrand=150):
    """Copy the highest candidates and the lowrand sample under ``out_dir/audit``."""
    sub = Path(out_dir) / "audit"
    sub.mkdir(parents=True, exist_ok=True)
    cand = sorted((r for r in rows if r["stratum"] == "cand"), key=_best, reverse=True)[:max_cand]
    low = sorted((r for r in rows if r["stratum"] == "lowrand"), key=lambda r: r["frame"])[:max_lowrand]
    with open(sub / "index.jsonl", "w", encoding="utf-8") as idx:
        for r in cand + low:
            src = Path(frames_dir) / r["frame"]
            if not src.is_file():
                continue
            y, rf = r.get("yolox"), r.get("rfdetr")
            name = (f"{r['stratum']}_y{(y or 0):.2f}_r{(rf or 0):.2f}_"
                    f"{r['window']}_{src.stem}.jpg")
            shutil.copyfile(src, sub / name)
            rec = {"file": name, "camera": camera, "ts": r["ts"],
                   "person": rf if rf is not None else y, "yolox_m": y, "rfdetr": rf,
                   "window": r["window"], "verdict": r["stratum"]}
            idx.write(json.dumps(rec) + "\n")
    return {"cand": len(cand), "lowrand": len(low)}


def summary(rows):
    per = Counter((r["window"], r["stratum"]) for r in rows)
    tot = Counter(r["stratum"] for r in rows)
    lines = [f"frames: {len(rows)}  {dict(sorted(tot.items()))}"]
    for w in sorted({r["window"] for r in rows}):
        lines.append(f"  {w} {({s: n for (ww, s), n in sorted(per.items()) if ww == w})}")
    both = [r for r in rows if r.get("yolox") is not None and r.get("rfdetr") is not None]
    if both:
        only_y = sum(r["yolox"] >= THRESHOLD > r["rfdetr"] for r in both)
        only_r = sum(r["rfdetr"] >= THRESHOLD > r["yolox"] for r in both)
        both_hi = sum(min(r["yolox"], r["rfdetr"]) >= THRESHOLD for r in both)
        lines.append(f"both scored: {len(both)}, >= {THRESHOLD}: only yolox {only_y}, "
                     f"only rfdetr {only_r}, both {both_hi}")
    return "\n".join(lines)


def _cmd_plan(a):
    listing = parse_listing(Path(a.listing).read_text(encoding="utf-8").splitlines())
    plan = plan_windows(listing, a.seed, a.per_stratum, a.window, a.segment_seconds,
                        _hours(a.day), _hours(a.night), a.days)
    total = plan_bytes(plan)
    Path(a.out).write_text(json.dumps({"seed": a.seed, "bytes": total, "windows": plan}, indent=1))
    if a.files_from:
        Path(a.files_from).write_text("".join(f + "\n" for f in sorted({w["file"] for w in plan})))
    print(f"{len(plan)} windows, {len({w['file'] for w in plan})} segments, {total} bytes "
          f"({total / 1e9:.2f} GB)")
    return 0


def _hours(text):
    lo, hi = text.split("-")
    return int(lo), int(hi)


def _cmd_score(a):
    plan = json.loads(Path(a.plan).read_text())["windows"]
    token = Path(a.token_file).read_text().strip() if a.token_file else None
    scorers = {}
    for name, url in parse_scorers(a.scorer):
        scorers[name] = (lambda u: lambda p: score_jpeg(u, p, token))(url)
    frames_dir = Path(a.frames_dir)
    meta = []
    for w in plan:
        video = Path(a.raw) / w["file"]
        wdir = frames_dir / w["window"]
        if not any(wdir.glob("*.jpg")):
            extract_frames(video, w["start"], w["duration"], a.every, wdir, a.width)
        base = segment_start(w["file"]).timestamp() + w["start"]
        for i, p in enumerate(sorted(wdir.glob("*.jpg"))):
            meta.append({"frame": f"{w['window']}/{p.name}", "window": w["window"],
                         "ts": base + i * a.every})
    out = Path(a.out)
    score_frames(meta, frames_dir, out, scorers, rate=a.max_rate)
    rows = assign_strata(read_jsonl(out), a.seed, a.lowrand)
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(summary(rows))
    return 0


def _cmd_dataset(a):
    rows = read_jsonl(a.scores)
    n = build_dataset(rows, a.frames_dir, a.out, a.camera, a.max_cand, a.lowrand)
    print(f"dataset {a.out}/audit: {n}; label with: tapo-monitor label --lang cs {a.out}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--listing", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--files-from")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--per-stratum", type=int, default=6)
    p.add_argument("--window", type=int, default=600, help="window length, seconds")
    p.add_argument("--segment-seconds", type=int, default=900)
    p.add_argument("--day", default="9-16", help="local hours counted as day, FROM-TO")
    p.add_argument("--night", default="21-5", help="local hours counted as night, FROM-TO")
    p.add_argument("--days", type=int, default=7, help="most recent complete days to draw from")
    p.set_defaults(fn=_cmd_plan)
    p = sub.add_parser("score")
    p.add_argument("--plan", required=True)
    p.add_argument("--raw", required=True, help="directory the segments were copied into")
    p.add_argument("--frames-dir", required=True)
    p.add_argument("--out", required=True, help="scores.jsonl")
    p.add_argument("--scorer", action="append", required=True, metavar="[NAME=]URL",
                   help="scorer /score URL; repeat for a second model (named yolox, rfdetr in order)")
    p.add_argument("--token-file", help="bearer token, sent to non-loopback scorers only")
    p.add_argument("--every", type=int, default=5, help="seconds between frames")
    p.add_argument("--width", type=int, default=0, help="scale frames to this width (0 = native)")
    p.add_argument("--max-rate", type=float, default=2.0, help="frames per second sent to a scorer")
    p.add_argument("--lowrand", type=int, default=150)
    p.add_argument("--seed", type=int, default=1)
    p.set_defaults(fn=_cmd_score)
    p = sub.add_parser("dataset")
    p.add_argument("--scores", required=True)
    p.add_argument("--frames-dir", required=True)
    p.add_argument("--out", required=True, help="dataset directory for tapo-monitor label")
    p.add_argument("--camera", default="audit")
    p.add_argument("--max-cand", type=int, default=400)
    p.add_argument("--lowrand", type=int, default=150)
    p.set_defaults(fn=_cmd_dataset)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, subprocess.CalledProcessError, OSError) as exc:
        print(f"recording_audit: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
