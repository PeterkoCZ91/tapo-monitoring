"""tools/sd_frame_dump.py, frame_pick_eval.py, frame_pick_sim.py: the pure parts."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dump = _load("sd_frame_dump")
fpe = _load("frame_pick_eval")
sim = _load("frame_pick_sim")

START = 1_700_000_000
SIZE = (1280, 720)


def rec(offset, person=0.0, height=None, blur=1.0, frame_var=1000.0, start=START):
    """A feature record for the frame ``offset`` s after ``start``; box of ``height`` px."""
    box = [600.0, 300.0, 660.0, 300.0 + height] if height else None
    return {"frame": f"/x/{start}/sdf_{start}_1_{offset:02d}_at{start + offset}.jpg",
            "person": person, "box": box, "blur": blur if box else None,
            "frame_var": frame_var}


def clip(people=None, smeared=(), n=37):
    """``n`` frames at 1 s; ``people`` maps offset -> (person, height)."""
    people = people or {}
    return [rec(o, *(people.get(o) or (0.0, None)), frame_var=100.0 if o in smeared else 1000.0)
            for o in range(n)]


# ── sd_frame_dump ────────────────────────────────────────────────────────────

def test_event_starts_filters_path_age_camera_and_dedupes():
    now = 1_000_000
    rows = [
        {"ts": now - 10, "path": "sd", "event_start": 50, "camera": "front"},
        {"ts": now - 20, "path": "sd", "event_start": 50, "camera": "front"},
        {"ts": now - 30, "path": "sd", "event_start": 40},                     # no camera: kept
        {"ts": now - 40, "path": "live", "event_start": 30, "camera": "front"},
        {"ts": now - 3 * 86400, "path": "sd", "event_start": 20, "camera": "front"},
        {"ts": now - 50, "path": "sd", "event_start": 10, "camera": "cam1"},
        {"ts": now - 60, "path": "sd", "camera": "front"},                     # no event_start
        {"ts": "bad", "path": "sd", "event_start": 5},
    ]
    assert dump.event_starts(rows, now, 2, camera="front") == [40, 50]
    assert dump.event_starts(rows, now, 2) == [10, 40, 50]


def test_read_index_skips_garbage(tmp_path):
    p = tmp_path / "index.jsonl"
    p.write_text('{"ts": 1, "path": "sd"}\nnot json\n[1]\n{"ts": 2}\n')
    assert [r["ts"] for r in dump.read_index(p)] == [1, 2]


def test_credentials_come_from_the_named_env_vars(monkeypatch):
    monkeypatch.setenv("CAM1_USER", "u")
    monkeypatch.setenv("CAM1_PASS", "p")
    monkeypatch.delenv("CAM1_CLOUD", raising=False)
    cam = SimpleNamespace(host="192.0.2.10", user_env="CAM1_USER", password_env="CAM1_PASS",
                          cloud_password_env="CAM1_CLOUD")
    assert dump.camera_login(cam) == ("192.0.2.10", "u", "p", "p")


def test_dump_is_sequential_skips_existing_and_survives_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CAM1_USER", "u")
    monkeypatch.setenv("CAM1_PASS", "p")
    cam = SimpleNamespace(host="192.0.2.10", user_env="CAM1_USER", password_env="CAM1_PASS",
                          cloud_password_env=None, rotate=0)
    (tmp_path / "10").mkdir()
    (tmp_path / "10" / "old.jpg").write_bytes(b"x")
    calls, sleeps = [], []

    def fetch(client, start, out_dir, span, every, rotate):
        calls.append((start, span, every))
        if start == 20:
            raise RuntimeError("-40214")
        return [f"{out_dir}/a.jpg", f"{out_dir}/b.jpg"]

    done = dump.dump(cam, [10, 20, 30], str(tmp_path), 30, 1, 5, fetch=fetch,
                     client_for=lambda: object(), sleep=sleeps.append, log=lambda m: None)
    assert done == {10: None, 20: 0, 30: 2}
    assert calls == [(20, 30, 1), (30, 30, 1)]
    assert sleeps == [5]


# ── frame_pick_eval ──────────────────────────────────────────────────────────

def test_frame_features_with_and_without_box():
    seen = []

    def blur(path, box=None):
        seen.append(box)
        return 2.5

    kw = {"size": [1280, 720], "blur": blur, "crop": lambda p, b: 39.0, "detail": lambda p: 800.0}
    hit = fpe.frame_features("f.jpg", {"person": 0.8, "box": [1, 2, 30, 90], "count": 1}, **kw)
    assert hit == {"frame": "f.jpg", "size": [1280, 720], "scored": True, "person": 0.8,
                   "box": [1.0, 2.0, 30.0, 90.0], "people": 1, "blur": 2.5, "crop_var": 39.0,
                   "frame_var": 800.0}
    assert seen == [[1.0, 2.0, 30.0, 90.0]]
    miss = fpe.frame_features("f.jpg", None, **kw)
    assert miss["scored"] is False and miss["box"] is None and miss["blur"] is None
    assert miss["frame_var"] == 800.0


def test_score_first_falls_back_in_order():
    answers = {"a": None, "b": {"person": 0.4}, "c": {"person": 0.9}}
    asked = []

    def score(url, path, **kw):
        asked.append(url)
        return answers[url]

    assert fpe.score_first(["a", "b", "c"], "f.jpg", score, tiles=2) == {"person": 0.4}
    assert asked == ["a", "b"]
    assert fpe.score_first(["a"], "f.jpg", score) is None


def test_event_dirs_and_evaluate_skip_unreadable(tmp_path):
    for ev in ("200", "100"):
        (tmp_path / ev).mkdir()
        (tmp_path / ev / "f_at1.jpg").write_bytes(b"x")
        (tmp_path / ev / "f_at2.jpg").write_bytes(b"x")
    (tmp_path / "empty").mkdir()
    found = fpe.event_dirs([str(tmp_path)])
    assert [name for name, _d in found] == ["100", "200"]
    assert fpe.event_dirs([str(tmp_path / "100")])[0][0] == "100"

    events = [(name, fpe._jpegs(d)) for name, d in found]
    out = fpe.evaluate(events, lambda p: None if p.endswith("at2.jpg") else {"frame": p}, workers=1)
    assert {k: len(v) for k, v in out.items()} == {"100": 1, "200": 1}


def test_unreadable_image_size_raises(tmp_path):
    pytest.importorskip("PIL")
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a jpeg")
    with pytest.raises(Exception):  # noqa: B017 - any decode error; the CLI skips the frame
        fpe._image_size(str(bad))


# ── frame_pick_sim ───────────────────────────────────────────────────────────

def test_parse_variant_roundtrip_and_errors():
    assert sim.parse_variant("dense+tallest+guard") == ("dense", "tallest", True)
    assert sim.parse_variant("grid6+sharpest") == ("grid6", "sharpest", False)
    assert sim.variant_name("grid6", "largest", True) == "grid6+largest+guard"
    for bad in ("grid6", "nope+tallest", "grid6+best", "grid6+tallest+x"):
        with pytest.raises(ValueError):
            sim.parse_variant(bad)
    assert len(sim.default_variants()) == 4 * 3 * 2


def test_grid_frames_follow_frame_offsets_and_capture_time():
    frames = clip()
    assert [sim.frame_time(f) - START for f in sim.grid_frames(frames, START, "grid6")] == \
        [0, 6, 12, 18, 24, 30]
    dense = [sim.frame_time(f) - START for f in sim.grid_frames(frames, START, "dense")]
    assert dense == [0, 2, 4, 6, 8, 10, 12, 18, 24, 30]
    assert sim.grid_frames(frames, START, "all") == frames
    # A frame missing from the clip is simply not there.
    assert len(sim.grid_frames(frames[1:], START, "grid6")) == 5


def test_pick_tallest_beats_sharpest_and_threshold_applies():
    frames = clip({6: (0.9, 100), 12: (0.5, 200), 18: (0.2, 400)})
    frames[6]["blur"] = 0.5   # sharpest
    assert sim.pick(frames, "sharpest", False, 0.3)["frame"] == frames[6]["frame"]
    assert sim.pick(frames, "tallest", False, 0.3, SIZE)["frame"] == frames[12]["frame"]
    assert sim.pick(frames, "tallest", False, 0.95) is None


def test_guard_drops_smeared_pick():
    frames = clip({6: (0.9, 100), 12: (0.8, 300)}, smeared={12})
    assert sim.pick(frames, "tallest", False, 0.3, SIZE)["frame"] == frames[12]["frame"]
    assert sim.pick(frames, "tallest", True, 0.3, SIZE)["frame"] == frames[6]["frame"]
    # The guard never empties the candidate list.
    only = clip({12: (0.8, 300)}, smeared={12})
    assert sim.pick(only, "tallest", True, 0.3, SIZE)["frame"] == only[12]["frame"]


def test_oracle_ignores_smeared_and_edge_boxes():
    frames = clip({3: (0.9, 150), 5: (0.9, 500), 7: (0.9, 300)}, smeared={5})
    frames[7]["box"] = [0.0, 100.0, 50.0, 400.0]  # touches the left edge
    assert sim.oracle(frames, 0.3, SIZE)["frame"] == frames[3]["frame"]
    assert sim.oracle(clip(), 0.3, SIZE) is None


def test_evaluate_metrics():
    # Event A: the subject is tallest at 3 s, between the grid points; the dense grid
    # (every 2 s over the first 12 s) misses 3 s too but catches 4 s.
    a = clip({3: (0.9, 400), 4: (0.9, 360), 6: (0.8, 200)})
    # Event B: only visible at 1 s - both grids miss it.
    b = [rec(o, *((0.9, 300) if o == 1 else (0.0, None)), start=START + 1000) for o in range(37)]
    # Event C: no subject at all -> not counted.
    c = [rec(o, start=START + 2000) for o in range(37)]
    feats = {str(START): a, str(START + 1000): b, str(START + 2000): c, "notes": []}
    variants = ["grid6+tallest", "dense+tallest+guard", "all+tallest"]
    events, summary = sim.evaluate(feats, variants, 0.3)

    assert [e["event"] for e in events] == [str(START), str(START + 1000)]
    assert events[0]["picks"]["grid6+tallest"]["ratio"] == pytest.approx(0.5)
    assert events[0]["picks"]["dense+tallest+guard"]["ratio"] == pytest.approx(0.9)
    assert events[1]["picks"]["grid6+tallest"] is None
    assert summary["grid6+tallest"] == {"mean_ratio": pytest.approx(0.5), "picked": 1,
                                        "smeared": 0, "missed": 1}
    assert summary["all+tallest"]["mean_ratio"] == pytest.approx(1.0)
    assert summary["all+tallest"]["missed"] == 0

    lines = sim.format_summary(events, summary)
    assert lines[0] == "events with a subject: 2"
    assert any(line.startswith("grid6+tallest") and line.split()[-3:] == ["0.50", "0", "1"]
               for line in lines)
    assert sim.format_pick(None) == "miss"
    assert sim.format_pick({"ratio": 0.5, "smeared": True}) == "0.50*"


def test_evaluate_counts_smeared_picks_and_caps_ratio():
    # Smeared frame at 6 s is the tallest on the grid; the oracle (steady) is shorter.
    frames = clip({6: (0.9, 600), 9: (0.9, 200)}, smeared={6})
    events, summary = sim.evaluate({str(START): frames},
                                   ["grid6+tallest", "grid6+tallest+guard"], 0.3)
    assert summary["grid6+tallest"]["smeared"] == 1
    assert summary["grid6+tallest"]["mean_ratio"] == pytest.approx(sim.RATIO_CAP)
    assert events[0]["picks"]["grid6+tallest"]["ratio"] == pytest.approx(3.0)


def test_frame_size_from_records():
    assert sim.frame_size([{"size": [640, 360]}]) == (640, 360)
    assert sim.frame_size([{}], default=(1, 2)) == (1, 2)


def test_gallery_and_sheets_write_files(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    ev = tmp_path / "frames" / str(START)
    ev.mkdir(parents=True)
    frames = []
    for o, (p, h) in {0: (0.9, 100), 1: (0.0, None), 2: (0.8, 300)}.items():
        path = ev / f"sdf_{START}_1_{o:02d}_at{START + o}.jpg"
        Image.new("RGB", (128, 72), (40 * o, 80, 120)).save(path)
        r = rec(o, p, h and h / 10)
        r["frame"], r["size"] = str(path), [128, 72]
        if r["box"]:
            r["box"] = [10.0, 5.0, 20.0, 5.0 + h / 10]
        frames.append(r)
    feats = {str(START): frames}
    variants = [sim.DEFAULT_BEFORE, sim.DEFAULT_AFTER]
    events, summary = sim.evaluate(feats, variants, 0.3)

    page = sim.write_gallery(events, summary, str(tmp_path / "g"), *variants)
    text = Path(page).read_text()
    assert f'src="img/{START}_a.jpg"' in text and f'src="img/{START}_b.jpg"' in text
    assert "http" not in text  # self-contained
    assert (tmp_path / "g" / "img" / f"{START}_a.jpg").exists()

    assert sim.write_sheets(feats, str(tmp_path / "s"), 0.3) == 1
    assert Image.open(tmp_path / "s" / f"{START}.jpg").size == (6 * 320, 196)


def test_cli_prints_summary(tmp_path, capsys):
    path = tmp_path / "f.json"
    path.write_text(json.dumps({str(START): clip({6: (0.9, 200), 7: (0.9, 400)})}))
    assert sim.main([str(path), "--variants", "grid6+sharpest,all+tallest"]) == 0
    out = capsys.readouterr().out
    assert "events with a subject: 1" in out and "all+tallest" in out
    assert sim.main([str(path), "--variants", "bogus"]) == 2
