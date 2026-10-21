"""tools/build_verifier_dataset.py: crop geometry, camera-day split, skips, determinism."""

import hashlib
import importlib.util
import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "build_verifier_dataset", ROOT / "tools" / "build_verifier_dataset.py")
bvd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bvd)

DAY = 86400.0
T0 = 1790000000.0


def make(tmp, rows):
    """rows: (name, camera, day_offset, label, boxes); returns dataset dir."""
    ds = tmp / "ds"
    ds.mkdir()
    index, labels = [], []
    for n, (name, cam, day, label, boxes) in enumerate(rows):
        path = ds / (name + ".jpg")
        Image.new("RGB", (200, 100), (n * 7 % 250, 40, 90)).save(path, "JPEG")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        index.append({"file": name + ".jpg", "camera": cam, "ts": T0 + day * DAY,
                      "person": 0.5, "boxes": boxes})
        if label:
            labels.append({"sha256": sha, "label": label, "path": name + ".jpg"})
    (ds / "index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in index))
    (ds / "labels.jsonl").write_text("".join(json.dumps(r) + "\n" for r in labels))
    return ds


BOX = [[0.25, 0.2, 0.5, 0.8, 0.9, "m"]]


def rows(n_days=6):
    out = []
    for d in range(n_days):
        for k in range(2):
            out.append((f"a{d}_{k}", "cam", d, "person" if k else "no_person", BOX))
    return out


def test_pad_box_geometry():
    assert bvd.pad_box([0.25, 0.2, 0.5, 0.8], (200, 100), 0.0, 1) == (50, 20, 100, 80)
    left, t, r, b = bvd.pad_box([0.25, 0.2, 0.5, 0.8], (200, 100), 0.1, 1)
    assert (left, r) == (45, 105) and (t, b) == (14, 86)
    # min size grows a tiny box, clamped inside the image at the corner
    assert bvd.pad_box([0.0, 0.0, 0.01, 0.01], (200, 100), 0.0, 40) == (0, 0, 40, 40)
    # min size larger than the image is capped at the image
    assert bvd.pad_box([0.4, 0.4, 0.5, 0.5], (20, 20), 0.0, 64) == (0, 0, 20, 20)


def test_crops_manifest_and_resize(tmp_path):
    ds = make(tmp_path, rows(2))
    out = tmp_path / "out"
    manifest, counts, _, _ = bvd.build(ds, out, pad=0.0, resize=64, seed=1)
    assert counts == {"person": 2, "no_person": 2}
    for r in manifest:
        assert (out / r["file"]).is_file() and r["file"].startswith(r["label"] + "/")
        with Image.open(out / r["file"]) as im:
            assert im.size == (64, 64)
        assert r["crop_px"] == [50, 20, 100, 80] and r["camera"] == "cam"
        assert len(r["source_sha256"]) == 64 and r["split"] in ("train", "val")
    lines = (out / "manifest.jsonl").read_text().splitlines()
    assert len(lines) == 4 and json.loads(lines[0])["score"] == 0.9


def test_unsure_and_unlabeled_skipped(tmp_path):
    ds = make(tmp_path, [("u", "cam", 0, "unsure", BOX), ("n", "cam", 0, None, BOX),
                         ("p", "cam", 0, "person", BOX)])
    manifest, counts, skipped, _ = bvd.build(ds, tmp_path / "out")
    assert [r["label"] for r in manifest] == ["person"]
    assert skipped["unsure"] == 1 and skipped["unlabeled"] == 1
    assert counts["no_person"] == 0


def test_split_by_camera_day_no_leak(tmp_path):
    ds = make(tmp_path, rows(8))
    manifest, _, _, assign = bvd.build(ds, tmp_path / "out", val_fraction=0.25, seed=3)
    sides = {}
    for r in manifest:
        sides.setdefault((r["camera"], r["day"]), set()).add(r["split"])
    assert all(len(s) == 1 for s in sides.values())
    assert {"train", "val"} <= {r["split"] for r in manifest}
    assert sum(1 for s in assign.values() if s == "val") == 2


def test_deterministic_with_seed(tmp_path):
    ds = make(tmp_path, rows(8))
    a, _, _, _ = bvd.build(ds, tmp_path / "o1", seed=5)
    b, _, _, _ = bvd.build(ds, tmp_path / "o2", seed=5)
    assert a == b
    assert (tmp_path / "o1" / "manifest.jsonl").read_bytes() == \
        (tmp_path / "o2" / "manifest.jsonl").read_bytes()
    splits = {s for seed in range(10)
              for s in [tuple(bvd.split_groups([f"g{i}" for i in range(8)], 0.25, seed).items())]}
    assert len(splits) > 1


def test_shadow_log_boxes_and_cli_warning(tmp_path, capsys):
    ds = make(tmp_path, [("p", "cam", 0, "person", None)])
    sha = hashlib.sha256((ds / "p.jpg").read_bytes()).hexdigest()
    log = tmp_path / "shadow.jsonl"
    log.write_text(json.dumps({"frame": sha[:16], "primary_boxes": [[0.1, 0.1, 0.4, 0.5, 0.7]]}) + "\n")
    assert bvd.main([str(ds), str(tmp_path / "out"), "--log", str(log)]) == 0
    text = capsys.readouterr().out
    assert "person     1 crops" in text and "WARNING: class no_person has only 0" in text
    assert "WARNING: class person has only 1" in text
