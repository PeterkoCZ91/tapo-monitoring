"""Let a larger teacher model label the frames it agrees on, so a person only sees the rest.

Nobody labels 800 frames by hand. A bigger detector than the production scorer (e.g.
YOLOX-x at 640) scores every collected frame once; where it and the production score
agree with confidence, the frame is labeled automatically (``"by": "auto:<model>"`` in
``labels.jsonl``); where they disagree, or the frame sits in the gray zone, it is left for
the labeling page, which then shows the biggest disagreements first. Those are exactly
the frames where one of the two models is wrong.

Agreement is not ground truth — two detectors can share a blind spot, which is why the
labels say who made them and a human label always wins. Teacher scores are cached in
``teacher.jsonl`` by image hash, so a rerun only scores new frames.

Runs on the operator's machine with a capped thread count at low priority; the
production scorer on the same host must keep answering.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import labeling

AGREE_HIGH = 0.65       # both at or above: a person
AGREE_LOW = 0.20        # both below: nobody (production side capped at the gray zone)
ALONE_HIGH = 0.80       # no production score: the teacher alone must be this sure
ALONE_LOW = 0.10


def decide(production, teacher):
    """``"person"``, ``"no_person"`` or None (ask a human). Pure."""
    if production is None:
        if teacher >= ALONE_HIGH:
            return "person"
        if teacher <= ALONE_LOW:
            return "no_person"
        return None
    if production >= AGREE_HIGH and teacher >= AGREE_HIGH:
        return "person"
    if production < labeling.GRAY_LOW and teacher < AGREE_LOW:
        return "no_person"
    return None


def _append(path, record):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(dataset_dir, score_fn, *, model, now=None, progress=None):
    """Score new frames with ``score_fn(jpeg_bytes) -> float`` and auto-label agreements.

    Returns counts: frames scored now, taken from the cache, labeled person / no_person
    automatically this run, and left for a human. Never touches an existing label.
    """
    now = time.time() if now is None else now
    frames = labeling.load_frames(dataset_dir)
    labeled = labeling.latest_labels(dataset_dir)
    cache = labeling.teacher_scores(dataset_dir)
    teacher_path = os.path.join(dataset_dir, labeling.TEACHER_NAME)
    labels_path = os.path.join(dataset_dir, labeling.LABELS_NAME)
    summary = {"scored": 0, "cached": 0, "auto_person": 0, "auto_no_person": 0,
               "for_human": 0}
    for index, frame in enumerate(frames, 1):
        if frame.sha256 in cache:
            summary["cached"] += 1
            teacher = cache[frame.sha256]
        else:
            try:
                with open(os.path.join(dataset_dir, *frame.path.split("/")), "rb") as fh:
                    teacher = float(score_fn(fh.read()))
            except OSError:
                continue
            summary["scored"] += 1
            cache[frame.sha256] = teacher
            _append(teacher_path, {"sha256": frame.sha256, "path": frame.path,
                                   "model": model, "person": round(teacher, 4),
                                   "scored_at": now})
        if progress is not None and index % 50 == 0:
            progress(index, len(frames))
        if frame.sha256 in labeled:
            continue
        label = decide(frame.score, teacher)
        if label is None:
            summary["for_human"] += 1
            continue
        record = {"path": frame.path, "sha256": frame.sha256, "label": label,
                  "labeled_at": float(now), "score": frame.score, "camera": frame.camera,
                  "source": frame.source, "by": f"auto:{model}",
                  "teacher": round(teacher, 4)}
        _append(labels_path, record)
        labeled[frame.sha256] = record
        summary["auto_" + label] += 1
    return summary


def load_teacher(model_path, input_size=640, threads=2):  # pragma: no cover - needs ORT
    """``score_fn(jpeg_bytes) -> person`` over a YOLOX ONNX model, CPU, capped threads."""
    import io

    import onnxruntime as ort
    from PIL import Image

    from . import scorer_service

    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, int(threads))
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(model_path, options, providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    def score(jpeg_bytes):
        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        tensor, _ratio = scorer_service.preprocess_image(img, input_size)
        (output,) = session.run(None, {input_name: tensor})
        return scorer_service.scores_from_output(output)["person"]

    return score


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tapo-monitor autolabel",
        description="Auto-label collected frames a larger teacher model agrees on")
    parser.add_argument("dataset_dir")
    parser.add_argument("--model", required=True, help="teacher YOLOX ONNX model")
    parser.add_argument("--input-size", type=int, default=640)
    parser.add_argument("--threads", type=int, default=2,
                        help="CPU threads (default 2, so a scorer on this host keeps up)")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.dataset_dir):
        print(f"{parser.prog}: {args.dataset_dir} is not a directory", file=sys.stderr)
        return 2
    if not os.path.isfile(args.model):
        print(f"{parser.prog}: model {args.model} not found", file=sys.stderr)
        return 2
    model = os.path.splitext(os.path.basename(args.model))[0]
    score_fn = load_teacher(args.model, args.input_size, args.threads)
    summary = run(args.dataset_dir, score_fn, model=model,
                  progress=lambda i, n: print(f"  {i}/{n}", flush=True))
    print(f"teacher {model}: scored {summary['scored']} new, {summary['cached']} cached; "
          f"auto-labeled {summary['auto_person']} person, {summary['auto_no_person']} "
          f"no person; {summary['for_human']} left for the labeling page")
    return 0
