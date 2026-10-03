#!/usr/bin/env python3
"""Estimate detector miss and false-alarm rates from a stratified, partly labeled score table.

Input: ``scores.jsonl`` (one object per frame: ``frame``, ``window``, ``ts``, ``stratum``,
``yolox``, ``rfdetr``) and ``labels.jsonl`` from ``tapo-monitor label``. Strata: ``cand`` (any
model >= 0.25), ``lowrand`` (a random sample of frames where both are < 0.25) and ``lowrest``
(the other low frames, unlabeled). Prints a weighted estimate per decision rule, per 1000
frames, with seeded bootstrap 95 % intervals (docs/operations.md, "Recording audit
estimate"). Stdlib only; read-only.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from bisect import bisect_right
from pathlib import PurePosixPath

LOW = 0.25
STRATA = ("cand", "lowrand", "lowrest")
COMBO_T = (0.08, 0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.25)
SMALL = 30
METRICS = ("persons", "caught", "missed", "fp")
NIGHT_RE = re.compile(r"night|noc", re.I)
AUDIT_NAME_RE = re.compile(r"_([dn]\d\d)_(\d+)\.jpg$")
VISIT_GAP = 30.0
DAY_RE = re.compile(r"day|den", re.I)

TEXT = {
    "en": {
        "frames": "Frames", "strata": "Strata (frames / labeled / persons among labeled)",
        "group": "Window group", "rules": "Per 1000 frames (estimate [95 % interval])",
        "persons": "persons", "caught": "caught", "missed": "missed",
        "fp": "false alarms", "miss_rate": "miss rate",
        "undetected": "Persons no model scores >= {t} (independent completeness check)",
        "warnings": "Warnings",
        "all": "all", "day": "day", "night": "night", "unknown": "unknown window",
        "w_small": "Small labeled sample in stratum {s}: {n} frames; the intervals are wide.",
        "w_none": "Stratum {s} has no labeled frames: its person rate is taken as 0.",
        "w_zero": "No person among {n} labeled lowrand frames: the 95 % upper bound of their "
                  "rate is about {u:.1%} (rule of three), which the bootstrap does not show.",
        "w_bias": "The estimate assumes lowrand was drawn at random and that lowrest has the "
                  "same person rate as lowrand (labeled lowrest frames are not used); if "
                  "either is false, or labeled frames are not a random draw from their "
                  "stratum, the result is biased. "
                  "Labeling mistakes and unmatched labels bias it too, and the interval covers "
                  "sampling noise only.",
        "w_unknown": "{n} rows have an unknown stratum or no frame name and were ignored.",
        "w_unmatched": "{n} labels matched no frame (check path/file against frame).",
        "visits": "Visit level (labeled persons grouped into visits: same window, gap <= {g:g} s)",
        "visit_line": "{v} visits on {f} person frames; unweighted, labeled frames only",
        "visit_rule": "caught {c}, missed {m}, caught share {p} [{lo}-{hi}] (Wilson 95 %)",
        "w_visit_ts": "{n} labeled person frames have no numeric ts and are left out of the visits.",
    },
    "cs": {
        "frames": "Snímků", "strata": "Strata (snímků / označeno / osob mezi označenými)",
        "group": "Skupina oken", "rules": "Na 1000 snímků (odhad [95% interval])",
        "persons": "osoby", "caught": "zachyceno", "missed": "míjeno",
        "fp": "falešné poplachy", "miss_rate": "podíl míjených",
        "undetected": "Osoby, které žádný model neskóruje >= {t} (nezávislá kontrola úplnosti)",
        "warnings": "Upozornění",
        "all": "vše", "day": "den", "night": "noc", "unknown": "neznámé okno",
        "w_small": "Malý označený vzorek ve stratu {s}: {n} snímků; intervaly jsou široké.",
        "w_none": "Stratum {s} nemá žádný označený snímek: podíl osob se bere jako 0.",
        "w_zero": "Mezi {n} označenými lowrand snímky žádná osoba: horní mez 95 % jejich "
                  "podílu je asi {u:.1%} (pravidlo tří), což bootstrap neukáže.",
        "w_bias": "Odhad předpokládá, že lowrand byl vybrán náhodně a že lowrest má stejný "
                  "podíl osob jako lowrand (označené lowrest snímky se nepoužijí) a že označené "
                  "snímky jsou náhodný výběr svého stratu; jinak je zkreslený. Chyby v označení a "
                  "nespárované štítky ho zkreslují také a interval zahrnuje jen výběrový šum.",
        "w_unknown": "{n} řádků má neznámý stratum nebo chybí jméno snímku; vynechány.",
        "w_unmatched": "{n} štítků nesedí na žádný snímek (zkontrolujte path/file proti frame).",
        "visits": "Úroveň návštěv (označené osoby seskupené do návštěv: stejné okno, mezera <= {g:g} s)",
        "visit_line": "{v} návštěv na {f} snímcích s osobou; bez vážení, jen označené snímky",
        "visit_rule": "zachyceno {c}, míjeno {m}, podíl zachycených {p} [{lo}-{hi}] (Wilson 95 %)",
        "w_visit_ts": "{n} označených snímků s osobou nemá číselné ts a do návštěv se nepočítají.",
    },
}


def _read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def load_scores(path):
    return _read_jsonl(path)


def load_labels(path):
    """Latest record per sha256 (or path/file when there is no hash)."""
    latest = {}
    for rec in _read_jsonl(path):
        key = rec.get("sha256") or rec.get("path") or rec.get("file")
        if isinstance(key, str):
            latest[key] = rec
    return list(latest.values())


def _frame_key(window, frame):
    """``(window, index)`` of a frame named ``<window>/<index>.jpg``, else None."""
    if not isinstance(frame, str):
        return None
    parts = PurePosixPath(frame.replace("\\", "/"))
    if parts.suffix != ".jpg" or not parts.stem.isdigit():
        return None
    win = window if isinstance(window, str) else parts.parent.name
    return (win, int(parts.stem))


def match_labels(rows, labels):
    """Map frame -> True/False (person / no_person); ``unsure`` is ignored.

    A label names its image in ``path`` or ``file``. It matches a ``frame`` that is equal, else
    one equal after stripping directories (datasets get moved), if that basename is unique
    among the frames. Failing that, a ``frame`` field of the label is tried, and finally the
    audit-dataset name ``<stratum>_y.._r.._<window>_<index>.jpg`` (see ``_audit_key``) against
    frames named ``<window>/<index>.jpg``. Returns ``(mapping, number of unmatched labels)``.
    """
    frames = [r["frame"] for r in rows if isinstance(r.get("frame"), str)]
    exact = set(frames)
    by_base = {}
    for f in frames:
        by_base.setdefault(PurePosixPath(f.replace("\\", "/")).name, []).append(f)
    by_key = {}
    for r in rows:
        key = _frame_key(r.get("window"), r.get("frame"))
        if key is not None:
            by_key.setdefault(key, []).append(r["frame"])
    out, unmatched = {}, 0
    for rec in labels:
        label = rec.get("label")
        if label not in ("person", "no_person"):
            continue
        name = next((rec[k] for k in ("path", "file") if isinstance(rec.get(k), str)), None)
        frame = None
        if name in exact:
            frame = name
        elif name is not None:
            cands = by_base.get(PurePosixPath(name.replace("\\", "/")).name, [])
            if len(cands) == 1:
                frame = cands[0]
        if frame is None and rec.get("frame") in exact:
            frame = rec["frame"]
        if frame is None and name is not None:
            m = AUDIT_NAME_RE.search(name)
            cands = by_key.get((m.group(1), int(m.group(2)))) if m else None
            if cands and len(cands) == 1:
                frame = cands[0]
        if frame is None:
            unmatched += 1
            continue
        out[frame] = label == "person"
    return out, unmatched


def _s(row, key):
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def rule_table():
    rules = {
        "yolox>=0.45": lambda y, r: y >= 0.45,
        "rfdetr>=0.45": lambda y, r: r >= 0.45,
        "rfdetr>=0.30": lambda y, r: r >= 0.30,
    }
    for t in COMBO_T:
        rules[f"rfdetr>=0.45 OR (yolox>=0.45 AND rfdetr>={t:.2f})"] = (
            lambda y, r, t=t: r >= 0.45 or (y >= 0.45 and r >= t))
    rules["either>=0.12"] = lambda y, r: y >= 0.12 or r >= 0.12
    rules["both>=0.12"] = lambda y, r: y >= 0.12 and r >= 0.12
    return rules


def is_night(window, night_windows):
    if night_windows is not None and window in night_windows:
        return True
    return bool(NIGHT_RE.search(str(window)))


def _group_of(window, night_windows):
    if is_night(window, night_windows):
        return "night"
    if night_windows is not None or DAY_RE.search(str(window)):
        return "day"
    return "unknown"


def _aggregate(rows, labels, rules):
    """Stratum sizes plus the labeled frames of ``cand`` and ``lowrand`` as patterns.

    A pattern is ``(is_person, alert flags per rule)``; equal patterns are counted together
    so that the bootstrap can resample them cheaply. Labeled ``lowrest`` frames are not a
    random sample of their stratum and are left out of the estimate.
    """
    names = list(rules)
    sizes = dict.fromkeys(STRATA, 0)
    labeled = dict.fromkeys(STRATA, 0)
    patterns = {"cand": {}, "lowrand": {}}
    for row in rows:
        stratum = row["stratum"]
        sizes[stratum] += 1
        truth = labels.get(row["frame"])
        if truth is None:
            continue
        labeled[stratum] += 1
        if stratum in patterns:
            y, r = _s(row, "yolox"), _s(row, "rfdetr")
            key = (truth, tuple(rules[n](y, r) for n in names))
            patterns[stratum][key] = patterns[stratum].get(key, 0) + 1
    return {"names": names, "sizes": sizes, "labeled": labeled,
            "patterns": {s: list(p.items()) for s, p in patterns.items()}}


def _estimate(agg, counts, total):
    """Stratum-weighted estimate per 1000 frames.

    Each labeled frame stands for ``N / n`` frames of its stratum (``lowrand`` stands for
    ``lowrand`` + ``lowrest``). ``counts[s][i]`` is how often pattern ``i`` is in the sample.
    """
    names = agg["names"]
    sizes = agg["sizes"]
    pop = {"cand": sizes["cand"], "lowrand": sizes["lowrand"] + sizes["lowrest"]}
    persons = {"cand": 0.0, "lowrand": 0.0}
    caught = dict.fromkeys(names, 0.0)
    fp = dict.fromkeys(names, 0.0)
    for s in persons:
        n = sum(counts[s])
        if not n:
            continue
        w = pop[s] / n
        for ((person, alerts), _), c in zip(agg["patterns"][s], counts[s], strict=True):
            if not c:
                continue
            if person:
                persons[s] += w * c
            for name, hit in zip(names, alerts, strict=True):
                if hit:
                    if person:
                        caught[name] += w * c
                    else:
                        fp[name] += w * c
    all_persons = persons["cand"] + persons["lowrand"]
    scale = 1000.0 / total
    out = {"undetected_share": persons["lowrand"] / all_persons if all_persons else None,
           "rules": {}}
    for name in names:
        missed = all_persons - caught[name]
        out["rules"][name] = {
            "persons": all_persons * scale, "caught": caught[name] * scale,
            "missed": missed * scale, "fp": fp[name] * scale,
            "miss_rate": missed / all_persons if all_persons else None}
    return out


def _percentile(values, q):
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _interval(est, samples):
    vals = [v for v in samples if v is not None]
    if est is None or not vals:
        return {"estimate": est, "lo": None, "hi": None}
    return {"estimate": est, "lo": min(_percentile(vals, 0.025), est),
            "hi": max(_percentile(vals, 0.975), est)}


def _group_report(rows, labels, rules, iterations, seed):
    agg = _aggregate(rows, labels, rules)
    total = len(rows)
    point = _estimate(agg, {s: [c for _, c in agg["patterns"][s]] for s in agg["patterns"]},
                      total)
    rng = random.Random(seed)
    reps = []
    for _ in range(iterations):
        counts = {}
        for s, pats in agg["patterns"].items():
            draws = [0] * len(pats)
            n = sum(c for _, c in pats)
            if n:
                # with-replacement resample of the n labeled frames of this stratum
                cum, run = [], 0
                for _, c in pats:
                    run += c
                    cum.append(run)
                for _ in range(n):
                    draws[bisect_right(cum, rng.randrange(n))] += 1
            counts[s] = draws
        reps.append(_estimate(agg, counts, total))
    rep = {
        "frames": total,
        "strata": {s: {"frames": agg["sizes"][s], "labeled": agg["labeled"][s],
                       "persons": sum(c for (person, _), c in agg["patterns"].get(s, [])
                                      if person) if s in agg["patterns"] else None}
                   for s in STRATA},
        "undetected_share": _interval(point["undetected_share"],
                                      [r["undetected_share"] for r in reps]),
        "rules": {},
    }
    for name in agg["names"]:
        rep["rules"][name] = {
            key: _interval(point["rules"][name][key], [r["rules"][name][key] for r in reps])
            for key in (*METRICS, "miss_rate")}
    return rep


def wilson(k, n, z=1.96):
    """Wilson score interval for k successes in n trials; ``(None, None)`` when n is 0."""
    if n <= 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def _ts(row):
    v = row.get("ts")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def person_visits(rows, labels, gap=VISIT_GAP):
    """Group labeled person frames into visits: same window, consecutive ts differ <= gap s.

    Returns a list of visits, each a list of rows; frames without a numeric ts are skipped.
    """
    per_window = {}
    for r in rows:
        if labels.get(r.get("frame")) is True and _ts(r) is not None:
            per_window.setdefault(r.get("window"), []).append(r)
    visits = []
    for win in sorted(per_window, key=str):
        cur = None
        for r in sorted(per_window[win], key=_ts):
            if cur is not None and _ts(r) - _ts(cur[-1]) <= gap:
                cur.append(r)
            else:
                cur = [r]
                visits.append(cur)
    return visits


def visit_report(rows, labels, gap=VISIT_GAP):
    """Per rule: visits with at least one alerting frame (caught) and the rest (missed)."""
    visits = person_visits(rows, labels, gap)
    frames = sum(len(v) for v in visits)
    rep = {"gap": gap, "visits": len(visits), "person_frames": frames,
           "no_ts": sum(1 for r in rows if labels.get(r.get("frame")) is True and _ts(r) is None),
           "rules": {}}
    for name, fn in rule_table().items():
        caught = sum(1 for v in visits if any(fn(_s(r, "yolox"), _s(r, "rfdetr")) for r in v))
        lo, hi = wilson(caught, len(visits))
        rep["rules"][name] = {"caught": caught, "missed": len(visits) - caught,
                              "share": caught / len(visits) if visits else None,
                              "lo": lo, "hi": hi}
    return rep


def _warnings(rep, lang, unmatched):
    t = TEXT[lang]
    out = []
    if rep["ignored_rows"]:
        out.append(t["w_unknown"].format(n=rep["ignored_rows"]))
    if unmatched:
        out.append(t["w_unmatched"].format(n=unmatched))
    for group, g in rep["groups"].items():
        prefix = "" if group == "all" else f"[{t[group]}] "
        for s in ("cand", "lowrand"):
            n = g["strata"][s]["labeled"]
            if g["strata"][s]["frames"] and n == 0:
                out.append(prefix + t["w_none"].format(s=s))
            elif 0 < n < SMALL:
                out.append(prefix + t["w_small"].format(s=s, n=n))
        low = g["strata"]["lowrand"]
        if low["labeled"] and low["persons"] == 0:
            out.append(prefix + t["w_zero"].format(n=low["labeled"], u=3 / low["labeled"]))
    if rep["visit_level"]["no_ts"]:
        out.append(t["w_visit_ts"].format(n=rep["visit_level"]["no_ts"]))
    out.append(t["w_bias"])
    return out


def build_report(rows, labels, iterations=1000, seed=0, night_windows=None, lang="en",
                 unmatched=0, visit_gap=VISIT_GAP):
    rules = rule_table()
    usable = [r for r in rows if r.get("stratum") in STRATA and isinstance(r.get("frame"), str)]
    groups = {"all": usable}
    for r in usable:
        groups.setdefault(_group_of(r.get("window"), night_windows), []).append(r)
    rep = {"frames": len(usable), "ignored_rows": len(rows) - len(usable), "seed": seed,
           "iterations": iterations, "low_threshold": LOW, "unmatched_labels": unmatched,
           "groups": {}}
    for name in ("all", "day", "night", "unknown"):
        if groups.get(name):
            rep["groups"][name] = _group_report(groups[name], labels, rules, iterations, seed)
    rep["visit_level"] = visit_report(usable, labels, visit_gap)
    rep["warnings"] = _warnings(rep, lang, unmatched)
    return rep


def _cell(iv, nd=1, pct=False):
    if iv["estimate"] is None:
        return "-"

    def f(v):
        return f"{v:.1%}" if pct else f"{v:.{nd}f}"
    if iv["lo"] is None:
        return f(iv["estimate"])
    return f"{f(iv['estimate'])} [{f(iv['lo'])}-{f(iv['hi'])}]"


def render(rep, lang="en"):
    t = TEXT[lang]
    out = [f"{t['frames']}: {rep['frames']}   seed {rep['seed']}, "
           f"{rep['iterations']} bootstrap"]
    for name, g in rep["groups"].items():
        out += ["", f"== {t['group']}: {t[name]} ({t['frames']}: {g['frames']}) ==", t["strata"]]
        out += [f"  {s}: {v['frames']} / {v['labeled']} / {'-' if v['persons'] is None else v['persons']}"
                for s, v in g["strata"].items()]
        out += [t["undetected"].format(t=rep["low_threshold"]) + ": "
                + _cell(g["undetected_share"], pct=True), t["rules"]]
        for rule, m in g["rules"].items():
            out.append(f"  {rule}: " + ", ".join(f"{t[k]} {_cell(m[k])}" for k in METRICS)
                       + f", {t['miss_rate']} {_cell(m['miss_rate'], pct=True)}")
    v = rep["visit_level"]
    out += ["", f"== {t['visits'].format(g=v['gap'])} ==",
            t["visit_line"].format(v=v["visits"], f=v["person_frames"])]
    for rule, m in v["rules"].items():
        out.append(f"  {rule}: " + t["visit_rule"].format(
            c=m["caught"], m=m["missed"], p=_cell({"estimate": m["share"], "lo": None}, pct=True),
            lo=_cell({"estimate": m["lo"], "lo": None}, pct=True),
            hi=_cell({"estimate": m["hi"], "lo": None}, pct=True)))
    out += ["", f"{t['warnings']}:"] + [f"  - {w}" for w in rep["warnings"]]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scores", help="scores.jsonl (frame, window, ts, stratum, yolox, rfdetr)")
    ap.add_argument("--labels", required=True, help="labels.jsonl from `tapo-monitor label`")
    ap.add_argument("--night-windows", help="comma-separated window names counted as night "
                    "(all others then count as day); default: 'night'/'noc' in the window name")
    ap.add_argument("--visit-gap", type=float, default=VISIT_GAP,
                    help="max seconds between labeled person frames of one visit (default 30)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--iterations", type=int, default=1000)
    ap.add_argument("--lang", choices=sorted(TEXT), default="en")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    try:
        rows = load_scores(args.scores)
        labels, unmatched = match_labels(rows, load_labels(args.labels))
    except (OSError, ValueError) as exc:
        print(f"recording_audit_estimate: {exc}", file=sys.stderr)
        return 2
    night = ({w.strip() for w in args.night_windows.split(",") if w.strip()}
             if args.night_windows else None)
    rep = build_report(rows, labels, args.iterations, args.seed, night, args.lang, unmatched,
                       args.visit_gap)
    print(json.dumps(rep, indent=2) if args.json else render(rep, args.lang))
    return 0


if __name__ == "__main__":
    sys.exit(main())
