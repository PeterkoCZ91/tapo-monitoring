"""Detector families behind the scorer service: YOLOX (default) and DETR-style exports."""
import io
import json
import math
import os
import sys
import threading
import types
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

np = pytest.importorskip("numpy")

from tapo_monitor import detectors, scorer_service  # noqa: E402

# ── configuration ───────────────────────────────────────────────────────────

def test_defaults_are_yolox_416():
    cfg = detectors.detector_config({})
    assert (cfg.arch, cfg.input_size) == ("yolox", 416)
    assert detectors.build_decoder(cfg).name == "yolox"


def test_blank_values_mean_default():
    cfg = detectors.detector_config({"TAPO_SCORER_ARCH": " ", "TAPO_SCORER_INPUT_SIZE": ""})
    assert (cfg.arch, cfg.input_size) == ("yolox", 416)


def test_detr_config_from_env():
    cfg = detectors.detector_config({
        "TAPO_SCORER_ARCH": "DETR", "TAPO_SCORER_DETR_PERSON_ID": "1",
        "TAPO_SCORER_DETR_ACTIVATION": "Sigmoid", "TAPO_SCORER_INPUT_SIZE": "560"})
    assert (cfg.arch, cfg.person_id, cfg.activation, cfg.input_size) == (
        "detr", 1, "sigmoid", 560)


def test_cli_input_size_beats_env():
    cfg = detectors.detector_config({"TAPO_SCORER_INPUT_SIZE": "512"}, cli_input_size=640)
    assert cfg.input_size == 640


@pytest.mark.parametrize("env", [
    {"TAPO_SCORER_ARCH": "yolov99"},
    {"TAPO_SCORER_ARCH": "detr", "TAPO_SCORER_DETR_ACTIVATION": "relu"},
    {"TAPO_SCORER_ARCH": "detr", "TAPO_SCORER_DETR_PERSON_ID": "7"},
    {"TAPO_SCORER_ARCH": "detr", "TAPO_SCORER_DETR_PERSON_ID": "x"},
    {"TAPO_SCORER_ARCH": "detr", "TAPO_SCORER_DETR_NORMALIZE": "gamma"},
    {"TAPO_SCORER_INPUT_SIZE": "abc"},
    {"TAPO_SCORER_INPUT_SIZE": "0"},
    {"TAPO_SCORER_INPUT_SIZE": "-640"},
])
def test_unknown_values_are_a_clear_startup_error(env):
    with pytest.raises(detectors.DetectorConfigError) as err:
        detectors.detector_config(env)
    assert "TAPO_SCORER" in str(err.value)


# ── YOLOX decoder keeps today's results ─────────────────────────────────────

def _yolox_out():
    out = np.zeros((1, 84, 85), dtype=np.float32)
    for i, conf in ((0, 0.55), (7, 0.90)):
        out[0, i, 4] = 1.0
        out[0, i, 5] = conf
    out[0, 20, 4] = 1.0
    out[0, 20, 5 + 2] = 0.7            # a car
    out[0, 30, 4] = 1.0
    out[0, 30, 5 + 16] = 0.4           # a dog
    return out


def test_yolox_decoder_matches_the_module_functions():
    out = _yolox_out()
    dec = detectors.build_decoder(detectors.detector_config({}, cli_input_size=64))
    got = dec.decode([out], 0.5, full=True)
    ref = scorer_service.scores_from_output(out)
    assert (got["person"], got["animal"], got["vehicle"], got["classes"]) == (
        ref["person"], ref["animal"], ref["vehicle"], ref["classes"])
    scores, boxes = scorer_service.person_scores(out, 64, with_boxes=True)
    assert got["person_scores"] == scores
    assert got["person_boxes"] == [scorer_service.scale_box(b, 0.5, 0, 0) for b in boxes]
    v_scores, _ = scorer_service.vehicle_scores(out, 64, with_boxes=True)
    assert got["vehicle_scores"] == v_scores
    box = scorer_service.best_person_box(out, 64)
    assert got["box"] == scorer_service.scale_box(box, 0.5, 0, 0)


def test_yolox_decoder_empty_output():
    dec = detectors.build_decoder(detectors.detector_config({}, cli_input_size=64))
    got = dec.decode([np.zeros((1, 0, 85), dtype=np.float32)], 1.0, full=True)
    assert got["person"] == 0.0 and got["box"] is None
    assert got["person_scores"] == [] and got["vehicle_scores"] == []


def test_yolox_preprocess_is_the_existing_letterbox():
    image_mod = pytest.importorskip("PIL.Image")
    img = image_mod.new("RGB", (128, 72))
    dec = detectors.build_decoder(detectors.detector_config({}, cli_input_size=64))
    tensor, ratio = dec.preprocess(img)
    ref, ref_ratio = scorer_service.preprocess_image(img, 64)
    assert ratio == ref_ratio and np.array_equal(tensor, ref)


# ── DETR decoder ────────────────────────────────────────────────────────────

def _detr(activation="softmax", person_id=0, size=64):
    return detectors.build_decoder(detectors.detector_config({
        "TAPO_SCORER_ARCH": "detr", "TAPO_SCORER_DETR_ACTIVATION": activation,
        "TAPO_SCORER_DETR_PERSON_ID": str(person_id)}, cli_input_size=size))


def _outputs(rows, classes, activation, strength=12.0):
    """Synthetic DETR head: ``rows`` is [(class_index, (cx, cy, w, h) normalised)]."""
    q = len(rows) + 3                       # a few idle queries
    logits = np.full((1, q, classes), -strength, dtype=np.float32)
    boxes = np.zeros((1, q, 4), dtype=np.float32)
    for i, (cls, box) in enumerate(rows):
        logits[0, i, cls] = strength
        boxes[0, i] = box
    if activation == "softmax":
        logits[0, len(rows):, -1] = strength           # idle queries say "no object"
    return logits, boxes


@pytest.mark.parametrize("activation,classes", [("softmax", 81), ("sigmoid", 80)])
def test_detr_coco80_person_car_dog(activation, classes):
    dec = _detr(activation, person_id=0)
    logits, boxes = _outputs([(0, (0.5, 0.5, 0.2, 0.4)), (2, (0.25, 0.25, 0.1, 0.1)),
                              (16, (0.8, 0.8, 0.1, 0.2))], classes, activation)
    got = dec.decode([logits, boxes], (200, 100), full=True)
    assert got["person"] > 0.99 and got["animal"] > 0.99 and got["vehicle"] > 0.99
    assert got["person_scores"] == pytest.approx([got["person"]])
    assert got["person_boxes"][0] == pytest.approx((80.0, 30.0, 120.0, 70.0))
    assert got["box"] == pytest.approx(got["person_boxes"][0])
    assert got["vehicle_boxes"][0] == pytest.approx((40.0, 20.0, 60.0, 30.0))
    assert set(got["classes"]) == {"person", "car", "dog"}


def test_detr_coco91_ids():
    # COCO91: person 1, car 3, dog 18
    dec = _detr("sigmoid", person_id=1)
    logits, boxes = _outputs([(1, (0.5, 0.5, 0.2, 0.4)), (3, (0.25, 0.25, 0.1, 0.1)),
                              (18, (0.8, 0.8, 0.1, 0.2))], 91, "sigmoid")
    got = dec.decode([logits, boxes], (200, 100), full=True)
    assert got["person"] > 0.99 and got["vehicle"] > 0.99 and got["animal"] > 0.99
    assert set(got["classes"]) == {"person", "car", "dog"}
    # the same logits under COCO80 mapping would not be a person at index 1
    assert _detr("sigmoid", person_id=0).decode(
        [logits, boxes], (200, 100), full=True)["person"] < 0.5


def test_detr_two_people_sorted_and_capped():
    dec = _detr("sigmoid")
    logits, boxes = _outputs([(0, (0.2, 0.5, 0.1, 0.2)), (0, (0.7, 0.5, 0.1, 0.2))],
                             80, "sigmoid")
    logits[0, 0, 0] = 0.0                     # sigmoid(0) = 0.5
    got = dec.decode([logits, boxes], (100, 100), full=True)
    assert len(got["person_scores"]) == 2
    assert got["person_scores"][0] > got["person_scores"][1]
    assert got["person_scores"][1] == pytest.approx(0.5, abs=1e-3)
    assert got["person_boxes"][0] == pytest.approx((65.0, 40.0, 75.0, 60.0))
    many = _outputs([(0, (0.05 * (i + 1), 0.5, 0.02, 0.02)) for i in range(15)],
                    80, "sigmoid")
    assert len(dec.decode(list(many), (100, 100), full=True)["person_scores"]) == 10


def test_detr_output_order_does_not_matter():
    dec = _detr("sigmoid")
    logits, boxes = _outputs([(0, (0.5, 0.5, 0.2, 0.4))], 80, "sigmoid")
    got = dec.decode([boxes, logits], (100, 100), full=True)
    assert got["person"] > 0.99


def test_detr_boxes_are_clipped_to_the_frame():
    dec = _detr("sigmoid")
    logits, boxes = _outputs([(0, (0.98, 0.5, 0.2, 0.4))], 80, "sigmoid")
    box = dec.decode([logits, boxes], (100, 100), full=True)["person_boxes"][0]
    assert box[2] == 100.0


def test_detr_tile_decode_skips_lists():
    dec = _detr("sigmoid")
    logits, boxes = _outputs([(0, (0.5, 0.5, 0.2, 0.4))], 80, "sigmoid")
    got = dec.decode([logits, boxes], (100, 100), full=False)
    assert got["person"] > 0.99 and got["box"] is not None
    assert "person_scores" not in got


@pytest.mark.parametrize("activation,classes", [("softmax", 81), ("sigmoid", 80)])
def test_detr_empty_output(activation, classes):
    dec = _detr(activation)
    got = dec.decode([np.zeros((1, 0, classes), np.float32),
                      np.zeros((1, 0, 4), np.float32)], (100, 100), full=True)
    assert got["person"] == 0.0 and got["box"] is None and got["person_scores"] == []
    assert got["classes"] == {}


def test_detr_all_below_floor_has_no_box_or_list_entries():
    dec = _detr("sigmoid")
    logits = np.full((1, 5, 80), -9.0, np.float32)
    got = dec.decode([logits, np.full((1, 5, 4), 0.5, np.float32)], (100, 100), full=True)
    assert got["box"] is None and got["person_scores"] == []


@pytest.mark.parametrize("activation,classes", [("softmax", 81), ("sigmoid", 80)])
def test_detr_nan_is_zero_not_a_crash(activation, classes):
    dec = _detr(activation)
    logits = np.full((1, 4, classes), np.nan, np.float32)
    boxes = np.full((1, 4, 4), np.nan, np.float32)
    got = dec.decode([logits, boxes], (100, 100), full=True)
    assert got["person"] == 0.0 and got["animal"] == 0.0 and got["vehicle"] == 0.0
    assert got["person_scores"] == [] and got["box"] is None
    json.dumps(got)


def test_detr_nan_box_of_a_real_score_is_dropped():
    dec = _detr("sigmoid")
    logits, boxes = _outputs([(0, (0.5, 0.5, 0.2, 0.4))], 80, "sigmoid")
    boxes[0, 0] = np.nan
    got = dec.decode([logits, boxes], (100, 100), full=True)
    assert got["person"] > 0.99 and got["box"] is None and got["person_boxes"] == []
    assert not math.isnan(got["person"])


@pytest.mark.parametrize("outputs", [
    [np.zeros((1, 5, 81), np.float32)],                                 # no boxes
    [np.zeros((5, 81), np.float32), np.zeros((5, 4), np.float32)],      # no batch axis
    [np.zeros((1, 5, 81), np.float32), np.zeros((1, 6, 4), np.float32)],  # query mismatch
    [np.zeros((1, 5, 4), np.float32), np.zeros((1, 5, 4), np.float32)],  # two box heads
])
def test_detr_wrong_shape_is_an_error_not_silence(outputs):
    with pytest.raises(ValueError):
        _detr("softmax").decode(outputs, (100, 100), full=True)


def test_detr_fewer_classes_than_coco_counts_missing_as_zero():
    dec = _detr("sigmoid")
    logits, boxes = _outputs([(0, (0.5, 0.5, 0.2, 0.4))], 5, "sigmoid")   # classes 0..4
    got = dec.decode([logits, boxes], (100, 100), full=True)
    assert got["person"] > 0.99 and got["animal"] == 0.0 and got["vehicle"] == 0.0


def test_detr_preprocess_squashes_to_input_and_normalises():
    image_mod = pytest.importorskip("PIL.Image")
    img = image_mod.new("RGB", (128, 72), (255, 255, 255))
    tensor, ctx = _detr("sigmoid", size=64).preprocess(img)
    assert tensor.shape == (1, 3, 64, 64) and tensor.dtype == np.float32
    assert ctx == (128, 72)
    # white under ImageNet normalisation: (1 - mean) / std
    assert tensor[0, 0, 5, 5] == pytest.approx((1 - 0.485) / 0.229, abs=1e-3)
    plain = detectors.build_decoder(detectors.detector_config({
        "TAPO_SCORER_ARCH": "detr", "TAPO_SCORER_DETR_NORMALIZE": "none"}, 64))
    assert plain.preprocess(img)[0][0, 0, 5, 5] == pytest.approx(1.0, abs=1e-3)


def test_detr_preprocess_resize_does_not_antialias():
    # RF-DETR's own predict() resizes with torch F.resize(antialias=False) (cv2 INTER_LINEAR):
    # each output pixel is a 2x2 bilinear tap, so a thin bright line that a box filter
    # (PIL BILINEAR on a downscale) would smear to a faint grey must stay at full strength
    # where it is sampled. 8x8 -> 4x4: output pixel i samples source (2i + 0.5).
    image_mod = pytest.importorskip("PIL.Image")
    arr = np.zeros((8, 8, 3), np.uint8)
    arr[:, 1] = 255                      # column 1 is white, the rest black
    img = image_mod.fromarray(arr)
    plain = detectors.build_decoder(detectors.detector_config({
        "TAPO_SCORER_ARCH": "detr", "TAPO_SCORER_DETR_NORMALIZE": "none"}, 4))
    tensor = plain.preprocess(img)[0][0, 0, 0]
    # out x=0 samples src 0.5 -> 0.5*col0 + 0.5*col1; x=1 samples src 2.5 -> cols 2,3 = 0
    assert tensor[0] == pytest.approx(0.5, abs=1e-6)
    assert tensor[1] == pytest.approx(0.0, abs=1e-6)


def test_detr_preprocess_resize_matches_torch_reference():
    torch = pytest.importorskip("torch")
    image_mod = pytest.importorskip("PIL.Image")
    rng = np.random.default_rng(3)
    img = image_mod.fromarray(rng.integers(0, 256, (90, 160, 3), dtype=np.uint8))
    plain = detectors.build_decoder(detectors.detector_config({
        "TAPO_SCORER_ARCH": "detr", "TAPO_SCORER_DETR_NORMALIZE": "none"}, 48))
    got = plain.preprocess(img)[0]
    ref = torch.nn.functional.interpolate(
        torch.from_numpy(np.asarray(img).copy()).permute(2, 0, 1)[None].float() / 255.0,
        size=(48, 48), mode="bilinear", align_corners=False, antialias=False).numpy()
    assert np.abs(got - ref).max() < 1e-5


# ── through build_score_fn and the HTTP surface ─────────────────────────────

def _fake_session(monkeypatch, outputs):
    class Session:
        def get_inputs(self):
            return [types.SimpleNamespace(name="images")]

        def get_providers(self):
            return ["CPUExecutionProvider"]

        def run(self, _names, _feed):
            return outputs

    fake_ort = types.SimpleNamespace(InferenceSession=lambda *a, **k: Session())
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.delenv(scorer_service.ENV_PROVIDER, raising=False)


def _jpeg(size=(200, 100)):
    image_mod = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    image_mod.new("RGB", size).save(buf, format="JPEG")
    return buf.getvalue()


def test_score_fn_with_detr_decoder_end_to_end(monkeypatch):
    logits, boxes = _outputs([(0, (0.5, 0.5, 0.2, 0.4))], 80, "sigmoid")
    _fake_session(monkeypatch, (logits, boxes))
    dec = _detr("sigmoid")
    score_fn = scorer_service.build_score_fn("m.onnx", decoder=dec)
    assert score_fn.arch == "detr"
    for tiles in (1, 2):
        result = score_fn(_jpeg(), tiles)
        assert result["person"] > 0.99
        assert result["box"] == pytest.approx([80.0, 30.0, 120.0, 70.0], abs=0.01)
        assert result["person_boxes"][0] == pytest.approx([80.0, 30.0, 120.0, 70.0], abs=0.1)
        assert (result["w"], result["h"]) == (200, 100)
        assert {"animal", "vehicle", "classes", "person_scores", "vehicle_scores",
                "vehicle_boxes"} <= set(result)
        json.dumps(result)


def test_default_score_fn_is_yolox(monkeypatch):
    out = _yolox_out()
    _fake_session(monkeypatch, (out,))
    score_fn = scorer_service.build_score_fn("m.onnx", input_size=64)
    assert score_fn.arch == "yolox"
    assert score_fn(_jpeg((128, 72)), 1)["person"] == pytest.approx(0.90, abs=1e-4)


def test_health_and_metrics_name_the_architecture_not_the_model():
    srv = scorer_service.make_server(lambda b, t=1: {"person": 0.0, "animal": 0.0},
                                     port=0, arch="detr")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=5) as resp:
            health = json.load(resp)
        with urllib.request.urlopen(f"{base}/metrics", timeout=5) as resp:
            metrics = json.load(resp)
    finally:
        srv.shutdown()
    assert health == {"ok": True, "arch": "detr"}
    assert metrics["arch"] == "detr"
    assert "model" not in json.dumps(health) and ".onnx" not in json.dumps(metrics)


def test_main_exits_with_a_clear_message_on_bad_arch(monkeypatch, capsys):
    monkeypatch.setenv("TAPO_SCORER_ARCH", "yolov99")
    with pytest.raises(SystemExit) as exit_info:
        scorer_service.main(["--model", "m.onnx", "--port", "0"])
    assert exit_info.value.code != 0
    assert "TAPO_SCORER_ARCH" in capsys.readouterr().err
