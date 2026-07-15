"""Tiny HTTP scoring service: POST a JPEG, get person/animal confidence back.

Runs a YOLOX ONNX model (Apache-2.0, e.g. yolox_tiny.onnx) on the local CPU and
answers ``{"person": 0.87, "animal": 0.0}`` — the daemon compares that to a config
threshold instead of asking a vision LLM whether the scene is empty. Heavy deps
(onnxruntime / numpy / Pillow) are imported lazily so the core package never needs
them; install with ``pip install tapo-monitor[scorer]``.

Model assets: https://github.com/Megvii-BaseDetection/YOLOX/releases (yolox_tiny.onnx,
input 416). The exported model applies sigmoid to objectness/class scores internally,
so confidence is simply objectness * class_score; box decoding is not needed for the
yes/no decision and is skipped entirely.
"""

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer

log = logging.getLogger(__name__)

PERSON_CLASS = 0
# COCO ids: bird, cat, dog, horse, sheep, cow, bear
ANIMAL_CLASSES = (14, 15, 16, 17, 18, 19, 21)
COCO_NAMES = (
    "person", "bicycle", "car", "motorbike", "aeroplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "sofa",
    "pottedplant", "bed", "diningtable", "toilet", "tvmonitor", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
)


def scores_from_output(output, num_classes=80, floor=0.01):
    """Max person/animal and per-class confidence from a raw YOLOX head output. Pure."""
    preds = output[0]                      # (N, 5+C)
    if not len(preds):
        return {"person": 0.0, "animal": 0.0, "classes": {}}
    obj = preds[:, 4]
    cls = preds[:, 5:5 + num_classes]
    conf = obj[:, None] * cls              # (N, C)
    per_class = conf.max(axis=0)
    person = float(per_class[PERSON_CLASS])
    animal = float(conf[:, list(ANIMAL_CLASSES)].max())
    named = {
        COCO_NAMES[i]: round(float(score), 4)
        for i, score in enumerate(per_class[:min(num_classes, len(COCO_NAMES))])
        if float(score) >= floor
    }
    return {"person": round(person, 4), "animal": round(animal, 4), "classes": named}


def _grids_and_strides(input_size, strides):
    """YOLOX anchor grid centres and per-anchor stride for a square input. Pure (numpy)."""
    import numpy as np

    grids, expanded = [], []
    for s in strides:
        hw = input_size // s
        xv, yv = np.meshgrid(np.arange(hw), np.arange(hw))
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        expanded.append(np.full((hw * hw, 1), s))
    return np.concatenate(grids, 0), np.concatenate(expanded, 0)


def best_person_box(output, input_size=640, strides=(8, 16, 32), floor=0.05):
    """Input-coord xyxy box of the highest-confidence person anchor, or None.

    This YOLOX export applies sigmoid to obj/class but leaves the box head **raw**, so the
    anchor's ``(x, y, w, h)`` must be decoded against its grid cell and stride:
    ``xy = (raw_xy + grid) * stride``, ``wh = exp(raw_wh) * stride`` (input pixels). We pick
    the anchor with the top person confidence (obj * person class); ``None`` when it fails
    ``floor``. If the anchor count doesn't match the grid (a different/pre-decoded export),
    fall back to treating the head as already-decoded ``cx,cy,w,h``. Pure.
    """
    import numpy as np

    preds = output[0]
    if not len(preds):
        return None
    conf = preds[:, 4] * preds[:, 5 + PERSON_CLASS]
    i = int(conf.argmax())
    if float(conf[i]) < floor:
        return None
    grids, expanded = _grids_and_strides(input_size, strides)
    if len(grids) == len(preds):
        s = float(expanded[i, 0])
        cx = (float(preds[i, 0]) + float(grids[i, 0])) * s
        cy = (float(preds[i, 1]) + float(grids[i, 1])) * s
        w = float(np.exp(preds[i, 2])) * s
        h = float(np.exp(preds[i, 3])) * s
    else:
        cx, cy, w, h = (float(v) for v in preds[i, :4])
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def tile_rects(width, height, tiles, overlap=0.15):
    """Original-coord ``(x0, y0, x1, y1)`` rects: the whole frame plus a tiles×tiles grid.

    Scoring each grid cell separately lets a distant person — a few pixels once the whole
    frame is letterboxed to the model's small input — fill a cell and register. The whole
    frame stays in the list so a close, large subject is still caught. ``overlap`` widens
    each cell so a subject on a seam isn't split. Pure.
    """
    rects = [(0.0, 0.0, float(width), float(height))]
    n = int(tiles)
    if n <= 1:
        return rects
    tw, th = width / n, height / n
    ox, oy = tw * overlap, th * overlap
    for j in range(n):
        for i in range(n):
            x0 = max(0.0, i * tw - ox)
            y0 = max(0.0, j * th - oy)
            x1 = min(float(width), (i + 1) * tw + ox)
            y1 = min(float(height), (j + 1) * th + oy)
            rects.append((x0, y0, x1, y1))
    return rects


def scale_box(box, ratio, off_x, off_y):
    """Map a tile-input-coord xyxy box to full-image original coords. Pure.

    ``ratio`` is the letterbox scale used for that tile (original->input); dividing undoes
    it, then the tile's top-left ``(off_x, off_y)`` offset places it in the full frame.
    """
    x1, y1, x2, y2 = box
    return (x1 / ratio + off_x, y1 / ratio + off_y, x2 / ratio + off_x, y2 / ratio + off_y)


def preprocess_image(img, input_size):
    """PIL RGB image -> (1,3,S,S) float32 letterboxed BGR array + used ratio."""
    import numpy as np
    from PIL import Image

    ratio = min(input_size / img.width, input_size / img.height)
    resized = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                         Image.BILINEAR)
    canvas = Image.new("RGB", (input_size, input_size), (114, 114, 114))
    canvas.paste(resized, (0, 0))
    arr = np.asarray(canvas, dtype=np.float32)[:, :, ::-1]   # RGB -> BGR (YOLOX convention)
    return arr.transpose(2, 0, 1)[None].copy(), ratio


def preprocess(jpeg_bytes, input_size):
    """JPEG bytes -> (1,3,S,S) float32 letterboxed BGR array + used ratio."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    return preprocess_image(img, input_size)


def build_score_fn(model_path, input_size=416):
    """Load the ONNX model once and return score_fn(jpeg_bytes, tiles=1) -> dict.

    ``tiles > 1`` scores the whole frame plus a tiles×tiles grid (see :func:`tile_rects`)
    and keeps the highest person score — the way distant subjects get rescued. The
    response gains a ``box`` (full-image xyxy in original pixels) for the winning person
    detection, so a caller can crop/zoom to it; ``box`` is ``None`` when no person.
    """
    import io

    import onnxruntime as ort
    from PIL import Image

    session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    def _run(img):
        tensor, ratio = preprocess_image(img, input_size)
        (output,) = session.run(None, {input_name: tensor})
        return output, ratio

    def score_fn(jpeg_bytes, tiles=1):
        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        rects = tile_rects(img.width, img.height, tiles)
        best = {"person": 0.0, "animal": 0.0, "classes": {}, "box": None,
                "w": img.width, "h": img.height}
        for (x0, y0, x1, y1) in rects:
            crop = img if len(rects) == 1 else img.crop((int(x0), int(y0), int(x1), int(y1)))
            output, ratio = _run(crop)
            scores = scores_from_output(output)
            if scores["animal"] > best["animal"]:
                best["animal"] = scores["animal"]
            if scores["person"] >= best["person"]:
                best["person"] = scores["person"]
                best["classes"] = scores["classes"]
                box = best_person_box(output, input_size)
                best["box"] = list(scale_box(box, ratio, x0, y0)) if box else None
        return best

    return score_fn


def make_server(score_fn, port=8765):
    """HTTP server: POST /score (JPEG body) -> JSON scores; GET /health -> ok."""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._reply(200, {"ok": True})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(self.path)
            if parsed.path != "/score":
                self._reply(404, {"error": "not found"})
                return
            try:
                tiles = max(1, int(parse_qs(parsed.query).get("tiles", ["1"])[0]))
            except (TypeError, ValueError):
                tiles = 1
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                self._reply(200, score_fn(body, tiles))
            except Exception as e:  # noqa: BLE001 - report, don't kill the server
                log.warning("scoring failed: %s", e)
                self._reply(500, {"error": str(e)})

        def log_message(self, fmt, *args):  # route http.server chatter to logging
            log.debug(fmt, *args)

    return HTTPServer(("0.0.0.0", port), Handler)


def main(argv=None):  # pragma: no cover - thin entry point, needs model weights
    parser = argparse.ArgumentParser(description="tapo-monitor scoring service")
    parser.add_argument("--model", required=True, help="path to YOLOX .onnx weights")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--input-size", type=int, default=416)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    score_fn = build_score_fn(args.model, args.input_size)
    server = make_server(score_fn, port=args.port)
    log.info("scorer listening on :%d (model=%s, input=%d)",
             args.port, args.model, args.input_size)
    server.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
