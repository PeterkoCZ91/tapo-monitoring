"""tools/label_selection.py --image-dir: images for frames missing from disagree/."""

import json

from tests.test_label_selection import Env, index


def test_image_dir_supplies_controls_and_report(tmp_path):
    e = Env(tmp_path)
    e.add(0.9, 0.8, 0.7, image=False)                  # control_person from sent-log
    e.add(0.1, 0.05, 0.2, image=False)                 # control_empty from review-log
    e.add(0.9, 0.1, 0.8)                               # disagree
    e.rows1.append({"source_id": "cam-a", "primary_person": 0.9})   # no frame
    for kind, n in (("sent-log", 1), ("review-log", 2)):
        d = tmp_path / "ds" / kind
        d.mkdir(parents=True)
        (d / f"x{n}.jpg").write_bytes(f"body-{n}".encode())
        (d / "index.jsonl").write_text(json.dumps({"file": f"x{n}.jpg", "ts": 1000.0 + n}) + "\n")
    # the same image again in the same directory is counted once
    (tmp_path / "ds" / "sent-log" / "zcopy.jpg").write_bytes(b"body-1")
    out = e.run("--image-dir", str(tmp_path / "ds" / "sent-log"),
                "--image-dir", str(tmp_path / "ds" / "review-log"))
    rep = (out / "selection-report.md").read_text()
    assert "| control_person | 1 | 1 |" in rep and "| control_empty | 1 | 1 |" in rep
    assert "| sent-log | 1 |" in rep and "| review-log | 1 |" in rep and "| disagree | 1 |" in rep
    assert "without `frame` (dropped, cannot be paired): 1" in rep
    stamps = sorted(r["ts"] for r in index(out))
    assert stamps[:2] == [1001.0, 1002.0] and len(stamps) == 3
