"""tools/recording_audit.py on a synthetic video and stub scorers (no camera, no models)."""

import importlib.util
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("recording_audit", ROOT / "tools" / "recording_audit.py")
ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ra)

TOKEN = "-".join(["dummy", "value"])
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")


def _listing(days, size=100):
    lines = []
    for d in days:
        for h in range(24):
            for m in (0, 15, 30, 45):
                lines.append(f"{d}/{h:02d}/zaznam_{d.replace('-', '')}T{h:02d}{m:02d}01.mkv\t{size}")
    return lines


DAYS = [f"2026-09-{d:02d}" for d in range(18, 31)] + ["2026-10-01", "2026-10-02", "2026-10-03"]


def test_segment_start_from_name():
    dt = ra.segment_start("2026-10-01/12/zaznam_20261001T120002.mkv")
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) == (2026, 10, 1, 12, 0, 2)
    with pytest.raises(ValueError):
        ra.segment_start("nonsense.mkv")


def test_plan_is_seeded_stratified_and_inside_one_segment():
    lines = _listing(DAYS)
    a = ra.plan_windows(ra.parse_listing(lines), seed=7)
    b = ra.plan_windows(ra.parse_listing(lines), seed=7)
    c = ra.plan_windows(ra.parse_listing(lines), seed=8)
    assert a == b and a != c
    assert [w["stratum"] for w in a].count("day") == 6
    assert [w["stratum"] for w in a].count("night") == 6
    assert len({w["window"] for w in a}) == 12
    for w in a:
        assert 0 <= w["start"] and w["start"] + w["duration"] <= 900
        h = ra.segment_start(w["file"]).hour
        if w["stratum"] == "day":
            assert 9 <= h < 16
        else:
            assert h >= 21 or h < 5
        # newest (partial) day and the two oldest (about to be pruned) are never used
        assert w["file"].split("/")[0] not in (DAYS[0], DAYS[1], DAYS[-1])
    # six windows spread over six different days per stratum
    assert len({w["file"].split("/")[0] for w in a if w["stratum"] == "day"}) == 6


def test_plan_reports_total_bytes_and_rejects_empty():
    plan = ra.plan_windows(ra.parse_listing(_listing(DAYS, size=10)), seed=1)
    assert ra.plan_bytes(plan) == 120
    with pytest.raises(ValueError):
        ra.plan_windows([], seed=1)


def test_strata_cand_lowrand_lowrest_and_seed():
    rows = [{"frame": f"f{i}", "yolox": 0.9 if i < 5 else 0.1, "rfdetr": 0.1} for i in range(10)]
    rows += [{"frame": "g", "yolox": 0.1, "rfdetr": 0.3}]
    out = ra.assign_strata(rows, seed=3, lowrand_n=2)
    by = {r["frame"]: r["stratum"] for r in out}
    assert all(by[f"f{i}"] == "cand" for i in range(5))
    assert by["g"] == "cand"
    assert sum(1 for r in out if r["stratum"] == "lowrand") == 2
    assert sum(1 for r in out if r["stratum"] == "lowrest") == 3
    assert out == ra.assign_strata(rows, seed=3, lowrand_n=2)
    assert out != ra.assign_strata(rows, seed=4, lowrand_n=2)


def test_all_missing_scores_are_error_not_a_low_sample():
    out = ra.assign_strata([{"frame": "h", "yolox": None, "rfdetr": None}], seed=1, lowrand_n=5)
    assert out[0]["stratum"] == "error"


class _Rate:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_limiter_caps_rate():
    t = _Rate()
    lim = ra.Limiter(2.0, clock=t.clock, sleep=t.sleep)
    for _ in range(5):
        lim.wait()
    assert t.now == pytest.approx(2.0)   # 5 calls at 2/s: the first is free, 4 more take 2 s


@needs_ffmpeg
def test_extract_frames_every_n_seconds(tmp_path):
    video = tmp_path / "seg.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5",
                    "-t", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    frames = ra.extract_frames(video, start=5, duration=20, every=5, out_dir=tmp_path / "o")
    assert len(frames) == 4
    assert [f[1] for f in frames] == [0, 5, 10, 15]          # offset inside the window
    assert all(f[0].exists() and f[0].suffix == ".jpg" for f in frames)


class _Handler(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Handler.seen.append((self.headers.get("Authorization"), len(body)))
        out = json.dumps({"person": 0.42}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_score_jpeg_and_token_only_for_non_loopback(tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        jpg = tmp_path / "a.jpg"
        jpg.write_bytes(b"\xff\xd8abc")
        url = f"http://127.0.0.1:{srv.server_port}/score"
        assert ra.score_jpeg(url, jpg, token=TOKEN) == 0.42
        assert _Handler.seen[-1][0] is None                     # loopback: no token sent
        assert ra.score_jpeg(url, jpg, token=TOKEN, force_token=True) == 0.42
        assert _Handler.seen[-1][0] == "Bearer " + TOKEN
        assert ra.send_token(url) is False
        assert ra.send_token("http://192.0.2.5:8767/score") is True
    finally:
        srv.shutdown()


def test_parse_scorer_names():
    assert ra.parse_scorers(["http://a/score"]) == [("yolox", "http://a/score")]
    assert ra.parse_scorers(["http://a/score", "http://b/score"]) == [
        ("yolox", "http://a/score"), ("rfdetr", "http://b/score")]
    assert ra.parse_scorers(["x=http://a/score"]) == [("x", "http://a/score")]
    with pytest.raises(ValueError):
        ra.parse_scorers(["u1", "u2", "u3"])


def test_score_frames_resumes_and_survives_a_failing_scorer(tmp_path):
    frames = tmp_path / "frames"
    (frames / "w01").mkdir(parents=True)
    for i in range(3):
        (frames / "w01" / f"{i}.jpg").write_bytes(b"x")
    calls = []

    def ok(path):
        calls.append(path.name)
        return 0.5

    def bad(path):
        raise OSError("down")

    out = tmp_path / "scores.jsonl"
    meta = [{"frame": f"w01/{i}.jpg", "window": "w01", "ts": 100.0 + i} for i in range(3)]
    ra.score_frames(meta, frames, out, {"yolox": ok, "rfdetr": bad}, rate=None)
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert len(rows) == 3 and all(r["yolox"] == 0.5 and r["rfdetr"] is None for r in rows)
    calls.clear()
    ra.score_frames(meta, frames, out, {"yolox": ok, "rfdetr": bad}, rate=None)
    assert calls == []                                          # nothing scored twice
    assert len(out.read_text().splitlines()) == 3


def _rows(n_cand, n_low):
    rows = []
    for i in range(n_cand):
        rows.append({"frame": f"w01/{i:03d}.jpg", "window": "w01", "ts": 1000.0 + i, "stratum": "cand",
                     "yolox": 0.3 + i / 1000, "rfdetr": 0.2})
    for i in range(n_low):
        rows.append({"frame": f"w02/{i:03d}.jpg", "window": "w02", "ts": 2000.0 + i, "stratum": "lowrand",
                     "yolox": 0.05, "rfdetr": 0.04})
    rows.append({"frame": "w02/rest.jpg", "window": "w02", "ts": 3000.0, "stratum": "lowrest",
                 "yolox": 0.01, "rfdetr": 0.01})
    return rows


def test_build_dataset_layout_caps_and_loads_in_labeler(tmp_path):
    frames = tmp_path / "frames"
    rows = _rows(6, 5)
    for r in rows:
        p = frames / r["frame"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xff\xd8" + r["frame"].encode() + b"\xff\xd9")
    out = tmp_path / "ds"
    n = ra.build_dataset(rows, frames, out, camera="cam", max_cand=4, max_lowrand=3)
    assert n == {"cand": 4, "lowrand": 3}
    recs = [json.loads(x) for x in (out / "audit" / "index.jsonl").read_text().splitlines()]
    assert len(recs) == 7
    assert {r["verdict"] for r in recs} == {"cand", "lowrand"}
    top = [r for r in recs if r["verdict"] == "cand"]
    assert max(r["yolox_m"] for r in top) == pytest.approx(0.305)      # highest candidates kept
    assert all((out / "audit" / r["file"]).is_file() for r in recs)
    assert all(r["camera"] == "cam" and "person" in r and "ts" in r for r in recs)
    assert all(r["frame"] == f"{r['window']}/{r['file'].rsplit('_', 1)[1]}" for r in recs)
    from tapo_monitor import labeling
    assert len(labeling.load_frames(out)) == 7


def test_cli_plan_writes_json_and_prints_size(tmp_path, capsys):
    lst = tmp_path / "l.txt"
    lst.write_text("\n".join(_listing(DAYS, size=1000)) + "\n")
    out = tmp_path / "plan.json"
    files = tmp_path / "files.txt"
    assert ra.main(["plan", "--listing", str(lst), "--out", str(out), "--seed", "5",
                    "--files-from", str(files)]) == 0
    plan = json.loads(out.read_text())
    assert len(plan["windows"]) == 12 and plan["bytes"] == 12000
    assert "12000" in capsys.readouterr().out
    assert len(files.read_text().split()) == 12
