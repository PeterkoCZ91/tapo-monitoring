"""tools/recording_audit_estimate.py: stratified miss-rate estimate on synthetic score tables."""

import importlib.util
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "recording_audit_estimate", ROOT / "tools" / "recording_audit_estimate.py")
rae = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rae)


def synth(seed=7, n_cand=600, n_lowrand=400, n_lowrest=19000, label_cand=250, label_low=300):
    """Rows with hidden truth; returns (rows, labels, truth)."""
    rng = random.Random(seed)
    rows, truth, labeled = [], {}, []
    idx = 0

    def add(stratum, person, y, r, window):
        nonlocal idx
        idx += 1
        row = {"frame": f"f{idx:06d}.jpg", "window": window, "ts": "2026-01-01T00:00:00Z",
               "stratum": stratum, "yolox": y, "rfdetr": r}
        rows.append(row)
        truth[row["frame"]] = person

    for i in range(n_cand):
        person = rng.random() < 0.5
        r = rng.uniform(0.3, 0.9) if person else rng.uniform(0.0, 0.5)
        y = rng.uniform(0.2, 0.8) if person else rng.uniform(0.0, 0.5)
        if max(y, r) < 0.25:
            r = 0.4
        add("cand", person, y, r, "night" if i % 2 else "day")
    for i in range(n_lowrand + n_lowrest):
        person = rng.random() < 0.01
        r = rng.uniform(0.1, 0.24) if person else rng.uniform(0.0, 0.2)
        y = rng.uniform(0.0, 0.24)
        add("lowrand" if i < n_lowrand else "lowrest", person, y, r, "night" if i % 2 else "day")
    cand = [r for r in rows if r["stratum"] == "cand"]
    low = [r for r in rows if r["stratum"] == "lowrand"]
    for r in rng.sample(cand, label_cand) + rng.sample(low, min(label_low, len(low))):
        labeled.append({"sha256": "h" + r["frame"], "path": "frames/" + r["frame"],
                        "label": "person" if truth[r["frame"]] else "no_person"})
    return rows, labeled, truth


def true_rate(rows, truth, fn):
    n = len(rows)
    persons = sum(truth[r["frame"]] for r in rows)
    caught = sum(truth[r["frame"]] for r in rows if fn(r))
    fp = sum(1 for r in rows if fn(r) and not truth[r["frame"]])
    return {"persons": persons / n * 1000, "caught": caught / n * 1000,
            "missed": (persons - caught) / n * 1000, "fp": fp / n * 1000}


def test_label_matching_by_path_file_and_basename():
    rows = [{"frame": "a/b/x.jpg", "stratum": "cand"}, {"frame": "y.jpg", "stratum": "cand"},
            {"frame": "z.jpg", "stratum": "cand"}]
    labels = [
        {"sha256": "1", "path": "a/b/x.jpg", "label": "person"},
        {"sha256": "2", "file": "other/dir/y.jpg", "label": "no_person"},
        {"sha256": "3", "path": "z.jpg", "label": "unsure"},
        {"sha256": "4", "path": "nomatch.jpg", "label": "person"},
    ]
    got, unmatched = rae.match_labels(rows, labels)
    assert got == {"a/b/x.jpg": True, "y.jpg": False}
    assert unmatched == 1


def test_last_label_per_sha_wins_and_unsure_ignored(tmp_path):
    p = tmp_path / "labels.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in [
        {"sha256": "1", "path": "a.jpg", "label": "person"},
        {"sha256": "1", "path": "a.jpg", "label": "no_person"},
        {"sha256": "2", "path": "b.jpg", "label": "person"},
        {"sha256": "2", "path": "b.jpg", "label": "unsure"},
    ]) + "\nbroken\n")
    latest = rae.load_labels(p)
    assert [x["label"] for x in latest] == ["no_person", "unsure"]
    got, _ = rae.match_labels([{"frame": "a.jpg"}, {"frame": "b.jpg"}], latest)
    assert got == {"a.jpg": False}


def test_exact_arithmetic_on_tiny_table():
    rows = [
        {"frame": "c1", "stratum": "cand", "yolox": 0.9, "rfdetr": 0.9, "window": "day"},
        {"frame": "c2", "stratum": "cand", "yolox": 0.5, "rfdetr": 0.3, "window": "day"},
        {"frame": "l1", "stratum": "lowrand", "yolox": 0.1, "rfdetr": 0.1, "window": "day"},
        {"frame": "l2", "stratum": "lowrand", "yolox": 0.1, "rfdetr": 0.1, "window": "day"},
        {"frame": "r1", "stratum": "lowrest", "yolox": 0.1, "rfdetr": 0.2, "window": "day"},
        {"frame": "r2", "stratum": "lowrest", "yolox": 0.0, "rfdetr": 0.0, "window": "day"},
    ]
    labels = {"c1": True, "c2": False, "l1": True, "l2": False}
    rep = rae.build_report(rows, labels, iterations=50, seed=1)
    g = rep["groups"]["all"]
    rule = g["rules"]["rfdetr>=0.45"]
    # persons: c1 + l1 + 0.5 * 2 unlabeled lowrest = 3 of 6 frames -> 500 per 1000
    assert abs(rule["persons"]["estimate"] - 500) < 1e-6
    assert abs(rule["caught"]["estimate"] - 1000 / 6) < 1e-6
    assert abs(rule["fp"]["estimate"]) < 1e-6
    # persons that no model scores >= 0.25: (l1 + 1 expected in lowrest) / 3
    assert abs(g["undetected_share"]["estimate"] - 2 / 3) < 1e-6
    either = g["rules"]["either>=0.12"]
    # labeled alerts: c1 (person), c2 (false alarm, weight 1); l1/l2 stay below 0.12
    assert abs(either["fp"]["estimate"] - 1 / 6 * 1000) < 1e-6


def test_truth_inside_bootstrap_interval():
    rows, labels, truth = synth()
    lab, _ = rae.match_labels(rows, labels)
    rep = rae.build_report(rows, lab, iterations=1000, seed=3)
    g = rep["groups"]["all"]
    rules = {
        "yolox>=0.45": lambda r: r["yolox"] >= 0.45,
        "rfdetr>=0.45": lambda r: r["rfdetr"] >= 0.45,
        "rfdetr>=0.30": lambda r: r["rfdetr"] >= 0.30,
        "either>=0.12": lambda r: r["rfdetr"] >= 0.12 or r["yolox"] >= 0.12,
    }
    hits = total = 0
    for name, fn in rules.items():
        t = true_rate(rows, truth, fn)
        for key in ("persons", "caught", "missed", "fp"):
            est = g["rules"][name][key]
            assert est["lo"] <= est["estimate"] <= est["hi"]
            total += 1
            hits += est["lo"] <= t[key] <= est["hi"]
    assert hits >= total - 1  # 95 % intervals: tolerate one chance miss over 16
    t = true_rate(rows, truth, lambda r: r["rfdetr"] >= 0.45)
    assert abs(g["rules"]["rfdetr>=0.45"]["missed"]["estimate"] - t["missed"]) < 0.6 * t["persons"]


def test_day_night_split_and_night_windows_option():
    rows, labels, _ = synth(n_lowrest=3000)
    lab, _ = rae.match_labels(rows, labels)
    rep = rae.build_report(rows, lab, iterations=20, seed=1)
    assert {"all", "day", "night"} <= set(rep["groups"])
    assert rep["groups"]["day"]["frames"] + rep["groups"]["night"]["frames"] == len(rows)
    for r in rows:
        r["window"] = "w1" if r["window"] == "day" else "w2"
    rep = rae.build_report(rows, lab, iterations=20, seed=1, night_windows={"w2"})
    assert rep["groups"]["night"]["frames"] > 0 and rep["groups"]["day"]["frames"] > 0
    assert rae.is_night("2026-01-01 noc 22-06", None) is True
    assert rae.is_night("den", None) is False


def test_seed_is_deterministic_and_warnings():
    rows, labels, _ = synth(label_cand=10, label_low=5, n_lowrest=500)
    lab, _ = rae.match_labels(rows, labels)
    a = rae.build_report(rows, lab, iterations=50, seed=9)
    b = rae.build_report(rows, lab, iterations=50, seed=9)
    assert a == b
    assert any("small" in w.lower() for w in a["warnings"])
    cs = rae.build_report(rows, lab, iterations=5, seed=9, lang="cs")
    assert any("Malý" in w for w in cs["warnings"])


def test_cli_json_and_text(tmp_path, capsys):
    rows, labels, _ = synth(n_lowrest=1000)
    s, lab = tmp_path / "scores.jsonl", tmp_path / "labels.jsonl"
    s.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    lab.write_text("\n".join(json.dumps(r) for r in labels) + "\n")
    assert rae.main([str(s), "--labels", str(lab), "--json", "--iterations", "30"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["frames"] == len(rows)
    assert rae.main([str(s), "--labels", str(lab), "--lang", "cs", "--iterations", "30"]) == 0
    text = capsys.readouterr().out
    assert "Upozornění" in text and "rfdetr>=0.45" in text
    assert rae.main([str(tmp_path / "missing"), "--labels", str(lab)]) == 2
