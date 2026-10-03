"""Partially decoded HEVC frames (grey base with a few intra blocks) are not scored."""

import io
import json
import threading
import urllib.request

import pytest

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")

from tapo_monitor import scorer_service  # noqa: E402

W, H = 640, 360


def _jpeg(arr):
    buf = io.BytesIO()
    Image.fromarray(arr.astype("uint8")).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _grey_with_patch():
    arr = np.full((H, W, 3), 128, dtype="uint8")
    arr[200:260, 300:330] = (180, 90, 60)       # a person-sized decoded block
    arr[300:320, 100:160] = (40, 60, 200)       # a car-sized decoded block
    return arr


def _noisy(mean, sigma, seed=1):
    rng = np.random.default_rng(seed)
    base = rng.normal(mean, sigma, (H, W, 1))
    return np.clip(np.repeat(base, 3, axis=2), 0, 255)


def _fog():
    yy, xx = np.mgrid[0:H, 0:W]
    wave = 16 * np.sin(xx / 70.0) * np.cos(yy / 45.0) + 8 * np.sin((xx + yy) / 23.0)
    return np.repeat((128 + wave)[:, :, None], 3, axis=2)


def _normal_scene():
    rng = np.random.default_rng(7)
    x = np.linspace(40, 220, W)[None, :, None]
    y = np.linspace(0, 40, H)[:, None, None]
    return np.clip(x + y + rng.normal(0, 12, (H, W, 3)), 0, 255)


def test_grey_frame_with_small_patch_is_artifact():
    assert scorer_service.is_decode_artifact(_jpeg(_grey_with_patch())) is True


def test_pure_grey_is_artifact():
    assert scorer_service.is_decode_artifact(_jpeg(np.full((H, W, 3), 128))) is True


@pytest.mark.parametrize("name", ["dark", "ir", "fog", "normal", "dim_grey"])
def test_real_scenes_are_not_artifacts(name):
    arr = {
        "dark": _noisy(14, 4),
        "ir": _noisy(95, 18),                    # grainy infrared grey, mean below the band
        "fog": _fog(),                           # foggy mid-grey with soft texture
        "normal": _normal_scene(),
        "dim_grey": _noisy(150, 3),              # flat but wrong mean
    }[name]
    assert scorer_service.is_decode_artifact(_jpeg(arr)) is False


def test_undecodable_bytes_are_not_artifacts():
    assert scorer_service.is_decode_artifact(b"jpegbytes") is False


def _serve(**kwargs):
    calls = []

    def score(body, tiles=1):
        calls.append(1)
        return {"person": 0.7, "animal": 0.1, "vehicle": 0.2, "box": [1, 2, 3, 4],
                "person_scores": [0.7], "person_boxes": [[1, 2, 3, 4]]}

    srv = scorer_service.make_server(score, port=0, **kwargs)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", calls


def _post(base, body):
    req = urllib.request.Request(base + "/score", data=body)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _metrics(base):
    with urllib.request.urlopen(base + "/metrics", timeout=5) as resp:
        return json.loads(resp.read())


def test_artifact_reply_is_zeroed_and_skips_inference():
    srv, base, calls = _serve()
    try:
        out = _post(base, _jpeg(_grey_with_patch()))
        assert out["artifact"] is True
        assert out["person"] == out["animal"] == out["vehicle"] == 0
        assert out["person_scores"] == [] and out["person_boxes"] == []
        assert out["box"] is None
        assert calls == []
        assert _metrics(base)["artifact"] == 1
    finally:
        srv.shutdown()


def test_normal_reply_is_unchanged_and_counter_zero():
    srv, base, calls = _serve()
    try:
        out = _post(base, _jpeg(_normal_scene()))
        assert out["person"] == 0.7 and "artifact" not in out
        assert calls == [1]
        assert _metrics(base)["artifact"] == 0
    finally:
        srv.shutdown()


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("TAPO_SCORER_ARTIFACT_FILTER", "0")
    srv, base, calls = _serve()
    try:
        out = _post(base, _jpeg(_grey_with_patch()))
        assert out["person"] == 0.7 and "artifact" not in out
        assert calls == [1]
        assert _metrics(base)["artifact"] == 0
    finally:
        srv.shutdown()


@pytest.mark.parametrize("value,expected", [
    (None, True), ("", True), ("1", True), ("0", False), (" off ", False),
    ("false", False), ("no", False),
])
def test_env_parsing(value, expected):
    env = {} if value is None else {"TAPO_SCORER_ARTIFACT_FILTER": value}
    assert scorer_service.artifact_filter_enabled(env) is expected
