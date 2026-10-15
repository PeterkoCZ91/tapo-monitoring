import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import faces

np = pytest.importorskip("numpy")  # decoding needs numpy; the core package does not


def test_varint_roundtrip():
    for v in (0, 1, 127, 128, 300, 2**21 + 5):
        enc = faces._varint(v)
        assert faces._read_varint(enc, 0) == (v, len(enc))


def test_symbolic_dims_keeps_the_batch_and_names_the_rest():
    dim = lambda n: faces._ld(1, faces._varint(1 << 3) + faces._varint(n))  # dim_value  # noqa: E731
    shape = dim(1) + dim(3) + dim(640) + dim(640)
    out = list(faces._fields(faces._symbolic_dims(shape)))
    assert len(out) == 4
    assert out[0][2] == dim(1)
    assert [p for _f, _w, _r, p in out[1:]] == [faces._ld(2, f"d{k}".encode())
                                               for k in (1, 2, 3)]


def _outputs(w, h, hits):
    """YuNet-shaped outputs, zero everywhere but ``hits``: (stride, row, col, score, box)."""
    out = {}
    for s in faces.STRIDES:
        n = (w // s) * (h // s)
        out[f"cls_{s}"] = np.zeros((1, n, 1), np.float32)
        out[f"obj_{s}"] = np.zeros((1, n, 1), np.float32)
        out[f"bbox_{s}"] = np.zeros((1, n, 4), np.float32)
    for s, r, c, score, (dx, dy, dw, dh) in hits:
        i = r * (w // s) + c
        out[f"cls_{s}"][0, i, 0] = score
        out[f"obj_{s}"][0, i, 0] = score
        out[f"bbox_{s}"][0, i] = (dx, dy, dw, dh)
    return out


def test_decode_places_a_face_in_original_pixels():
    # stride 16, cell (row 2, col 3), centre offset (0.5, 0.5), size exp(1)*16 ~ 43.5 px
    out = _outputs(320, 192, [(16, 2, 3, 0.9, (0.5, 0.5, 1.0, 1.0))])
    (face,) = faces.decode(out, 320, 192, 300, 180, min_score=0.5, min_size=0)
    cx, cy, side = 3.5 * 16, 2.5 * 16, np.exp(1.0) * 16
    assert face[:4] == pytest.approx([cx - side / 2, cy - side / 2, cx + side / 2,
                                      cy + side / 2], abs=0.1)
    assert face[4] == pytest.approx(0.9, abs=1e-3)       # sqrt(cls * obj)


def test_decode_drops_low_scores_small_faces_and_duplicates():
    out = _outputs(320, 192, [
        (16, 2, 3, 0.9, (0.5, 0.5, 1.0, 1.0)),
        (16, 2, 3 + 0, 0.9, (0.5, 0.5, 1.0, 1.0)),   # same cell: written once
        (8, 4, 7, 0.85, (0.5, 0.5, 1.5, 1.5)),       # overlaps the first -> NMS
        (8, 20, 30, 0.95, (0.5, 0.5, 0.0, 0.0)),     # 8 px face -> min_size
        (32, 1, 1, 0.5, (0.5, 0.5, 0.5, 0.5)),       # below min_score
    ])
    found = faces.decode(out, 320, 192, 320, 192, min_score=0.75, min_size=20)
    assert len(found) == 1 and found[0][4] == pytest.approx(0.9, abs=1e-3)
    assert faces.decode(_outputs(320, 192, []), 320, 192, 320, 192) == []


def test_faces_in_box_keeps_faces_centred_on_the_person():
    found = [[100, 10, 140, 50, 0.9], [400, 10, 440, 50, 0.8]]
    assert faces.faces_in_box(found, [80, 0, 200, 300]) == [found[0]]
    assert faces.faces_in_box(found, None) == found        # no box: every face counts
    assert faces.faces_in_box(None, [0, 0, 10, 10]) == []
    assert faces.faces_in_box([["x", 1, 2, 3]], [0, 0, 10, 10]) == []


@pytest.mark.parametrize("threads", [3, None])
def test_face_detector_honours_the_inference_thread_budget(monkeypatch, tmp_path, threads):
    import types

    calls = []

    def session(_data, options, **_kwargs):
        calls.append(options)
        return types.SimpleNamespace(get_inputs=lambda: [types.SimpleNamespace(name="in")],
                                     get_outputs=lambda: [])

    monkeypatch.setitem(sys.modules, "onnxruntime", types.SimpleNamespace(
        InferenceSession=session, SessionOptions=types.SimpleNamespace))
    monkeypatch.setattr(faces, "dynamic_model_bytes", lambda data: data)
    model = tmp_path / "face.onnx"
    model.write_bytes(b"model")

    faces.FaceDetector(str(model), threads=threads)

    if threads:
        assert calls[0].intra_op_num_threads == 3
    else:
        assert not hasattr(calls[0], "intra_op_num_threads")
