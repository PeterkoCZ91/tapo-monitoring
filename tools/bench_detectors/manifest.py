#!/usr/bin/env python3
"""Build manifest.json for the benchmark from a labeled dataset directory.

The dataset is the one ``tapo-monitor label`` / ``collect_frames.sh`` work on: JPEGs below the
directory, optional ``index.jsonl`` files (with ``ts`` per frame) and a ``labels.jsonl``.

    python3 manifest.py DATASET_DIR --out BENCH_DIR/manifest.json [--lat 50.0 --lon 14.4]

One row per JPEG: path (relative to the dataset), sha, kind (manual | auto | none), label,
camera, ts and elev (solar elevation in degrees, only with --lat/--lon; without them
evaluate.py skips the day/night split). A frame is "auto" when its latest label has a
``by`` starting with ``auto:`` (a teacher's label), "manual" otherwise.
"""
import argparse
import collections
import datetime
import glob
import hashlib
import json
import math
import os
import re


def solar_elevation(ts, lat, lon):
    """Rough solar elevation in degrees at unix time ``ts`` (no refraction, no equation of time)."""
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    day = dt.timetuple().tm_yday
    decl = math.radians(23.44) * math.sin(math.radians(360 / 365 * (day - 81)))
    hours = dt.hour + dt.minute / 60 + lon / 15
    ha = math.radians(15 * (hours - 12))
    la = math.radians(lat)
    x = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, x))))


def load_labels(dataset):
    """Latest row per sha256 wins."""
    latest = {}
    path = os.path.join(dataset, "labels.jsonl")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("sha256"):
                    latest[row["sha256"]] = row
    return latest


def load_times(dataset):
    times = {}
    for idx in glob.glob(os.path.join(dataset, "**", "index.jsonl"), recursive=True):
        base = os.path.relpath(os.path.dirname(idx), dataset)
        with open(idx, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and "file" in row and isinstance(row.get("ts"), (int, float)):
                    times[os.path.normpath(os.path.join(base, row["file"]))] = row["ts"]
    return times


def filename_time(name):
    m = re.search(r"(\d{8})-(\d{6})", os.path.basename(name))
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return None


def build(dataset, lat=None, lon=None):
    labels = load_labels(dataset)
    times = load_times(dataset)
    rows = []
    for path in sorted(glob.glob(os.path.join(dataset, "**", "*.jpg"), recursive=True)):
        rel = os.path.relpath(path, dataset)
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        lab = labels.get(sha)
        ts = times.get(os.path.normpath(rel))
        if ts is None:
            ts = filename_time(rel)
        kind = "none"
        if lab:
            kind = "auto" if str(lab.get("by", "")).startswith("auto:") else "manual"
        have_site = ts is not None and lat is not None and lon is not None
        rows.append({"path": rel, "sha": sha, "kind": kind, "label": lab.get("label") if lab else None,
                     "camera": (lab or {}).get("camera"), "ts": ts,
                     "elev": solar_elevation(ts, lat, lon) if have_site else None})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset")
    ap.add_argument("--out", default="manifest.json")
    ap.add_argument("--lat", type=float, help="latitude, enables the day/night split")
    ap.add_argument("--lon", type=float, help="longitude, enables the day/night split")
    a = ap.parse_args(argv)
    if (a.lat is None) != (a.lon is None):
        ap.error("--lat and --lon go together")
    rows = build(os.path.expanduser(a.dataset), a.lat, a.lon)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    print(len(rows), "frames,", len({r["sha"] for r in rows}), "unique")
    print(dict(collections.Counter((r["kind"], r["label"]) for r in rows)))
    print("without timestamp:", sum(r["ts"] is None for r in rows))


if __name__ == "__main__":
    main()
