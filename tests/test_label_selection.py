"""tools/label_selection.py: categories, quotas, labeled skip, output format."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "label_selection", ROOT / "tools" / "label_selection.py")
ls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ls)

BIG = [0.4, 0.2, 0.5, 0.6, 0.9]
TINY = [0.4, 0.2, 0.45, 0.25, 0.6]


class Env:
    def __init__(self, tmp):
        self.tmp = tmp
        (tmp / "d1").mkdir()
        (tmp / "d2").mkdir()
        self.rows1, self.rows2, self.n = [], [], 0

    def add(self, p, s1, s2, source="cam-a", boxes=None, image=True):
        self.n += 1
        body = f"body-{self.n}".encode()
        sha = hashlib.sha256(body).hexdigest()
        b = boxes or []
        r1 = {"frame": sha[:16], "source_id": source, "primary_person": p,
              "shadow_person": s1, "primary_boxes": b, "shadow_boxes": b}
        self.rows1.append(r1)
        if s2 is not None:
            self.rows2.append({**r1, "shadow_person": s2, "shadow_name": "x"})
        if image:
            name = f"20260101T0000{self.n:02d}123_p{p:.2f}_s{s1:.2f}.jpg"
            (self.tmp / "d1" / name).write_bytes(body)
        return sha

    def run(self, *extra, dry=False):
        t = self.tmp
        for fn, rows in (("s1.jsonl", self.rows1), ("s2.jsonl", self.rows2)):
            (t / fn).write_text("".join(json.dumps(r) + "\n" for r in rows))
        argv = ["--shadow-log", str(t / "s1.jsonl"), "--shadow2-log", str(t / "s2.jsonl"),
                "--disagree-dir", str(t / "d1"), "--disagree-dir", str(t / "d2"),
                "--out", str(t / "out"), *extra]
        if dry:
            argv.append("--dry-run")
        assert ls.main(argv) == 0
        return t / "out"


def index(out):
    return [json.loads(x) for x in (out / "index.jsonl").read_text().splitlines()]


def test_categories(tmp_path):
    e = Env(tmp_path)
    e.add(0.9, 0.1, 0.8)                       # strong
    e.add(0.6, 0.3, 0.7)                       # threshold
    e.add(0.1, 0.2, 0.3, boxes=[TINY])         # small (no model reaches 0.45)
    e.add(0.9, 0.8, 0.7, boxes=[BIG])          # control_person
    e.add(0.1, 0.05, 0.2)                      # control_empty
    e.add(0.05, 0.3, 0.1)                      # missed (rfdetr 0.3 >= 0.25, primary < 0.10)
    e.add(0.3, 0.3, 0.3)                       # nothing
    out = e.run()
    rep = (out / "selection-report.md").read_text()
    for cat in ls.CATEGORIES:
        assert f"| {cat} | 1 | 1 |" in rep
    assert len(index(out)) == 6


def test_quota_caps_source_and_controls(tmp_path):
    e = Env(tmp_path)
    for _ in range(10):
        e.add(0.9, 0.1, 0.8, source="big")
    for _ in range(2):
        e.add(0.9, 0.1, 0.8, source="small")
    for _ in range(10):
        e.add(0.9, 0.8, 0.8, source="small")
    out = e.run("--limit", "8", "--source-share", "0.5", "--control", "1")
    rep = (out / "selection-report.md").read_text()
    assert "Selected: 6" in rep          # 1 control reserved, cap 3 of the other 7
    assert "| control_person | 10 | 1 |" in rep
    per_source = [int(line.rsplit("|", 2)[1]) for line in rep.split("## By source")[1].splitlines()
                  if line.startswith("| ") and line[2:3].isalnum() and "source" not in line]
    assert per_source and max(per_source) <= 4


def test_controls_reserved_when_one_source_fills_its_cap(tmp_path):
    e = Env(tmp_path)
    for _ in range(20):
        e.add(0.9, 0.1, 0.8, source="busy")
    for _ in range(5):
        e.add(0.9, 0.8, 0.8, source="busy")
        e.add(0.1, 0.05, 0.2, source="busy")
    out = e.run("--limit", "10", "--source-share", "0.5", "--control", "2")
    rep = (out / "selection-report.md").read_text()
    assert "| control_person | 5 | 2 |" in rep and "| control_empty | 5 | 2 |" in rep
    assert "| strong | 20 | 3 |" in rep       # cap = 0.5 x (10 - 4 reserved)


def test_labeled_skipped_and_missing_image(tmp_path):
    e = Env(tmp_path)
    done = e.add(0.9, 0.1, 0.8)
    e.add(0.9, 0.1, 0.8, image=False)
    e.add(0.9, 0.1, 0.8)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps({"sha256": done, "label": "person"}) + "\n")
    out = e.run("--labels", str(labels))
    assert len(index(out)) == 1


def test_teacher_labels_and_undo_do_not_skip(tmp_path):
    e = Env(tmp_path)
    auto = e.add(0.9, 0.1, 0.8)
    undone = e.add(0.9, 0.1, 0.8)
    e.add(0.9, 0.1, 0.8)
    labels = tmp_path / "labels.jsonl"
    labels.write_text("\n".join(json.dumps(r) for r in (
        {"sha256": auto, "label": "person", "by": "auto:yolox_x"},
        {"sha256": undone, "label": "person"},
        {"sha256": undone, "label": "unlabeled"},
    )) + "\n")
    out = e.run("--labels", str(labels))
    assert len(index(out)) == 3


def test_output_format_and_anonymity(tmp_path):
    e = Env(tmp_path)
    sha = e.add(0.9, 0.1, 0.8, source="rtsp://cam-secret", boxes=[BIG])
    out = e.run("--batch", "3")
    (rec,) = index(out)
    assert rec["file"].startswith("disagree3/") and (out / rec["file"]).is_file()
    assert rec["ts"] == 1767225601.123 and rec["person"] == 0.9
    assert {b[5] for b in rec["boxes"]} == {"yolox640", "rfdetr", "yolox960"}
    assert all(len(b) == 6 for b in rec["boxes"])
    assert rec["models"] == ["yolox640", "rfdetr", "yolox960"]
    (t,) = [json.loads(x) for x in (out / "teacher.jsonl").read_text().splitlines()]
    assert t == {"sha256": sha, "person": 0.1}
    everything = "".join(p.read_text() for p in out.glob("*") if p.is_file())
    assert "cam-secret" not in everything and str(tmp_path) not in everything


def test_dry_run_writes_nothing(tmp_path, capsys):
    e = Env(tmp_path)
    e.add(0.9, 0.1, 0.8)
    out = e.run(dry=True)
    assert not out.exists()
    assert "| strong | 1 | 1 |" in capsys.readouterr().out


def test_frame_in_both_dirs_counted_once(tmp_path):
    e = Env(tmp_path)
    e.add(0.9, 0.1, 0.8)
    src = next((tmp_path / "d1").iterdir())
    (tmp_path / "d2" / "20260101T000009999_p0.90_s0.80.jpg").write_bytes(src.read_bytes())
    assert len(index(e.run())) == 1


def test_models_lists_only_models_that_ran(tmp_path):
    e = Env(tmp_path)
    e.add(0.9, 0.1, None)          # no second-shadow row
    (rec,) = index(e.run())
    assert rec["models"] == ["yolox640", "rfdetr"] and "boxes" not in rec


def test_camera_from_dataset_index_reaches_frames_of_the_same_source(tmp_path):
    e = Env(tmp_path)
    e.add(0.9, 0.1, 0.8, source="cam-a")                 # disagree/ image, no camera
    e.add(0.9, 0.1, 0.8, source="cam-b")                 # no dataset frame for cam-b
    ds = tmp_path / "sent-log"
    ds.mkdir()
    body = b"dataset-frame"
    sha = hashlib.sha256(body).hexdigest()
    (ds / "f.jpg").write_bytes(body)
    (ds / "index.jsonl").write_text(json.dumps({"file": "f.jpg", "ts": 1.0,
                                                "camera": "garden"}) + "\n")
    e.rows1.append({"frame": sha[:16], "source_id": "cam-a", "primary_person": 0.9,
                    "shadow_person": 0.1, "primary_boxes": [], "shadow_boxes": []})
    e.rows2.append({**e.rows1[-1], "shadow_person": 0.8, "shadow_name": "x"})
    out = e.run("--image-dir", str(ds))
    cams = sorted(str(r.get("camera")) for r in index(out))
    assert cams == ["None", "garden", "garden"]


def test_small_and_threshold_picked_before_strong(tmp_path):
    e = Env(tmp_path)
    for _ in range(20):
        e.add(0.9, 0.1, 0.8)                             # strong
    for _ in range(3):
        e.add(0.5, 0.4, 0.4)                             # threshold
    for _ in range(4):
        e.add(0.3, 0.3, 0.3, boxes=[TINY])               # small
    out = e.run("--limit", "8", "--source-share", "1", "--control", "0", "--small", "2")
    rep = (out / "selection-report.md").read_text()
    assert "| threshold | 3 | 3 |" in rep and "| small | 4 | 2 |" in rep
    assert "| strong | 20 | 3 |" in rep


def test_third_and_fourth_logs_join_and_tiled_score_takes_the_best_tile(tmp_path):
    e = Env(tmp_path)
    sha = e.add(0.1, 0.1, 0.1)                 # all three agree on empty...
    r1 = e.rows1[-1]
    rows3 = [{**r1, "shadow_person": 0.2, "shadow_name": "m896"}]
    rows4 = [{**r1, "shadow_person": 0.1, "shadow_tile_person": 0.8, "shadow_name": "t2"}]
    (tmp_path / "s3.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows3))
    (tmp_path / "s4.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows4))
    out = e.run("--shadow3-log", str(tmp_path / "s3.jsonl"),
                "--shadow4-log", str(tmp_path / "s4.jsonl"))
    rows = index(out)
    assert len(rows) == 1                      # ...but the tiled fourth makes it strong
    assert rows[0]["models"] == list(ls.DEFAULT_MODELS)
    rep = (out / "selection-report.md").read_text()
    assert "| strong | 1 | 1 |" in rep
    assert sha


def test_fourth_log_needs_the_third(tmp_path):
    e = Env(tmp_path)
    e.add(0.9, 0.1, 0.8)
    for fn in ("s1.jsonl", "s2.jsonl"):
        (tmp_path / fn).write_text("")
    argv = ["--shadow-log", str(tmp_path / "s1.jsonl"), "--shadow2-log", str(tmp_path / "s2.jsonl"),
            "--shadow4-log", str(tmp_path / "s1.jsonl"), "--out", str(tmp_path / "o")]
    assert ls.main(argv) == 2


def test_missed_category_region_and_missed_subdir(tmp_path):
    e = Env(tmp_path)
    inside = [0.65, 0.1, 0.75, 0.3, 0.5]
    outside = [0.1, 0.5, 0.2, 0.9, 0.5]
    e.add(0.05, 0.4, 0.4, boxes=[inside])          # missed, in region
    e.add(0.05, 0.4, 0.4, boxes=[outside])         # missed, outside region
    e.add(0.20, 0.4, 0.4, boxes=[inside])          # primary too high
    e.add(0.05, 0.4, 0.4, boxes=[inside], image=False)  # image only in missed/
    (tmp_path / "d1" / "missed").mkdir()
    (tmp_path / "d1" / "missed" / "20260101T000059123_p0.05_s0.40.jpg").write_bytes(
        b"body-4")
    out = e.run("--control", "0", "--missed-region", "0.6,0.05,0.8,0.35")
    rep = (out / "selection-report.md").read_text()
    assert "| missed | 2 | 2 |" in rep
    assert len(index(out)) == 2


def test_missed_quota_before_strong_and_region_off_by_default(tmp_path):
    e = Env(tmp_path)
    for _ in range(5):
        e.add(0.05, 0.6, 0.6, boxes=[BIG])         # missed (not strong: max 0.6 >= high, min < low)
    for _ in range(10):
        e.add(0.9, 0.1, 0.8)                       # strong
    out = e.run("--limit", "8", "--source-share", "1", "--control", "0", "--missed", "3")
    rep = (out / "selection-report.md").read_text()
    assert "| missed | 5 | 3 |" in rep and "| strong | 10 | 5 |" in rep


@pytest.mark.parametrize("region", ["nan,0,1,1", "0,0,inf,1", "-0.1,0,1,1",
                                   "0,0,1.1,1", "0.8,0,0.2,1", "0,0.8,1,0.2",
                                   "0.2,0,0.2,1"])
def test_invalid_missed_region_rejected_before_output(tmp_path, region):
    e = Env(tmp_path)
    e.add(0.05, 0.4, 0.4)
    with pytest.raises(SystemExit) as exc:
        e.run("--missed-region", region)
    assert exc.value.code == 2
    assert not (tmp_path / "out").exists()


def test_nonfinite_primary_cannot_create_missed_candidate(tmp_path):
    e = Env(tmp_path)
    e.add(float("nan"), 0.3, 0.3, image=False)
    (tmp_path / "d1" / "20260101T000001123_p0.05_s0.30.jpg").write_bytes(b"body-1")
    out = e.run("--control", "0")
    assert index(out) == []
    assert "| missed | 0 | 0 |" in (out / "selection-report.md").read_text()


@pytest.mark.parametrize("option,value", [("--missed", "-1"),
                                         ("--missed-primary", "nan"),
                                         ("--missed-primary", "1.1"),
                                         ("--missed-shadow", "inf"),
                                         ("--missed-shadow", "-0.1")])
def test_invalid_missed_threshold_or_quota_rejected(tmp_path, option, value):
    e = Env(tmp_path)
    e.add(0.05, 0.4, 0.4)
    with pytest.raises(SystemExit) as exc:
        e.run(option, value)
    assert exc.value.code == 2
    assert not (tmp_path / "out").exists()


def test_missed_boundaries_follow_second_model_with_custom_names(tmp_path):
    e = Env(tmp_path)
    e.add(0.10, 0.30, 0.30)       # primary boundary is excluded
    e.add(0.099, 0.25, 0.10)      # shadow boundary is included
    e.add(0.05, 0.249, 0.30)      # third model cannot qualify instead of the second
    out = e.run("--models", "production,teacher,other", "--control", "0")
    (rec,) = index(out)
    assert rec["person"] == 0.099
    assert rec["models"] == ["production", "teacher", "other"]
    assert "| missed | 1 | 1 |" in (out / "selection-report.md").read_text()


def test_missed_region_uses_highest_scoring_shadow_box(tmp_path):
    e = Env(tmp_path)
    inside = [0.65, 0.1, 0.75, 0.3, 0.5]
    outside = [0.1, 0.5, 0.2, 0.9, 0.8]
    e.add(0.05, 0.4, 0.4, boxes=[inside, outside])
    e.add(0.05, 0.4, 0.4, boxes=[[*inside[:4], 0.9], outside])
    out = e.run("--control", "0", "--missed-region", "0.6,0.05,0.8,0.35")
    (rec,) = index(out)
    assert rec["file"].endswith("000002123_p0.05_s0.40.jpg")
    assert "| missed | 1 | 1 |" in (out / "selection-report.md").read_text()
