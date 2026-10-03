#!/usr/bin/env python3
"""Compare person detectors on hand-labeled frames. Needs only numpy.

    python3 evaluate.py --bench-dir DIR --dataset DATASET_DIR [--baseline yolox_m]
        [--candidate MODEL] [--cascade MODEL --band 0.30,0.65] [--models a,b] [--max-disagree 60]

Reads DIR/manifest.json (manifest.py), DIR/scores/*.jsonl (run.py) and DATASET_DIR/labels.jsonl.
Prints Markdown and writes DIR/results.md:

  1. AUC and false alarms / missed persons at fixed thresholds (manual labels)
  2. the threshold at which a model misses at most 2 % of the manual persons, with
     bootstrap intervals, and the operating point matched to the baseline at 0.45
  3. the same on all labels (secondary, biased towards the teacher, see README.md)
  4. day and night (needs --lat/--lon in manifest.py), 5. disagreements, 6. a cascade

Latest label per sha256 wins; ``unsure`` is ignored; a label whose ``by`` starts with
``auto:`` is a teacher's label, everything else is manual.
"""
import argparse
import json
import os
import shutil

import numpy as np

THRESHOLDS = (0.30, 0.45, 0.55, 0.65)
NIGHT_ELEVATION = -3.0
MIN_CELL = 30
BOOTSTRAP = 1000
SEED = 12345
MAX_MISSED_SHARE = 0.02


def read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def load_labels(dataset):
    """``{sha: (label, kind)}``, latest row per sha256 wins, kind is manual or auto."""
    latest = {}
    for row in read_jsonl(os.path.join(dataset, "labels.jsonl")):
        if row.get("sha256"):
            latest[row["sha256"]] = row
    return {sha: (r.get("label"), "auto" if str(r.get("by", "")).startswith("auto:") else "manual")
            for sha, r in latest.items()}


def load_scores(bench, name):
    return {r["sha"]: float(r["person"]) for r in read_jsonl(os.path.join(bench, "scores", name + ".jsonl"))}


def auc(pos, neg):
    """ROC AUC = P(pos > neg) + 0.5 P(tie), from average ranks. NaN when a class is empty."""
    pos, neg = np.asarray(pos, dtype=float), np.asarray(neg, dtype=float)
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    allv = np.concatenate([pos, neg])
    order = allv.argsort(kind="mergesort")
    s = allv[order]
    ranks = np.empty(len(allv))
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def fp_miss(pos, neg, t):
    """(negatives scoring >= t, positives scoring < t)."""
    return int((np.asarray(neg) >= t).sum()), int((np.asarray(pos) < t).sum())


def match_threshold(neg, target_fp):
    """Lowest threshold (best recall) with at most ``target_fp`` negatives at or above it."""
    neg = np.sort(np.asarray(neg, dtype=float))[::-1]
    if target_fp >= len(neg):
        return 0.0
    v = neg[target_fp]
    return float(np.nextafter(v, 1.0)) if v < 1 else 1.0


def recall_threshold(pos, share=MAX_MISSED_SHARE):
    """Highest threshold that still misses at most ``share`` of the positives."""
    ps = np.sort(np.asarray(pos, dtype=float))
    k = int(np.floor(share * len(ps)))
    return float(ps[k]) if k < len(ps) else 0.0


def bootstrap(pos, neg, t, seed=SEED, n=BOOTSTRAP):
    """95 % intervals ``((fp_lo, fp_hi), (miss_pct_lo, miss_pct_hi))`` at a fixed threshold.

    Positives and negatives are resampled separately; the generator is seeded, so the same
    scores always give the same intervals.
    """
    rng = np.random.default_rng(seed)
    pos, neg = np.asarray(pos, dtype=float), np.asarray(neg, dtype=float)
    fps, miss = [], []
    for _ in range(n):
        p = pos[rng.integers(0, len(pos), len(pos))] if len(pos) else pos
        q = neg[rng.integers(0, len(neg), len(neg))] if len(neg) else neg
        fps.append((q >= t).sum())
        miss.append((p < t).mean() * 100 if len(p) else np.nan)
    return tuple(np.percentile(fps, [2.5, 97.5])), tuple(np.percentile(miss, [2.5, 97.5]))


def select_disagree(cand, base, labels, known, limit, hi=0.45, lo=0.25):
    """Frames where one model scores >= ``hi`` and the other < ``lo`` and no human has judged them.

    Returns ``(total, [(sha, candidate score, baseline score)])`` largest difference first,
    capped at ``limit``. ``known`` limits the choice to frames the manifest knows.
    """
    items = []
    for sha, c in cand.items():
        b = base.get(sha)
        if b is None or sha not in known:
            continue
        lab = labels.get(sha)
        if lab and lab[1] == "manual":
            continue
        if (c >= hi and b < lo) or (b >= hi and c < lo):
            items.append((abs(c - b), sha, c, b))
    items.sort(key=lambda x: (-x[0], x[1]))
    return len(items), [(sha, c, b) for _, sha, c, b in items[:limit]]


def cascade_scores(base, cand, lo, hi):
    """Baseline score outside ``[lo, hi]``, candidate score inside (when it has one)."""
    return {s: (cand[s] if lo <= base[s] <= hi and s in cand else base[s]) for s in base}


def split(frames, scores, subset=lambda s: True):
    pos = [scores[s] for s, lab in frames if lab == "person" and s in scores and subset(s)]
    neg = [scores[s] for s, lab in frames if lab == "no_person" and s in scores and subset(s)]
    return pos, neg


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def f3(x):
    return "n/a" if x != x else "%.3f" % x


def section_basic(title, frames, scores, subset=lambda s: True):
    rows = []
    for m, sc in scores.items():
        pos, neg = split(frames, sc, subset)
        row = [m, len(pos) + len(neg), len(pos), len(neg), f3(auc(pos, neg))]
        for t in THRESHOLDS:
            fp, ms = fp_miss(pos, neg, t)
            row.append("%d / %d" % (fp, ms))
        rows.append(row)
    hdr = ["model", "n", "person", "no_person", "AUC"] + ["@%.2f FP / miss" % t for t in THRESHOLDS]
    return "%s\n\n%s\n" % (title, table(hdr, rows))


def section_matched(title, frames, scores, baseline, subset=lambda s: True):
    base = scores[baseline]
    rows = []
    for m, sc in scores.items():
        common = {s for s in sc if s in base}
        sub = lambda s, c=common: s in c and subset(s)  # noqa: E731
        pos, neg = split(frames, sc, sub)
        bpos, bneg = split(frames, base, sub)
        if not pos or not neg:
            continue
        bfp, _ = fp_miss(bpos, bneg, 0.45)
        t = 0.45 if m == baseline else match_threshold(neg, bfp)
        fp, ms = fp_miss(pos, neg, t)
        fci, mci = bootstrap(pos, neg, t)
        rows.append([m, "%.3f" % t, fp, "[%d, %d]" % fci, ms, "%.1f%%" % (100 * ms / len(pos)),
                     "[%.1f, %.1f]%%" % mci, len(pos), len(neg)])
    hdr = ["model", "threshold", "FP", "FP 95% CI", "missed", "missed %", "missed 95% CI", "person", "no_person"]
    note = ("Candidate threshold = lowest threshold whose FP count is at most that of %s at 0.45 on the "
            "same frames. Bootstrap: %d resamples, seed %d, threshold held fixed. If the baseline "
            "already raises almost every false alarm in the sample, this point is degenerate; "
            "read the 2 %% table instead." % (baseline, BOOTSTRAP, SEED))
    return "%s\n\n%s\n\n%s\n" % (title, table(hdr, rows), note)


def section_recall(frames, scores):
    rows = []
    for m, sc in scores.items():
        pos, neg = split(frames, sc)
        if not pos or not neg:
            continue
        t = recall_threshold(pos)
        fp, ms = fp_miss(pos, neg, t)
        fci, _ = bootstrap(pos, neg, t)
        rows.append([m, "%.3f" % t, fp, "[%d, %d]" % fci, ms, "%.1f%%" % (100 * ms / len(pos))])
    return ("## Threshold that misses at most %d %% of the persons\n\n%s\n\n"
            "Per model: the highest threshold that misses at most %d %% of the persons in this "
            "set; FP counted on its no_person frames. Fewer false alarms at the same recall "
            "is better.\n" % (MAX_MISSED_SHARE * 100,
                             table(["model", "threshold", "FP", "FP 95% CI", "missed", "missed %"], rows),
                             MAX_MISSED_SHARE * 100))


def export_disagree(out_dir, cand, baseline, picked, meta, dataset):
    sub = os.path.join(out_dir, "disagree", cand)
    if os.path.isdir(sub):
        shutil.rmtree(sub)
    os.makedirs(sub)
    recs = []
    for sha, c, b in picked:
        m = meta[sha]
        src = os.path.join(dataset, m["path"])
        if not os.path.isfile(src):
            continue
        name = "%s_%s_c%.2f_b%.2f_%s" % (m.get("camera") or "cam", sha[:8], c, b, os.path.basename(m["path"]))
        shutil.copy2(src, os.path.join(sub, name))
        recs.append({"ts": m.get("ts"), "file": name, "camera": m.get("camera"), "verdict": "disagree",
                     "etype": "disagree", "person": round(c, 4), baseline: round(b, 4), "src": m["path"]})
    with open(os.path.join(sub, "index.jsonl"), "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    return recs, sub


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench-dir", default=".")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--baseline", default="yolox_m", help="scores/<name>.jsonl of the model in production")
    ap.add_argument("--candidate", help="model for the disagreement export (default: first non-baseline)")
    ap.add_argument("--models", help="comma list (default: every scores/*.jsonl)")
    ap.add_argument("--max-disagree", type=int, default=60)
    ap.add_argument("--disagree-dir", default="disagree-dataset", help="output directory for the export")
    ap.add_argument("--no-disagree", action="store_true")
    ap.add_argument("--cascade", help="candidate re-scoring the baseline's gray band")
    ap.add_argument("--band", default="0.30,0.65")
    a = ap.parse_args(argv)
    bench, dataset = os.path.expanduser(a.bench_dir), os.path.expanduser(a.dataset)
    base = a.baseline

    sdir = os.path.join(bench, "scores")
    names = a.models.split(",") if a.models else sorted(
        f[:-6] for f in os.listdir(sdir) if f.endswith(".jsonl"))
    if base not in names:
        names.append(base)
    names.sort(key=lambda n: (n != base, n))
    scores = {n: load_scores(bench, n) for n in names}
    labels = load_labels(dataset)
    meta = {}
    with open(os.path.join(bench, "manifest.json"), encoding="utf-8") as f:
        for r in json.load(f):
            meta.setdefault(r["sha"], r)
    elev = {s: r.get("elev") for s, r in meta.items()}
    have_elev = any(v is not None for v in elev.values())

    manual = [(s, lab[0]) for s, lab in labels.items() if lab[1] == "manual" and lab[0] in ("person", "no_person")]
    every = [(s, lab[0]) for s, lab in labels.items() if lab[0] in ("person", "no_person")]
    night = lambda s: elev.get(s) is not None and elev[s] < NIGHT_ELEVATION  # noqa: E731
    day = lambda s: elev.get(s) is not None and elev[s] >= NIGHT_ELEVATION  # noqa: E731

    out = ["# Person detector benchmark", "",
           "Baseline: %s. Models: %s. Manual labels: %d, all labels: %d (unsure ignored, latest label per "
           "sha256 wins).\n" % (base, ", ".join(names), len(manual), len(every))]
    out.append(section_basic("## 1. Manual labels: AUC and false alarms / missed persons by threshold",
                             manual, scores))
    out.append(section_recall(manual, scores))
    out.append(section_matched("## 2. Operating point matched to %s at 0.45 (manual labels)" % base,
                               manual, scores, base))
    out.append("## 3. All labels (secondary)\n\n**Automatic labels come from a teacher detector and favour "
               "models of its family; read these numbers as an upper bound, not a fair comparison.**\n")
    out.append(section_basic("### 3a. Thresholds", every, scores))
    out.append(section_matched("### 3b. Matched operating point", every, scores, base))
    if have_elev:
        out.append("## 4. Day / night (manual labels; night = solar elevation below %.0f degrees)\n" % NIGHT_ELEVATION)
        for label, fn in (("day", day), ("night", night)):
            pos, neg = split(manual, scores[base], fn)
            warn = ""
            if min(len(pos), len(neg)) < MIN_CELL:
                warn = " (a class has fewer than %d manual %s frames: indicative only)" % (MIN_CELL, label)
            out.append(section_basic("### %s%s" % (label, warn), manual, scores, fn))
    else:
        out.append("## 4. Day / night\n\nSkipped: the manifest has no solar elevation "
                   "(run manifest.py with --lat and --lon).\n")

    if a.cascade:
        lo, hi = (float(x) for x in a.band.split(","))
        b, c = scores[base], scores[a.cascade]
        sc = {base: b, a.cascade: c, a.cascade + "+cascade": cascade_scores(b, c, lo, hi)}
        share = 100.0 * sum(1 for s in b if lo <= b[s] <= hi) / max(1, len(b))
        out.append("## 6. Cascade: %s inside [%.2f, %.2f], %s elsewhere\n" % (a.cascade, lo, hi, base))
        out.append(section_basic("### Manual labels", manual, sc))
        out.append(section_recall(manual, sc))
        out.append("%.1f %% of all frames fall in the band and would be re-scored. Frames the candidate "
                   "has no score for keep the baseline score.\n" % share)

    if not a.no_disagree:
        cand = a.candidate or next((n for n in names if n != base), None)
        if cand in scores:
            total, picked = select_disagree(scores[cand], scores[base], labels, meta, a.max_disagree)
            recs, sub = export_disagree(os.path.expanduser(a.disagree_dir), cand, base, picked, meta, dataset)
            out.append("## 5. Disagreements: %s vs %s (not yet judged by a human)\n\n%d frames disagree (one "
                       "score at 0.45 or more, the other below 0.25); %d exported, largest difference first, to "
                       "`%s`. Label them with `tapo-monitor label %s`, append the new lines of its "
                       "`labels.jsonl` to the dataset's `labels.jsonl` and re-run: they join the manual set.\n" % (cand, base, total, len(recs), sub, a.disagree_dir))
        else:
            out.append("## 5. Disagreements\n\nNo candidate found (use --candidate MODEL).\n")

    text = "\n".join(out)
    print(text)
    with open(os.path.join(bench, "results.md"), "w", encoding="utf-8") as f:
        f.write(text)


if __name__ == "__main__":
    main()
