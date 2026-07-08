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


def scores_from_output(output, num_classes=80):
    """Max person/animal confidence from a raw YOLOX head output (1, N, 5+C). Pure."""
    preds = output[0]                      # (N, 5+C)
    obj = preds[:, 4]
    cls = preds[:, 5:5 + num_classes]
    conf = obj[:, None] * cls              # (N, C)
    person = float(conf[:, PERSON_CLASS].max()) if len(preds) else 0.0
    animal = float(conf[:, list(ANIMAL_CLASSES)].max()) if len(preds) else 0.0
    return {"person": round(person, 4), "animal": round(animal, 4)}


def preprocess(jpeg_bytes, input_size):
    """JPEG bytes -> (1,3,S,S) float32 letterboxed BGR array + used ratio."""
    import io

    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    ratio = min(input_size / img.width, input_size / img.height)
    resized = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                         Image.BILINEAR)
    canvas = Image.new("RGB", (input_size, input_size), (114, 114, 114))
    canvas.paste(resized, (0, 0))
    arr = np.asarray(canvas, dtype=np.float32)[:, :, ::-1]   # RGB -> BGR (YOLOX convention)
    return arr.transpose(2, 0, 1)[None].copy(), ratio


def build_score_fn(model_path, input_size=416):
    """Load the ONNX model once and return score_fn(jpeg_bytes) -> dict."""
    import onnxruntime as ort

    session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    def score_fn(jpeg_bytes):
        tensor, _ratio = preprocess(jpeg_bytes, input_size)
        (output,) = session.run(None, {input_name: tensor})
        return scores_from_output(output)

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
            if self.path != "/score":
                self._reply(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                self._reply(200, score_fn(body))
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
