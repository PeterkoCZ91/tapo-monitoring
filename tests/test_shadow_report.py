"""tools/shadow_report.py: shadow-run log statistics on a synthetic log and labels."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("shadow_report", ROOT / "tools" / "shadow_report.py")
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)


def row(ts, src, p, s, ps=0.1, ss=0.2, error=None):
    return {"ts": ts, "source_id": src, "primary_person": p, "primary_vehicle": 0.0,
            "shadow_person": s, "primary_seconds": ps, "shadow_seconds": ss, "error": error}


ROWS = [
    row("2026-01-01T10:00:00Z", "cam-a", 0.9, 0.8),     # both
    row("2026-01-01T10:05:00Z", "cam-a", 0.6, 0.1),     # primary only
    row("2026-01-01T11:00:00Z", "cam-b", 0.1, 0.7),     # shadow only
    row("2026-01-01T11:30:00Z", "cam-b", 0.1, 0.2),     # neither
    row("2026-01-01T12:00:00Z", "cam-a", 0.5, None, error="timeout"),
    row("2026-01-02T12:00:00Z", "cam-b", 0.2, 0.3, ps=0.4, ss=0.8),
]


def write_log(tmp_path, rows=ROWS, extra=""):
    path = tmp_path / "shadow.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n" + extra)
    return path


def test_load_skips_bad_lines_and_filters_since(tmp_path):
    path = write_log(tmp_path, extra="not json\n[1]\n")
    assert len(sr.load_log(path)) == 6
    since = sr.parse_since("2026-01-01T11:00:00Z")
    assert len(sr.load_log(path, since)) == 4
    assert sr.parse_since("2026-01-01") is not None


def test_report_counts_matrix_and_timing(tmp_path):
    rep = sr.build_report(sr.load_log(write_log(tmp_path)), 0.45)
    assert rep["records"] == 6 and rep["errors"] == 1
    assert rep["errors_by_kind"] == {"timeout": 1}
    assert rep["matrix"] == {"both": 1, "primary_only": 1, "shadow_only": 1, "neither": 2}
    assert rep["compared"] == 5
    assert rep["disagreement_share"] == pytest.approx(2 / 5)
    assert rep["primary_seconds"]["median"] == pytest.approx(0.1)
    assert rep["shadow_seconds"]["p95"] == pytest.approx(0.65)


def test_breakdowns_use_hashed_sources_hours_and_bands(tmp_path):
    rep = sr.build_report(sr.load_log(write_log(tmp_path)), 0.45)
    keys = set(rep["disagree_by_source"])
    assert keys and all(len(k) == 8 and "cam" not in k for k in keys)
    assert sum(rep["disagree_by_source"].values()) == 2
    assert rep["disagree_by_hour"] == {"10": 1, "11": 1}
    assert sum(rep["disagree_by_band"].values()) == 2


def test_correlations(tmp_path):
    rep = sr.build_report(sr.load_log(write_log(tmp_path)), 0.45)
    assert -1 <= rep["pearson"] <= 1 and -1 <= rep["spearman"] <= 1
    perfect = [row("2026-01-01T00:00:00Z", "a", x / 10, x / 10) for x in range(1, 6)]
    r = sr.build_report(perfect, 0.45)
    assert r["pearson"] == pytest.approx(1.0) and r["spearman"] == pytest.approx(1.0)
    flat = [row("2026-01-01T00:00:00Z", "a", 0.5, x / 10) for x in range(3)]
    assert sr.build_report(flat, 0.45)["pearson"] is None


def test_spearman_handles_ties():
    assert sr.spearman([1, 1, 2], [1, 1, 2]) == pytest.approx(1.0)


def test_empty_log():
    rep = sr.build_report([], 0.45)
    assert rep["records"] == 0 and rep["disagreement_share"] is None
    assert sr.render(rep, "en")


def make_labels(tmp_path):
    d = tmp_path / "disagree"
    d.mkdir()
    spec_ = [("a_p0.80_s0.10", b"1", "person"),      # primary only, person
             ("b_p0.70_s0.05", b"2", "no_person"),   # primary only, not person
             ("c_p0.10_s0.90", b"3", "person"),      # shadow only, person
             ("d_p0.20_s0.60", b"4", "no_person"),   # shadow only, not person
             ("e_p0.60_s0.10", b"5", "unsure")]
    lines = []
    for name, data, label in spec_:
        (d / f"{name}.jpg").write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        lines.append({"path": f"x/{name}.jpg", "sha256": sha, "label": "no_person"})
        lines.append({"path": f"x/{name}.jpg", "sha256": sha, "label": label})
    lab = tmp_path / "labels.jsonl"
    lab.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return lab, d


def test_label_rules(tmp_path):
    lab, d = make_labels(tmp_path)
    items = sr.load_labeled(lab, d)
    assert len(items) == 4      # unsure dropped, last line wins
    byname = {e["rule"]: e for e in sr.evaluate_rules(items, 0.45)}
    assert byname["primary"]["fp"] == 1 and byname["primary"]["miss"] == 1
    assert byname["shadow>=0.45"]["fp"] == 1 and byname["shadow>=0.45"]["miss"] == 1
    assert byname["shadow>=0.3"]["n"] == 4
    assert "shadow>=0.194" in byname
    # the combo adds primary-only frames whose shadow score reaches T: only 0.10 and 0.05 here
    combo = byname["shadow>=0.45 OR (primary>=0.45 AND shadow>=0.08)"]
    assert combo["fp"] == 1 and combo["miss"] == 0
    combo_hi = byname["shadow>=0.45 OR (primary>=0.45 AND shadow>=0.20)"]
    assert combo_hi["fp"] == 1 and combo_hi["miss"] == 1


def test_labels_without_dir_use_path_scores(tmp_path):
    lab, _ = make_labels(tmp_path)
    assert len(sr.load_labeled(lab, None)) == 4


def test_cli_text_and_json(tmp_path, capsys):
    log = write_log(tmp_path)
    lab, d = make_labels(tmp_path)
    assert sr.main([str(log), "--labels", str(lab), "--disagree-dir", str(d), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["records"] == 6 and out["labels"]["rules"]
    assert sr.main([str(log), "--lang", "cs", "--labels", str(lab)]) == 0
    text = capsys.readouterr().out
    assert "Záznamů" in text and "malý" in text
    assert sr.main([str(log)]) == 0
    assert "Records" in capsys.readouterr().out
    assert sr.main([str(tmp_path / "missing.jsonl")]) == 2
