"""Detector families the scorer service can run, behind one small decoder interface.

The service owns HTTP, tiling, metrics and the response shape; a *decoder* owns the two
model-specific halves of scoring:

* ``preprocess(img) -> (tensor, ctx)``: PIL RGB image to the model's input tensor, plus an
  opaque ``ctx`` that ``decode`` needs to map boxes back to the image;
* ``decode(outputs, ctx, full) -> dict``: the raw ONNX outputs (a list of arrays) to the
  service's internal shape, with every box in **pixels of the image that was preprocessed**
  (the tile crop, when tiling): ``person``, ``animal``, ``vehicle``, ``classes``, ``box``
  (best person, or ``None``) and, when ``full`` is true, the parallel lists
  ``person_scores``/``person_boxes`` and ``vehicle_scores``/``vehicle_boxes``.

Families: ``yolox`` (default, the existing behaviour, implemented by the functions in
:mod:`tapo_monitor.scorer_service`) and ``detr`` (NMS-free DETR-style exports such as
RF-DETR, D-FINE and RT-DETR: ``logits`` [1,Q,C] and ``boxes`` [1,Q,4] normalised cxcywh).

Configuration is environment-driven and validated once at start (:func:`detector_config`):
an unknown value is a :class:`DetectorConfigError`, never a silent fall back to YOLOX, since
a service that quietly runs the wrong decoder reports confident nonsense.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from . import scorer_service as ss

ENV_ARCH = "TAPO_SCORER_ARCH"
ENV_INPUT_SIZE = "TAPO_SCORER_INPUT_SIZE"
ENV_DETR_PERSON_ID = "TAPO_SCORER_DETR_PERSON_ID"
ENV_DETR_ACTIVATION = "TAPO_SCORER_DETR_ACTIVATION"
ENV_DETR_NORMALIZE = "TAPO_SCORER_DETR_NORMALIZE"

ARCHS = ("yolox", "detr")
DEFAULT_INPUT_SIZE = {"yolox": 416, "detr": 640}
ACTIVATIONS = ("softmax", "sigmoid")
NORMALIZATIONS = ("imagenet", "none")

# COCO80 (contiguous, what the service names classes by) to COCO91 (original category ids,
# what many DETR exports index their logits by).
COCO91_IDS = (tuple(range(1, 12)) + tuple(range(13, 26)) + (27, 28) + tuple(range(31, 45))
              + tuple(range(46, 66)) + (67, 70) + tuple(range(72, 83)) + tuple(range(84, 91)))
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
BOX_FLOOR = 0.05


class DetectorConfigError(ValueError):
    """An environment value that names no supported detector setting."""


@dataclass(frozen=True)
class DetectorConfig:
    arch: str = "yolox"
    input_size: int = 416
    person_id: int = 0
    activation: str = "softmax"
    normalize: str = "imagenet"


def _env_text(env, name):
    return (env.get(name) or "").strip().lower()


def _choice(env, name, allowed, default):
    value = _env_text(env, name) or default
    if value not in allowed:
        raise DetectorConfigError(
            f"{name}={value!r} is not supported; use one of: {', '.join(allowed)}")
    return value


def _positive_int(text, name):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value <= 0:
        raise DetectorConfigError(f"{name}={text!r} must be a positive integer")
    return value


def detector_config(env=None, cli_input_size=None):
    """Validated :class:`DetectorConfig` from the environment. Pure.

    The input size comes from the explicit ``cli_input_size`` first (the unit file passes
    ``--input-size``), then ``TAPO_SCORER_INPUT_SIZE``, then the family's default.
    """
    env = os.environ if env is None else env
    arch = _choice(env, ENV_ARCH, ARCHS, "yolox")
    if cli_input_size is not None:
        size = _positive_int(str(cli_input_size), "--input-size")
    elif _env_text(env, ENV_INPUT_SIZE):
        size = _positive_int(_env_text(env, ENV_INPUT_SIZE), ENV_INPUT_SIZE)
    else:
        size = DEFAULT_INPUT_SIZE[arch]
    cfg = DetectorConfig(arch=arch, input_size=size)
    if arch != "detr":
        return cfg
    person_id = _choice(env, ENV_DETR_PERSON_ID, ("0", "1"), "0")
    return DetectorConfig(
        arch=arch, input_size=size, person_id=int(person_id),
        activation=_choice(env, ENV_DETR_ACTIVATION, ACTIVATIONS, "softmax"),
        normalize=_choice(env, ENV_DETR_NORMALIZE, NORMALIZATIONS, "imagenet"))


class YoloxDecoder:
    """YOLOX head output (``[1, anchors, 5+C]``, raw box head): the existing behaviour."""

    name = "yolox"

    def __init__(self, input_size=416):
        self.input_size = input_size

    def preprocess(self, img):
        return ss.preprocess_image(img, self.input_size)      # letterboxed BGR, ratio

    def decode(self, outputs, ratio, full=True):
        output = outputs[0]
        scores = ss.scores_from_output(output)
        box = ss.best_person_box(output, self.input_size)
        result = {"person": scores["person"], "animal": scores["animal"],
                  "vehicle": scores["vehicle"], "classes": scores["classes"],
                  "box": ss.scale_box(box, ratio, 0, 0) if box else None}
        if full:
            found, found_boxes = ss.person_scores(output, self.input_size, with_boxes=True)
            result["person_scores"] = found
            result["person_boxes"] = [ss.scale_box(b, ratio, 0, 0) for b in found_boxes]
            v_found, v_boxes = ss.vehicle_scores(output, self.input_size, with_boxes=True)
            result["vehicle_scores"] = v_found
            result["vehicle_boxes"] = [ss.scale_box(b, ratio, 0, 0) for b in v_boxes]
        return result


class DetrDecoder:
    """NMS-free DETR-style head: ``logits`` [1,Q,C] and ``boxes`` [1,Q,4] (norm. cxcywh).

    Each of the Q queries is one candidate detection, so there is no NMS: the per-person
    list is simply the queries over the floor, best first. ``activation`` is ``softmax``
    (classic DETR/RT-DETR-softmax heads, whose last column is "no object" and is never
    read) or ``sigmoid`` (RF-DETR, D-FINE, RT-DETR focal-loss heads). ``person_id`` picks
    the label space: 0 = COCO80, 1 = COCO91 (person = 1, gaps in the id list). Columns the
    export does not have count as 0 rather than failing.
    """

    name = "detr"

    def __init__(self, input_size=640, person_id=0, activation="softmax",
                 normalize="imagenet"):
        self.input_size = input_size
        self.activation = activation
        self.normalize = normalize
        self.columns = COCO91_IDS if person_id == 1 else tuple(range(80))

    def preprocess(self, img):
        import numpy as np
        from PIL import Image

        size = self.input_size
        # DETR-family models are trained on a plain (aspect-squashing) resize, so the
        # normalised boxes map back to the original frame with no letterbox offset.
        arr = np.asarray(img.resize((size, size), Image.BILINEAR),
                         dtype=np.float32) / 255.0
        if self.normalize == "imagenet":
            arr = (arr - np.asarray(IMAGENET_MEAN, np.float32)) / np.asarray(
                IMAGENET_STD, np.float32)
        return arr.transpose(2, 0, 1)[None].astype(np.float32), (img.width, img.height)

    @staticmethod
    def _split(outputs):
        import numpy as np

        arrays = [np.asarray(o) for o in outputs]
        boxes = [a for a in arrays if a.ndim == 3 and a.shape[-1] == 4]
        logits = [a for a in arrays if a.ndim == 3 and a.shape[-1] != 4]
        if len(boxes) != 1 or len(logits) != 1:
            raise ValueError(
                "DETR output needs one [1,Q,C] logits and one [1,Q,4] boxes array, got "
                f"shapes {[a.shape for a in arrays]}")
        if logits[0].shape[0] < 1 or logits[0].shape[:2] != boxes[0].shape[:2]:
            raise ValueError(f"DETR logits {logits[0].shape} and boxes {boxes[0].shape} "
                             "disagree on batch/query count")
        return logits[0][0].astype(np.float64), boxes[0][0].astype(np.float64)

    def _probabilities(self, logits):
        """(Q, 80) class probabilities in COCO80 order; non-finite becomes 0. Pure."""
        import numpy as np

        with np.errstate(all="ignore"):
            if self.activation == "softmax":
                shifted = logits - logits.max(axis=1, keepdims=True)
                exp = np.exp(shifted)
                probs = exp / exp.sum(axis=1, keepdims=True)
            else:
                probs = 1.0 / (1.0 + np.exp(-logits))
        probs = np.clip(np.nan_to_num(probs, nan=0.0, posinf=0.0, neginf=0.0), 0.0, 1.0)
        out = np.zeros((len(probs), 80))
        for index, column in enumerate(self.columns):
            if column < probs.shape[1]:
                out[:, index] = probs[:, column]
        return out

    @staticmethod
    def _pixel_boxes(boxes, width, height):
        """xyxy pixel boxes and a finite-row mask. Pure."""
        import numpy as np

        cx, cy, w, h = (boxes[:, k] for k in range(4))
        xyxy = np.stack(((cx - w / 2) * width, (cy - h / 2) * height,
                         (cx + w / 2) * width, (cy + h / 2) * height), axis=1)
        finite = np.isfinite(xyxy).all(axis=1)
        xyxy = np.nan_to_num(xyxy)
        xyxy[:, 0::2] = np.clip(xyxy[:, 0::2], 0, width)
        xyxy[:, 1::2] = np.clip(xyxy[:, 1::2], 0, height)
        return xyxy, finite

    @staticmethod
    def _ranked(conf, xyxy, finite, floor, limit):
        import numpy as np

        keep = np.flatnonzero((conf >= floor) & finite)
        keep = keep[np.argsort(-conf[keep], kind="stable")][:limit]
        return ([round(float(conf[i]), 4) for i in keep],
                [tuple(float(v) for v in xyxy[i]) for i in keep])

    def decode(self, outputs, ctx, full=True):
        import numpy as np

        width, height = ctx
        logits, boxes = self._split(outputs)
        probs = self._probabilities(logits)
        xyxy, finite = self._pixel_boxes(boxes, width, height)
        person = probs[:, ss.PERSON_CLASS]
        if not len(probs):
            per_class = np.zeros(80)
            animal = vehicle = 0.0
        else:
            per_class = probs.max(axis=0)
            animal = float(probs[:, list(ss.ANIMAL_CLASSES)].max())
            vehicle = float(probs[:, list(ss.VEHICLE_CLASSES)].max())
        _, best = self._ranked(person, xyxy, finite, BOX_FLOOR, 1)
        result = {
            "person": round(float(person.max()) if len(person) else 0.0, 4),
            "animal": round(animal, 4), "vehicle": round(vehicle, 4),
            "classes": {ss.COCO_NAMES[i]: round(float(s), 4)
                        for i, s in enumerate(per_class) if float(s) >= 0.01},
            "box": best[0] if best else None,
        }
        if full:
            vehicle_conf = (probs[:, list(ss.VEHICLE_CLASSES)].max(axis=1)
                            if len(probs) else person)
            for key, conf in (("person", person), ("vehicle", vehicle_conf)):
                found, found_boxes = self._ranked(
                    conf, xyxy, finite, ss.PERSON_LIST_FLOOR, ss.PERSON_LIST_LIMIT)
                result[f"{key}_scores"] = found
                result[f"{key}_boxes"] = found_boxes
        return result


def build_decoder(cfg):
    """The decoder object for a validated :class:`DetectorConfig`."""
    if cfg.arch == "detr":
        return DetrDecoder(cfg.input_size, cfg.person_id, cfg.activation, cfg.normalize)
    return YoloxDecoder(cfg.input_size)
