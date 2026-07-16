import json
import os
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

np = pytest.importorskip("numpy")  # service tests need numpy; core package does not

from tapo_monitor import scorer_service


@pytest.fixture
def server():
    srv = scorer_service.make_server(
        lambda body, tiles=1: {"person": 0.8, "animal": 0.0, "n": len(body), "tiles": tiles},
        port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_post_score_returns_json(server):
    req = urllib.request.Request(f"{server}/score", data=b"jpegbytes",
                                 headers={"Content-Type": "image/jpeg"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        out = json.load(resp)
    assert out == {"person": 0.8, "animal": 0.0, "n": 9, "tiles": 1}


def test_health(server):
    with urllib.request.urlopen(f"{server}/health", timeout=5) as resp:
        assert json.load(resp) == {"ok": True}


def test_unknown_path_404(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{server}/nope", timeout=5)
    assert e.value.code == 404


def test_score_fn_error_500():
    def boom(_body, tiles=1):
        raise ValueError("bad image")
    srv = scorer_service.make_server(boom, port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/score", data=b"x")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 500
    finally:
        srv.shutdown()


def test_scores_from_output_person_and_animal():
    out = np.zeros((1, 100, 85), dtype=np.float32)
    out[0, 3, 4] = 0.9          # objectness
    out[0, 3, 5 + 0] = 0.8      # person class -> 0.72
    out[0, 7, 4] = 0.5
    out[0, 7, 5 + 16] = 0.6     # dog -> 0.30
    scores = scorer_service.scores_from_output(out)
    assert scores["person"] == pytest.approx(0.72, abs=1e-4)
    assert scores["animal"] == pytest.approx(0.30, abs=1e-4)


def test_scores_from_output_empty_is_zero():
    out = np.zeros((1, 10, 85), dtype=np.float32)
    assert scorer_service.scores_from_output(out) == {"person": 0.0, "animal": 0.0, "classes": {}}

def test_scores_from_output_returns_named_classes():
    out = np.zeros((1, 4, 85), dtype=np.float32)
    out[0, 0, 4] = 0.9
    out[0, 0, 5 + 0] = 0.8       # person -> 0.72
    out[0, 1, 4] = 0.5
    out[0, 1, 5 + 16] = 0.6      # dog -> 0.30
    out[0, 2, 4] = 0.4
    out[0, 2, 5 + 2] = 0.02      # car -> 0.008, below floor

    scores = scorer_service.scores_from_output(out)

    assert scores["person"] == pytest.approx(0.72, abs=1e-4)
    assert scores["animal"] == pytest.approx(0.30, abs=1e-4)
    assert scores["classes"]["person"] == pytest.approx(0.72, abs=1e-4)
    assert scores["classes"]["dog"] == pytest.approx(0.30, abs=1e-4)
    assert "car" not in scores["classes"]

def test_best_person_box_decodes_grid_and_stride():
    # input 64 -> grids 8x8(s8)+4x4(s16)+2x2(s32) = 84 anchors. Anchor 0 = grid(0,0),
    # stride 8, raw box (0,0,0,0) -> cx=cy=0, w=h=exp(0)*8=8 -> xyxy (-4,-4,4,4).
    out = np.zeros((1, 84, 85), dtype=np.float32)
    out[0, 0, 4] = 0.9
    out[0, 0, 5 + 0] = 0.8                  # person conf 0.72 on anchor 0
    box = scorer_service.best_person_box(out, input_size=64)
    assert box == pytest.approx((-4.0, -4.0, 4.0, 4.0))


def test_best_person_box_none_below_floor():
    out = np.zeros((1, 84, 85), dtype=np.float32)
    out[0, 0, 4] = 0.1
    out[0, 0, 5 + 0] = 0.1                  # 0.01, below default floor 0.05
    assert scorer_service.best_person_box(out, input_size=64) is None


def test_best_person_box_falls_back_when_anchor_count_mismatch():
    # 3 anchors != any grid count -> treat head as already-decoded cx,cy,w,h
    out = np.zeros((1, 3, 85), dtype=np.float32)
    out[0, 1, :4] = [100, 80, 40, 60]
    out[0, 1, 4] = 0.9
    out[0, 1, 5 + 0] = 0.8
    assert scorer_service.best_person_box(out, input_size=640) == \
        pytest.approx((80.0, 50.0, 120.0, 110.0))


def test_tile_rects_whole_frame_only_when_tiles_1():
    assert scorer_service.tile_rects(400, 300, 1) == [(0.0, 0.0, 400.0, 300.0)]


def test_tile_rects_grid_count_and_bounds():
    rects = scorer_service.tile_rects(400, 300, 2, overlap=0.0)
    assert len(rects) == 5                   # whole frame + 2x2
    assert rects[0] == (0.0, 0.0, 400.0, 300.0)
    assert rects[1] == (0.0, 0.0, 200.0, 150.0)          # top-left cell
    assert rects[4] == (200.0, 150.0, 400.0, 300.0)      # bottom-right cell
    for x0, y0, x1, y1 in rects:             # all within the frame
        assert 0.0 <= x0 < x1 <= 400.0 and 0.0 <= y0 < y1 <= 300.0


def _rect(person, animal=0.0, box=None, classes=None):
    return {"person": person, "animal": animal,
            "classes": classes or ({"person": person} if person else {}), "box": box}


def test_combine_full_frame_score_decides_tile_only_adds_box():
    # Tile hallucinations must not raise the decision score: person/animal come
    # from the full frame; the best tile only contributes box + tile_person.
    combined = scorer_service.combine_rect_scores(
        [_rect(0.05), _rect(0.55, box=[10, 20, 30, 60]), _rect(0.40, box=[1, 2, 3, 4])])
    assert combined["person"] == 0.05
    assert combined["tile_person"] == 0.55
    assert combined["box"] == [10, 20, 30, 60]     # from the best-person tile


def test_combine_prefers_full_frame_box_when_present():
    combined = scorer_service.combine_rect_scores(
        [_rect(0.9, box=[100, 100, 200, 300]), _rect(0.95, box=[5, 5, 9, 9])])
    assert combined["person"] == 0.9
    assert combined["box"] == [100, 100, 200, 300]


def test_combine_animal_also_full_frame_only():
    combined = scorer_service.combine_rect_scores(
        [_rect(0.0, animal=0.1), _rect(0.0, animal=0.8)])
    assert combined["animal"] == 0.1


def test_combine_single_rect_has_no_tile_person():
    combined = scorer_service.combine_rect_scores([_rect(0.7, box=[1, 2, 3, 4])])
    assert combined["person"] == 0.7
    assert combined["box"] == [1, 2, 3, 4]
    assert "tile_person" not in combined


def test_scale_box_undoes_ratio_and_adds_tile_offset():
    # tile-input box at ratio 0.5 -> /0.5 = *2, then shift by the tile origin
    assert scorer_service.scale_box((10, 20, 30, 40), 0.5, 100, 200) == \
        pytest.approx((120.0, 240.0, 160.0, 280.0))


def test_scores_from_output_uses_darknet_coco_names():
    out = np.zeros((1, 1, 85), dtype=np.float32)
    out[0, 0, 4] = 0.5
    out[0, 0, 5 + 3] = 0.8       # darknet coco.names class 3: motorbike

    scores = scorer_service.scores_from_output(out)

    assert scores["classes"]["motorbike"] == pytest.approx(0.4, abs=1e-4)
    assert "motorcycle" not in scores["classes"]

