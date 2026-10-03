#!/usr/bin/env python3
"""Summarize a scorer shadow-run log (see docs/operations.md, "Shadow run of a second detector").

Reads the JSONL written by the scorer's shadow runner and prints counts, timing, the
agreement of the two models at their thresholds, where they disagree, score correlation
and, with hand labels, how a few candidate decision rules would have done on the labeled
disagreements. Stdlib only; read-only; source ids are shown as short hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PRIMARY_THRESHOLD = 0.45
BANDS = ((0.0, 0.25, "<0.25"), (0.25, 0.45, "0.25-0.45"), (0.45, 0.65, "0.45-0.65"),
         (0.65, 1.01, ">=0.65"))
FIXED_SHADOW = (0.45, 0.30, 0.194)
COMBO_T = (0.08, 0.10, 0.12, 0.14, 0.16, 0.18, 0.20)
NAME_SCORES = re.compile(r"p(\d+(?:\.\d+)?)_s(\d+(?:\.\d+)?)")

TEXT = {
    "en": {
        "records": "Records", "errors": "Errors", "compared": "Compared (both scored)",
        "timing": "Seconds per frame", "primary": "primary", "shadow": "shadow",
        "median": "median", "p95": "p95",
        "matrix": "Agreement at primary >= {p} and shadow >= {s}",
        "both": "both see a person", "primary_only": "only primary", "shadow_only": "only shadow",
        "neither": "neither", "share": "Disagreement share",
        "by_source": "Disagreements by source (hash)", "by_hour": "Disagreements by hour (UTC)",
        "by_band": "Disagreements by score band (higher of the two)",
        "corr": "Score correlation", "labels": "Hand-labeled disagreements",
        "labeled": "Labeled frames", "persons": "with a person",
        "fp": "false alarms", "miss": "misses",
        "warn": "Warning: the labeled sample is small and biased - only frames where the models "
                "differ strongly were saved - so these counts rank rules, they do not estimate "
                "real-world error rates.",
        "none": "none",
    },
    "cs": {
        "records": "Záznamů", "errors": "Chyb", "compared": "Porovnáno (skóre obou)",
        "timing": "Sekundy na snímek", "primary": "primární", "shadow": "shadow",
        "median": "medián", "p95": "p95",
        "matrix": "Shoda při primární >= {p} a shadow >= {s}",
        "both": "oba vidí osobu", "primary_only": "jen primární", "shadow_only": "jen shadow",
        "neither": "ani jeden", "share": "Podíl rozporů",
        "by_source": "Rozpory podle zdroje (hash)", "by_hour": "Rozpory podle hodiny (UTC)",
        "by_band": "Rozpory podle pásma skóre (vyšší z obou)",
        "corr": "Korelace skóre", "labels": "Ručně označené rozpory",
        "labeled": "Označených snímků", "persons": "s osobou",
        "fp": "falešné poplachy", "miss": "zmeškané",
        "warn": "Upozornění: označený vzorek je malý a zkreslený - ukládaly se jen snímky, kde se "
                "modely výrazně liší - čísla tedy řadí pravidla, neodhadují skutečnou chybovost.",
        "none": "žádné",
    },
}


def parse_since(text):
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_ts(text):
    try:
        return parse_since(text)
    except (ValueError, AttributeError):
        return None


def load_log(path, since=None):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if since is not None:
                ts = _parse_ts(rec.get("ts"))
                if ts is None or ts < since:
                    continue
            rows.append(rec)
    return rows


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sxx * syy)


def _ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs, ys):
    return pearson(_ranks(xs), _ranks(ys)) if len(xs) >= 2 else None


def _stats(values):
    return {"median": percentile(values, 0.5), "p95": percentile(values, 0.95)}


def _short(source):
    return hashlib.sha256(str(source).encode()).hexdigest()[:8]


def _band(score):
    for lo, hi, name in BANDS:
        if lo <= score < hi:
            return name
    return BANDS[-1][2]


def build_report(rows, shadow_threshold, primary_threshold=PRIMARY_THRESHOLD):
    errors = Counter(r["error"] for r in rows if r.get("error"))
    matrix = {"both": 0, "primary_only": 0, "shadow_only": 0, "neither": 0}
    by_source, by_hour, by_band = Counter(), Counter(), Counter()
    xs, ys = [], []
    for r in rows:
        p, s = _num(r.get("primary_person")), _num(r.get("shadow_person"))
        if p is None or s is None or r.get("error"):
            continue
        xs.append(p)
        ys.append(s)
        pp, sp = p >= primary_threshold, s >= shadow_threshold
        key = "both" if pp and sp else "primary_only" if pp else "shadow_only" if sp else "neither"
        matrix[key] += 1
        if pp != sp:
            by_source[_short(r.get("source_id"))] += 1
            ts = _parse_ts(r.get("ts"))
            if ts is not None:
                by_hour[f"{ts.astimezone(timezone.utc).hour:02d}"] += 1
            by_band[_band(max(p, s))] += 1
    compared = len(xs)
    disagree = matrix["primary_only"] + matrix["shadow_only"]
    return {
        "records": len(rows),
        "errors": sum(errors.values()),
        "errors_by_kind": dict(errors),
        "thresholds": {"primary": primary_threshold, "shadow": shadow_threshold},
        "compared": compared,
        "matrix": matrix,
        "disagreement_share": disagree / compared if compared else None,
        "primary_seconds": _stats([v for v in (_num(r.get("primary_seconds")) for r in rows)
                                   if v is not None]),
        "shadow_seconds": _stats([v for v in (_num(r.get("shadow_seconds")) for r in rows)
                                  if v is not None]),
        "disagree_by_source": dict(sorted(by_source.items(), key=lambda kv: (-kv[1], kv[0]))),
        "disagree_by_hour": dict(sorted(by_hour.items())),
        "disagree_by_band": dict(by_band),
        "pearson": pearson(xs, ys),
        "spearman": spearman(xs, ys),
    }


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scores_from_name(name):
    m = NAME_SCORES.search(Path(name).name)
    return (float(m.group(1)), float(m.group(2))) if m else None


def load_labeled(labels_path, disagree_dir=None):
    """Labeled disagreement frames as ``[(primary, shadow, is_person)]``.

    The last labels.jsonl line per sha256 wins; ``unsure`` frames are dropped. Scores come
    from the saved file name (``p<primary>_s<shadow>``), found by hashing the files in
    ``disagree_dir``, or else from the record's own path.
    """
    latest = {}
    with open(labels_path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("sha256"), str):
                latest[rec["sha256"]] = rec
    by_hash = {}
    if disagree_dir:
        for p in sorted(Path(disagree_dir).glob("*.jpg")):
            scores = _scores_from_name(p.name)
            if scores:
                by_hash[_sha256(p)] = scores
    items = []
    for sha, rec in latest.items():
        label = rec.get("label")
        if label not in ("person", "no_person"):
            continue
        scores = by_hash.get(sha) or _scores_from_name(str(rec.get("path", "")))
        if scores:
            items.append((scores[0], scores[1], label == "person"))
    return items


def _rules(pt):
    rules = [("primary", lambda p, s: p >= pt)]
    for t in FIXED_SHADOW:
        rules.append((f"shadow>={t:g}", lambda p, s, t=t: s >= t))
    for t in COMBO_T:
        rules.append((f"shadow>=0.45 OR (primary>={pt:g} AND shadow>={t:.2f})",
                      lambda p, s, t=t: s >= 0.45 or (p >= pt and s >= t)))
    return rules


def evaluate_rules(items, primary_threshold=PRIMARY_THRESHOLD):
    out = []
    for name, fn in _rules(primary_threshold):
        fp = sum(1 for p, s, person in items if fn(p, s) and not person)
        miss = sum(1 for p, s, person in items if not fn(p, s) and person)
        out.append({"rule": name, "n": len(items), "fp": fp, "miss": miss})
    return out


def _fmt(value, nd=3):
    return "-" if value is None else f"{value:.{nd}f}"


def render(rep, lang="en"):
    t = TEXT[lang]
    th = rep["thresholds"]
    out = [f"{t['records']}: {rep['records']}   {t['errors']}: {rep['errors']}"
           + (f" {rep['errors_by_kind']}" if rep["errors"] else ""),
           f"{t['compared']}: {rep['compared']}", "", t["timing"]]
    for key in ("primary", "shadow"):
        st = rep[f"{key}_seconds"]
        out.append(f"  {t[key]}: {t['median']} {_fmt(st['median'])}, {t['p95']} {_fmt(st['p95'])}")
    m = rep["matrix"]
    out += ["", t["matrix"].format(p=th["primary"], s=th["shadow"])]
    out += [f"  {t[k]}: {m[k]}" for k in ("both", "primary_only", "shadow_only", "neither")]
    share = rep["disagreement_share"]
    out.append(f"  {t['share']}: " + ("-" if share is None else f"{share:.1%}"))
    for title, key in (("by_source", "disagree_by_source"), ("by_hour", "disagree_by_hour"),
                       ("by_band", "disagree_by_band")):
        out += ["", t[title]]
        out += [f"  {k}: {v}" for k, v in rep[key].items()] or [f"  {t['none']}"]
    out += ["", f"{t['corr']}: Pearson {_fmt(rep['pearson'])}, Spearman {_fmt(rep['spearman'])}"]
    lab = rep.get("labels")
    if lab is not None:
        out += ["", f"{t['labels']}: {t['labeled']} {lab['n']}, {t['persons']} {lab['persons']}"]
        for r in lab["rules"]:
            out.append(f"  {r['rule']}: {r['fp']} {t['fp']}, {r['miss']} {t['miss']}")
        out += ["", t["warn"]]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("log", help="shadow JSONL log")
    ap.add_argument("--since", help="ISO time; earlier records are ignored (naive = UTC)")
    ap.add_argument("--lang", choices=sorted(TEXT), default="en")
    ap.add_argument("--shadow-threshold", type=float, default=0.45)
    ap.add_argument("--labels", help="labels.jsonl from `tapo-monitor label`")
    ap.add_argument("--disagree-dir", help="saved disagreement frames (p<primary>_s<shadow>.jpg)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    try:
        since = parse_since(args.since) if args.since else None
        rep = build_report(load_log(args.log, since), args.shadow_threshold)
        if args.labels:
            items = load_labeled(args.labels, args.disagree_dir)
            rep["labels"] = {"n": len(items), "persons": sum(1 for i in items if i[2]),
                             "rules": evaluate_rules(items)}
    except (OSError, ValueError) as exc:
        print(f"shadow_report: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2) if args.json else render(rep, args.lang))
    return 0


if __name__ == "__main__":
    sys.exit(main())
