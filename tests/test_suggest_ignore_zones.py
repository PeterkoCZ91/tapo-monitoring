import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import suggest_ignore_zones as siz  # noqa: E402

ROOF = [0.0, 0.27, 0.44, 0.68, 0.6]


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


def _rows(n, box_for, cam="cam1"):
    rows = []
    for i in range(n):
        rows.append({"ts": f"2026-10-{10 + i % 3:02d}T12:{i % 60:02d}:00Z", "source_id": cam,
                     "frame": hashlib.sha256(f"{cam}{i}".encode()).hexdigest()[:16],
                     "primary_boxes": [box_for(i)], "error": None})
    return rows


def test_static_box_is_suggested(tmp_path, capsys):
    p = _write(tmp_path / "a.jsonl", _rows(30, lambda i: ROOF))
    assert siz.main([p]) == 0
    out = capsys.readouterr().out
    assert "ignore_boxes_mode: observe" in out
    assert "30 frames, 3 days" in out
    assert "[0.000, 0.260, 0.450, 0.690]" in out


def test_moving_person_is_not_suggested():
    rows = _rows(40, lambda i: [0.02 * i, 0.3, 0.02 * i + 0.1, 0.7, 0.8])
    obs = [("cam1", r["frame"], r["ts"][:10], r["primary_boxes"][0]) for r in rows]
    assert siz.suggest(obs) == {}


def test_labelled_person_cluster_excluded(tmp_path):
    rows = _rows(30, lambda i: ROOF)
    ds = tmp_path / "ds"
    ds.mkdir()
    sha = hashlib.sha256(b"cam10").hexdigest()
    assert rows[0]["frame"] == sha[:16]
    (ds / "labels.jsonl").write_text(json.dumps(
        {"sha256": sha, "label": "person", "path": "x.jpg"}) + "\n")
    p = _write(tmp_path / "a.jsonl", rows)
    assert siz.main([p, "--labels", str(ds)]) == 0
    assert siz.main([p]) == 0
    obs = siz.load_observations([p], ("primary_boxes",))
    persons = siz.person_frame_prefixes(str(ds))
    assert siz.suggest(obs, person_frames=persons) == {}
    assert "cam1" in siz.suggest(obs)


def test_thresholds_and_single_day(tmp_path):
    rows = _rows(30, lambda i: ROOF)
    for r in rows:
        r["ts"] = "2026-10-10T12:00:00Z"
    obs = siz.load_observations([_write(tmp_path / "a.jsonl", rows)], ("primary_boxes",))
    assert siz.suggest(obs) == {}
    assert "cam1" in siz.suggest(obs, min_days=1)


def test_empty_and_garbage_input(tmp_path, capsys):
    p = tmp_path / "e.jsonl"
    p.write_text("\nnot json\n[1]\n")
    assert siz.main([str(p)]) == 0
    assert "Nothing to suggest" in capsys.readouterr().out
    assert siz.main([str(tmp_path / "missing.jsonl")]) == 2
