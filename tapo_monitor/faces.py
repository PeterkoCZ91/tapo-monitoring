"""Face detection for the scoring service: OpenCV Zoo YuNet (2023mar) without OpenCV.

The follow-up photo of a passage is most useful when it shows a face, and the person box
alone cannot tell a frontal frame from one with the back or a cut-off head. This module
finds faces so the scorer can report them next to the person boxes (``faces`` in the
reply, asked for with ``?faces=1``); the daemon can then prefer a frame with a face.

Dependencies are the scorer's own (onnxruntime, numpy, Pillow), imported lazily so the
core package never needs them. Pre/post-processing mirrors cv::FaceDetectorYN:

* input is BGR float32 in 0..255, NCHW, no normalisation, zero-padded bottom/right to a
  multiple of 32 (no rescale);
* ``score = sqrt(clip(cls) * clip(obj))``; per stride ``s`` and grid cell ``(r, c)``
  ``cx = (c + dx) * s``, ``cy = (r + dy) * s``, ``w = exp(dw) * s``, ``h = exp(dh) * s``;
* greedy NMS at IoU 0.3.

Thresholds were set on a TC46 indoor test (2026-10-14, two people at 1-3 m, day): true
faces scored 0.77-0.94 at 53-170 px; the only false hits (a shoe rack, shelf clutter)
scored 0.65-0.73 or were under 20 px. Profiles, faces covered by a hand and heads cut by
the frame edge are not found — the detector is a preference signal, never a gate.
"""

import math

STRIDES = (8, 16, 32)
NMS_IOU = 0.3
DEFAULT_MIN_SCORE = 0.75
DEFAULT_MIN_SIZE = 20       # px, the shorter side of a face box in the original frame
TOP_K = 5000


# ── ONNX input shape patch ──────────────────────────────────────────────────
# The published model declares a fixed 1x3x640x640 input although the graph is
# size-agnostic; onnxruntime enforces declared dims, so the model bytes are patched in
# memory (input/output dims -> symbolic) by this minimal protobuf rewriter. The ``onnx``
# package is not needed.

def _read_varint(b, i):
    shift = val = 0
    while True:
        x = b[i]
        i += 1
        val |= (x & 0x7F) << shift
        if not x & 0x80:
            return val, i
        shift += 7


def _varint(v):
    out = bytearray()
    while True:
        x = v & 0x7F
        v >>= 7
        if v:
            out.append(x | 0x80)
        else:
            out.append(x)
            return bytes(out)


def _fields(b):
    """Yield ``(field_no, wire_type, raw_bytes_of_whole_field, payload_or_None)``."""
    i, n = 0, len(b)
    while i < n:
        start = i
        key, i = _read_varint(b, i)
        fno, wt = key >> 3, key & 7
        payload = None
        if wt == 0:
            _, i = _read_varint(b, i)
        elif wt == 1:
            i += 8
        elif wt == 2:
            ln, i = _read_varint(b, i)
            payload = b[i:i + ln]
            i += ln
        elif wt == 5:
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        yield fno, wt, b[start:i], payload


def _ld(fno, payload):
    return _varint((fno << 3) | 2) + _varint(len(payload)) + payload


def _rewrite(b, path, leaf):
    """Rewrite the length-delimited sub-messages along ``path`` (field numbers) with ``leaf``."""
    if not path:
        return leaf(b)
    out = bytearray()
    for fno, wt, raw, payload in _fields(b):
        if wt == 2 and fno == path[0]:
            out += _ld(fno, _rewrite(payload, path[1:], leaf))
        else:
            out += raw
    return bytes(out)


def _symbolic_dims(shape):
    """TensorShapeProto: every Dimension (field 1) but the batch becomes ``dim_param``."""
    out = bytearray()
    k = 0
    for fno, wt, raw, _payload in _fields(shape):
        if fno == 1 and wt == 2:
            out += raw if k == 0 else _ld(1, _ld(2, f"d{k}".encode()))
            k += 1
        else:
            out += raw
    return bytes(out)


def dynamic_model_bytes(data):
    """The model with symbolic input/output sizes (ModelProto.graph.input/output shapes)."""
    for io_field in (11, 12):
        data = _rewrite(data, (7, io_field, 2, 1, 2), _symbolic_dims)
    return data


# ── decoding (pure, numpy) ──────────────────────────────────────────────────

def nms(boxes, scores, iou_thr=NMS_IOU):
    """Indices kept by greedy non-maximum suppression, best score first."""
    import numpy as np
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = scores.argsort()[::-1][:TOP_K]
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        rest = order[1:]
        inter = (np.maximum(0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]))
                 * np.maximum(0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest])))
        iou = inter / np.maximum(areas[i] + areas[rest] - inter, 1e-9)
        order = rest[iou <= iou_thr]
    return keep


def decode(outputs, padded_w, padded_h, width, height, min_score=DEFAULT_MIN_SCORE,
           min_size=DEFAULT_MIN_SIZE):
    """Faces ``[[x1, y1, x2, y2, score], ...]`` (original px, best first) from model outputs.

    ``outputs`` maps the YuNet output names (``cls_8``, ``obj_8``, ``bbox_8``, ... for
    strides 8/16/32) to arrays; ``padded_*`` is the network input size, ``width``/``height``
    the original frame (boxes are clipped to it). A face whose shorter side is under
    ``min_size`` px is dropped.
    """
    import numpy as np
    all_boxes, all_scores = [], []
    for s in STRIDES:
        cols = padded_w // s
        cls = np.clip(outputs[f"cls_{s}"][0, :, 0], 0, 1)
        obj = np.clip(outputs[f"obj_{s}"][0, :, 0], 0, 1)
        score = np.sqrt(cls * obj)
        idx = np.nonzero(score >= min_score)[0]
        if not idx.size:
            continue
        r = (idx // cols).astype(np.float32)
        c = (idx % cols).astype(np.float32)
        bb = outputs[f"bbox_{s}"][0, idx]
        cx, cy = (c + bb[:, 0]) * s, (r + bb[:, 1]) * s
        bw, bh = np.exp(bb[:, 2]) * s, np.exp(bb[:, 3]) * s
        all_boxes.append(np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1))
        all_scores.append(score[idx])
    if not all_boxes:
        return []
    boxes = np.concatenate(all_boxes)
    scores = np.concatenate(all_scores)
    faces = []
    for i in nms(boxes, scores):
        x1, y1, x2, y2 = (float(v) for v in boxes[i])
        x1, y1 = max(0.0, x1), max(0.0, y1)
        x2, y2 = min(float(width), x2), min(float(height), y2)
        if min(x2 - x1, y2 - y1) < min_size:
            continue
        faces.append([round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1),
                      round(float(scores[i]), 4)])
    return faces


def faces_in_box(faces, box):
    """The faces whose centre lies inside the person ``box`` ``[x1, y1, x2, y2]``. Pure.

    Without a usable box every face counts: a frame-level face is still a face.
    """
    try:
        bx1, by1, bx2, by2 = (float(v) for v in box)
    except (TypeError, ValueError):
        return list(faces or [])
    kept = []
    for face in faces or []:
        try:
            x1, y1, x2, y2 = (float(v) for v in face[:4])
        except (TypeError, ValueError):
            continue
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        if bx1 <= cx <= bx2 and by1 <= cy <= by2 and math.isfinite(cx + cy):
            kept.append(face)
    return kept


# ── detector ────────────────────────────────────────────────────────────────

class FaceDetector:
    """YuNet on the CPU; ``detect(pil_image)`` -> faces as in :func:`decode`."""

    def __init__(self, model_path, min_score=DEFAULT_MIN_SCORE, min_size=DEFAULT_MIN_SIZE):
        import onnxruntime as ort
        with open(model_path, "rb") as f:
            data = dynamic_model_bytes(f.read())
        options = ort.SessionOptions()
        options.log_severity_level = 3
        self._session = ort.InferenceSession(data, options,
                                             providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name
        self._names = [o.name for o in self._session.get_outputs()]
        self.min_score = float(min_score)
        self.min_size = float(min_size)

    def detect(self, img):
        import numpy as np
        img = img.convert("RGB")
        w, h = img.size
        pw, ph = (w - 1) // 32 * 32 + 32, (h - 1) // 32 * 32 + 32
        blob = np.zeros((1, 3, ph, pw), np.float32)
        blob[0, :, :h, :w] = np.asarray(img, dtype=np.float32)[:, :, ::-1].transpose(2, 0, 1)
        outputs = dict(zip(self._names, self._session.run(None, {self._input: blob}),
                           strict=True))
        return decode(outputs, pw, ph, w, h, self.min_score, self.min_size)
