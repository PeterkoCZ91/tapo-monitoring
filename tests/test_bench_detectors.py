"""Tests for tools/bench_detectors on synthetic scores (no models, no network)."""

import importlib.util
import json
import pathlib

import pytest

np = pytest.importorskip("numpy")

TOOL = pathlib.Path(__file__).resolve().parent.parent / "tools" / "bench_detectors"


def _load(name):
    spec = importlib.util.spec_from_file_location("bench_" + name, TOOL / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ev = _load("evaluate")
mf = _load("manifest")
rn = _load("run")


def test_auc_perfect_inverted_and_ties():
    assert ev.auc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert ev.auc([0.1, 0.2], [0.9, 0.8]) == 0.0
    assert ev.auc([0.5, 0.5], [0.5, 0.5]) == 0.5
    assert ev.auc([0.9, 0.3], [0.5, 0.1]) == pytest.approx(0.75)
    assert ev.auc([], [0.1]) != ev.auc([], [0.1])  # NaN


def test_fp_miss_counts_at_threshold():
    assert ev.fp_miss([0.9, 0.4, 0.5], [0.5, 0.2], 0.5) == (1, 1)


def test_match_threshold_allows_exactly_target_fp():
    neg = [0.9, 0.8, 0.7, 0.1]
    for target in (0, 1, 2, 3):
        t = ev.match_threshold(neg, target)
        assert sum(n >= t for n in neg) <= target
    assert ev.match_threshold(neg, 2) <= 0.7 + 1e-6 and ev.match_threshold(neg, 2) > 0.7
    assert ev.match_threshold(neg, 4) == 0.0


def test_recall_threshold_misses_at_most_two_percent():
    pos = [i / 100 for i in range(1, 101)]
    t = ev.recall_threshold(pos)
    assert sum(p < t for p in pos) <= 2
    assert t == pytest.approx(0.03)
    assert ev.recall_threshold([0.7]) == 0.7


def test_bootstrap_is_seeded_and_brackets_the_point_estimate():
    pos = [0.9] * 40 + [0.2] * 10
    neg = [0.6] * 5 + [0.1] * 45
    a = ev.bootstrap(pos, neg, 0.5, seed=7, n=200)
    assert a == ev.bootstrap(pos, neg, 0.5, seed=7, n=200)
    assert a != ev.bootstrap(pos, neg, 0.5, seed=8, n=200)
    (fp_lo, fp_hi), (m_lo, m_hi) = a
    assert fp_lo <= 5 <= fp_hi and m_lo <= 20 <= m_hi


def test_select_disagree_filters_orders_and_caps():
    cand = {"a": 0.9, "b": 0.1, "c": 0.5, "d": 0.95, "e": 0.9, "x": 0.9}
    base = {"a": 0.1, "b": 0.8, "c": 0.4, "d": 0.2, "e": 0.1}
    labels = {"e": ("person", "manual"), "d": ("person", "auto")}
    known = {"a", "b", "c", "d", "e"}
    total, picked = ev.select_disagree(cand, base, labels, known, 10)
    # c is no disagreement, e is judged by a human, x has no baseline score; d is auto-labeled and stays
    assert total == 3
    assert [s for s, _, _ in picked] == ["a", "d", "b"]  # differences 0.8, 0.75, 0.7
    assert [s for s, _, _ in ev.select_disagree(cand, base, labels, known, 2)[1]] == ["a", "d"]


def test_cascade_rescoring_only_inside_the_band():
    base = {"lo": 0.1, "mid": 0.5, "hi": 0.9, "nocand": 0.5}
    cand = {"lo": 0.9, "mid": 0.2, "hi": 0.1}
    assert ev.cascade_scores(base, cand, 0.30, 0.65) == {"lo": 0.1, "mid": 0.2, "hi": 0.9, "nocand": 0.5}


def test_load_labels_latest_wins_and_marks_auto(tmp_path):
    rows = [{"sha256": "a", "label": "person", "by": "auto:yolox_x"},
            {"sha256": "a", "label": "no_person"},
            {"sha256": "b", "label": "person", "by": "auto:yolox_x"},
            {"sha256": "c", "label": "unsure"}]
    (tmp_path / "labels.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    assert ev.load_labels(str(tmp_path)) == {
        "a": ("no_person", "manual"), "b": ("person", "auto"), "c": ("unsure", "manual")}


def test_manifest_kinds_and_elevation(tmp_path):
    folder = tmp_path / "host" / "sent-log"
    folder.mkdir(parents=True)
    (folder / "20260101-120000-000000.jpg").write_bytes(b"one")
    (folder / "20260101-000000-000000.jpg").write_bytes(b"two")
    (folder / "other.jpg").write_bytes(b"three")
    import hashlib
    sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
    labels = [{"sha256": sha(b"one"), "label": "person", "camera": "front"},
              {"sha256": sha(b"two"), "label": "no_person", "by": "auto:yolox_x"}]
    (tmp_path / "labels.jsonl").write_text("\n".join(json.dumps(r) for r in labels))
    rows = {r["path"].split("/")[-1]: r for r in mf.build(str(tmp_path), 50.0, 14.0)}
    assert rows["20260101-120000-000000.jpg"]["kind"] == "manual"
    assert rows["20260101-000000-000000.jpg"]["kind"] == "auto"
    assert rows["other.jpg"]["kind"] == "none" and rows["other.jpg"]["elev"] is None
    assert rows["20260101-000000-000000.jpg"]["elev"] < -3 < rows["20260101-120000-000000.jpg"]["elev"]
    assert mf.build(str(tmp_path))[0]["elev"] is None


def test_solar_elevation_midday_above_midnight():
    noon = 1767268800.0  # 2026-01-01 12:00 UTC
    assert mf.solar_elevation(noon, 50.0, 0.0) > 10
    assert mf.solar_elevation(noon + 12 * 3600, 50.0, 0.0) < -3


def test_unique_frames_prefers_manual_and_dedupes():
    rows = [{"sha": "a", "kind": "auto"}, {"sha": "a", "kind": "manual"}, {"sha": "b", "kind": "none"}]
    got = rn.unique_frames(rows)
    assert [(r["sha"], r["kind"]) for r in got] == [("a", "manual"), ("b", "none")]
    assert rn.best([]) == 0.0 and rn.best([0.2, 0.7]) == 0.7
    assert rn.safe_name("hf:org/model") == "hf_org_model"


def test_end_to_end_tables_and_disagree_export(tmp_path):
    bench, dataset = tmp_path / "bench", tmp_path / "ds"
    (bench / "scores").mkdir(parents=True)
    dataset.mkdir()
    manifest, labels = [], []
    base, cand = [], []
    for i in range(10):
        sha = f"m{i}"
        (dataset / (sha + ".jpg")).write_bytes(b"x")
        manifest.append({"path": sha + ".jpg", "sha": sha, "kind": "manual", "camera": "c", "ts": 1.0, "elev": 20.0})
        labels.append({"sha256": sha, "label": "person" if i < 5 else "no_person"})
        base.append({"sha": sha, "person": 0.9 if i % 2 == 0 else 0.2})
        cand.append({"sha": sha, "person": 0.9 if i < 5 else 0.1})
    (dataset / "u.jpg").write_bytes(b"u")
    manifest.append({"path": "u.jpg", "sha": "u", "kind": "none", "camera": "c", "ts": 1.0, "elev": 20.0})
    base.append({"sha": "u", "person": 0.1})
    cand.append({"sha": "u", "person": 0.8})
    (bench / "manifest.json").write_text(json.dumps(manifest))
    (dataset / "labels.jsonl").write_text("\n".join(json.dumps(r) for r in labels))
    for name, rows in (("yolox_m", base), ("cand", cand)):
        (bench / "scores" / (name + ".jsonl")).write_text("\n".join(json.dumps(r) for r in rows))
    out = tmp_path / "out"
    ev.main(["--bench-dir", str(bench), "--dataset", str(dataset), "--disagree-dir", str(out),
             "--cascade", "cand"])
    text = (bench / "results.md").read_text()
    assert "| cand | 10 | 5 | 5 | 1.000 |" in text
    assert "## 6. Cascade" in text and "1 frames disagree" in text
    index = [json.loads(line) for line in (out / "disagree" / "cand" / "index.jsonl").read_text().splitlines()]
    assert [r["src"] for r in index] == ["u.jpg"] and index[0]["yolox_m"] == 0.1
