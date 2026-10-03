#!/usr/bin/env python3
"""Score the frames of a manifest with ONE detector and append the person score per frame.

    python3 run.py MODEL --bench-dir DIR --dataset DATASET_DIR [--limit N]

MODEL is one of
  scorer             a running tapo-monitor scorer service (--scorer-url, default
                     http://127.0.0.1:8766/score; a bearer token is read from the variable
                     named by --token-env). Use it for the production baseline.
  yolox_s|m|x        a local YOLOX ONNX file <--yolox-dir>/<name>.onnx, 640 letterbox,
                     objectness x class, like the scorer service
  rfdetr_nano|small|medium|base|large     the ``rfdetr`` package (COCO class 1 = person)
  tv_frcnn           torchvision Faster R-CNN ResNet-50 FPN v2 (COCO weights, downloaded)
  hf:<repo id>       any HuggingFace object detector (DETR family: DETR, RT-DETR, D-FINE ...)
                     through AutoModelForObjectDetection; the class named "person" is used

Output: DIR/scores/<name>.jsonl, one {"sha", "person", "sec"} per unique frame, resumable.
The person score is the highest confidence of any person detection (floor 0.02). Run one
model at a time, under ``nice``. Manual frames are scored first so a --limit run still
covers the labels that matter.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

FLOOR = 0.02
ORDER = {"manual": 0, "none": 1, "auto": 2}


def unique_frames(rows):
    """One row per sha256, manual frames first, then unlabeled, then automatic."""
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: ORDER.get(r.get("kind"), 3)):
        if r["sha"] not in seen:
            seen.add(r["sha"])
            out.append(r)
    return out


def safe_name(model):
    return model.replace(":", "_").replace("/", "_")


def letterbox(img, size):
    """YOLOX preprocessing: scale to fit, paste top-left on a 114-grey square, BGR, CHW."""
    import numpy as np
    from PIL import Image
    ratio = min(size / img.width, size / img.height)
    resized = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))), Image.BILINEAR)
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    canvas.paste(resized, (0, 0))
    arr = np.asarray(canvas, dtype=np.float32)[:, :, ::-1]
    return arr.transpose(2, 0, 1)[None].copy()


def best(scores):
    scores = list(scores)
    return max(scores) if scores else 0.0


def make_scorer(args):
    token = os.environ.get(args.token_env) if args.token_env else None

    def score(path):
        with open(path, "rb") as f:
            body = f.read()
        headers = {"Content-Type": "image/jpeg"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(args.scorer_url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return float(json.load(resp)["person"])
    return score


def make_yolox(args):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = args.threads
    opts.inter_op_num_threads = 1
    sess = ort.InferenceSession(os.path.join(os.path.expanduser(args.yolox_dir), args.model + ".onnx"),
                                opts, providers=["CPUExecutionProvider"])
    name = sess.get_inputs()[0].name

    def score(path):
        from PIL import Image
        out = sess.run(None, {name: letterbox(Image.open(path).convert("RGB"), 640)})[0][0]
        return float((out[:, 4] * out[:, 5]).max())    # person is class 0; sigmoid already applied
    return score


def make_rfdetr(args):
    import rfdetr
    import torch
    torch.set_num_threads(args.threads)
    size = args.model.split("_", 1)[1]
    cls = {"nano": "RFDETRNano", "small": "RFDETRSmall", "medium": "RFDETRMedium",
           "base": "RFDETRBase", "large": "RFDETRLarge"}[size]
    model = getattr(rfdetr, cls)()

    def score(path):
        from PIL import Image
        det = model.predict(Image.open(path).convert("RGB"), threshold=FLOOR)
        return best(float(c) for c, k in zip(det.confidence, det.class_id, strict=False) if int(k) == 1)
    return score


def make_hf(args):
    import torch
    from transformers import AutoImageProcessor, AutoModelForObjectDetection
    torch.set_num_threads(args.threads)
    repo = args.model[3:]
    proc = AutoImageProcessor.from_pretrained(repo)
    model = AutoModelForObjectDetection.from_pretrained(repo).eval()
    person = [k for k, v in model.config.id2label.items() if v == "person"][0]

    def score(path):
        from PIL import Image
        img = Image.open(path).convert("RGB")
        with torch.no_grad():
            out = model(**proc(images=img, return_tensors="pt"))
        res = proc.post_process_object_detection(out, threshold=FLOOR, target_sizes=[img.size[::-1]])[0]
        return best(float(s) for s, k in zip(res["scores"], res["labels"], strict=False) if int(k) == person)
    return score


def make_frcnn(args):
    import torch
    import torchvision.transforms.functional as tf
    from torchvision.models.detection import FasterRCNN_ResNet50_FPN_V2_Weights, fasterrcnn_resnet50_fpn_v2
    torch.set_num_threads(args.threads)
    model = fasterrcnn_resnet50_fpn_v2(weights=FasterRCNN_ResNet50_FPN_V2_Weights.COCO_V1,
                                       box_score_thresh=FLOOR).eval()

    def score(path):
        from PIL import Image
        with torch.no_grad():
            out = model([tf.to_tensor(Image.open(path).convert("RGB"))])[0]
        return best(float(s) for s, k in zip(out["scores"], out["labels"], strict=False) if int(k) == 1)
    return score


def make_model(args):
    m = args.model
    if m == "scorer":
        return make_scorer(args)
    if m in ("yolox_s", "yolox_m", "yolox_x"):
        return make_yolox(args)
    if m.startswith("rfdetr_"):
        return make_rfdetr(args)
    if m.startswith("hf:"):
        return make_hf(args)
    if m == "tv_frcnn":
        return make_frcnn(args)
    raise SystemExit("unknown model: " + m)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model")
    ap.add_argument("--bench-dir", default=".", help="holds manifest.json, receives scores/")
    ap.add_argument("--dataset", required=True, help="dataset directory the manifest paths are relative to")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--yolox-dir", default="weights")
    ap.add_argument("--scorer-url", default="http://127.0.0.1:8766/score")
    ap.add_argument("--token-env", help="name of the variable holding the scorer's bearer token")
    args = ap.parse_args(argv)
    os.environ.setdefault("OMP_NUM_THREADS", str(args.threads))
    bench = os.path.expanduser(args.bench_dir)
    dataset = os.path.expanduser(args.dataset)
    with open(os.path.join(bench, "manifest.json"), encoding="utf-8") as f:
        frames = unique_frames(json.load(f))
    os.makedirs(os.path.join(bench, "scores"), exist_ok=True)
    out = os.path.join(bench, "scores", safe_name(args.model) + ".jsonl")
    done = set()
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            done = {json.loads(line)["sha"] for line in f if line.strip()}
    score = make_model(args)
    n, t0 = 0, time.time()
    with open(out, "a", encoding="utf-8") as fo:
        for r in frames:
            if r["sha"] in done:
                continue
            if args.limit is not None and n >= args.limit:
                break
            t = time.time()
            s = score(os.path.join(dataset, r["path"]))
            fo.write(json.dumps({"sha": r["sha"], "person": round(s, 4), "sec": round(time.time() - t, 3)}) + "\n")
            fo.flush()
            n += 1
            if n % 200 == 0:
                print(args.model, n, round((time.time() - t0) / n, 2), "s/img", file=sys.stderr, flush=True)
    print("done", args.model, n, "new of", len(frames))


if __name__ == "__main__":
    main()
