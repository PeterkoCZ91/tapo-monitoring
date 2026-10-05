"""tools/label_selection.py: categories, quotas, labeled skip, output format."""

import hashlib
import importlib.util
import json
from pathlib import Path

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
    e.add(0.3, 0.3, 0.3)                       # nothing
    out = e.run()
    rep = (out / "selection-report.md").read_text()
    for cat in ls.CATEGORIES:
        assert f"| {cat} | 1 | 1 |" in rep
    assert len(index(out)) == 5


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
    assert "Selected: 7" in rep
    assert "| control_person | 10 | 1 |" in rep
    per_source = [int(line.rsplit("|", 2)[1]) for line in rep.split("## By source")[1].splitlines()
                  if line.startswith("| ") and line[2:3].isalnum() and "source" not in line]
    assert per_source and max(per_source) <= 4


def test_labeled_skipped_and_missing_image(tmp_path):
    e = Env(tmp_path)
    done = e.add(0.9, 0.1, 0.8)
    e.add(0.9, 0.1, 0.8, image=False)
    e.add(0.9, 0.1, 0.8)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps({"sha256": done, "label": "person"}) + "\n")
    out = e.run("--labels", str(labels))
    assert len(index(out)) == 1


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
