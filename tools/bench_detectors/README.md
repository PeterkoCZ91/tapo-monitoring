# Detector benchmark

Compare person detectors on **your own hand-labeled frames** before you change the model
behind the scoring service. The results in [Measurements](../../docs/measurements.md#detector-comparison)
come from this tool. Nothing here talks to a camera or to the monitor; it reads a dataset
directory and writes score files next to it.

Files:

- `manifest.py` lists the dataset's JPEGs with their label kind and, optionally, solar elevation.
- `run.py` scores every frame with one model and appends `{sha, person, sec}` per frame.
- `evaluate.py` turns score files and labels into Markdown tables and exports disagreements.
- `requirements.txt` lists the dependencies. They are deliberately not an extra of the
  package: install them in a separate virtual environment.

## Requirements

- A dataset in the format `tapo-monitor label` and `tools/collect_frames.sh` produce (see
  [Labeling](../../docs/labeling.md)): JPEGs, optional `index.jsonl` files and a
  `labels.jsonl` with `person` / `no_person` / `unsure` verdicts. **Hand labels matter most**;
  see the biases below.
- Python 3.10 or newer. A CPU is enough (a few hundred ms to about a second per frame);
  Large models are slow on weak CPUs, so run overnight and use `nice`.
- For YOLOX: the ONNX files (`yolox_s.onnx`, `yolox_m.onnx`, `yolox_x.onnx`) from the
  [YOLOX releases](https://github.com/Megvii-BaseDetection/YOLOX/releases), in one directory.
- For the `scorer` adapter: a running scoring service (the production baseline), see
  [Operations](../../docs/operations.md).

## Install

```bash
python3 -m venv ~/bench-venv
~/bench-venv/bin/pip install -r tools/bench_detectors/requirements.txt
```

Delete the lines of adapters you will not use (`rfdetr`, `torch`, `torchvision`,
`transformers`); `evaluate.py` needs only `numpy`.

## Run

```bash
B=~/bench && mkdir -p "$B"
PY=~/bench-venv/bin/python
T=tools/bench_detectors

# 1. the manifest (--lat/--lon only to split day and night; omit them to skip that table)
$PY $T/manifest.py ~/tapo-dataset --out $B/manifest.json --lat 50.0 --lon 14.4

# 2. score with each model, one at a time (resumable: re-run to continue)
nice -n 19 $PY $T/run.py scorer       --bench-dir $B --dataset ~/tapo-dataset   # the production model
nice -n 19 $PY $T/run.py rfdetr_large --bench-dir $B --dataset ~/tapo-dataset
nice -n 19 $PY $T/run.py hf:PekingU/rtdetr_r50vd --bench-dir $B --dataset ~/tapo-dataset

# 3. tables
$PY $T/evaluate.py --bench-dir $B --dataset ~/tapo-dataset --baseline scorer \
    --candidate rfdetr_large --cascade rfdetr_large --disagree-dir $B/disagree
```

Models for `run.py`: `scorer`, `yolox_s|m|x` (`--yolox-dir`), `rfdetr_nano|small|medium|base|large`,
`tv_frcnn` (torchvision Faster R-CNN v2) and `hf:<repo id>` for any HuggingFace object
detector (DETR, RT-DETR, D-FINE and so on; the class named `person` is used). The score of a
frame is the highest person confidence found, with a floor of 0.02. Score files are named
after the model (`scores/hf_<repo>.jsonl` for `hf:` models); `--baseline` names the file of the
model you run in production (`scorer` above, or `yolox_m` when you scored it locally).

`run.py` scores manual frames first, then unlabeled ones, then automatic ones, so a `--limit`
run already covers the labels that count. The `scorer` adapter sends the frame to
`--scorer-url` (default `http://127.0.0.1:8766/score`) and reads a bearer token from the
variable named by `--token-env`; a local ONNX run differs from the service by up to about
0.03 because of JPEG decoding and resizing.

## Read the results

`evaluate.py` prints Markdown and writes `results.md`.

1. **AUC and fixed thresholds.** AUC is the chance a random person frame outscores a random
   empty one (0.5 = coin flip, 1 = perfect); it does not depend on a threshold. The
   columns give false alarms (`no_person` frames at or above the threshold) / missed persons
   (`person` frames below it).
2. **At most 2 % of persons missed.** The highest threshold that keeps recall at 98 % per
   model, with the false alarms that remain and a 95 % bootstrap interval (1000 resamples,
   fixed seed, positives and negatives resampled separately). This is the fairest single
   comparison: models score on different scales, so compare at equal recall, not at 0.45.
   The matched-to-baseline table is kept for completeness; when the baseline already raises
   nearly every false alarm in your sample it is degenerate.
3. **All labels** includes the automatic labels. See the bias below; read it as a ceiling.
4. **Day and night** (with `--lat/--lon`). Night = solar elevation below -3 degrees.
   Cells with fewer than 30 frames in a class are marked indicative.
5. **Disagreements.** Frames no human has judged where one model scores 0.45 or more and the
   other below 0.25, copied with an `index.jsonl` to `--disagree-dir/disagree/<candidate>/`
   (largest difference first). Label them (`tapo-monitor label <disagree-dir>`), append the new
   lines of its `labels.jsonl` to the dataset's, and re-run. **This is the most informative
   step**: it tells you who is right exactly where the models differ.
6. **Cascade** (`--cascade MODEL --band 0.30,0.65`): keep the baseline and let the candidate
   re-score only the gray band. Compare it with the candidate alone.

## Biases, read before you trust a number

- **Hand labels versus automatic labels.** Automatic labels come from a teacher detector. If
  the teacher is in the same family as the model in production (here YOLOX), those labels favour
  that family and hide errors both share. Use hand labels for the comparison; the
  all-labels tables are a secondary, optimistic view for the teacher's family.
- **Frame selection.** Hand-labeled frames are not a random sample of your cameras. If they
  come from the sent log and the review log, the negatives are mostly frames where the
  production model raised an alarm (its own errors) and the positives are mostly frames it
  saw. The production model then looks worse than it is in service, because its easy true
  negatives are missing, and its misses are under-represented. A challenger from another
  family gains from both effects. Treat the size of the gap as an upper bound and use the
  disagreement review to check it.
- **Few negatives.** A few dozen `no_person` frames give wide intervals (a count of 8 false
  alarms can mean 3 to 14). Check the interval before ranking close models. Label more
  empty frames, drawn at random, to tighten this.
- **Night and per-camera cells are small.** Day/night is a rough solar-elevation rule and
  the night cell often has very few negatives.
- **Same frames, same thresholds.** All models see exactly the same JPEGs, but each scores
  on its own scale; never compare models at one fixed threshold only.
- **Parity.** A model's score through this tool is not guaranteed to equal its score inside
  the service after export (resize without or with antialiasing, fp16 on a GPU). Re-check
  parity on your frames before deployment.

## Licences

Check the licence of every model and library before you ship it. As reported by the projects
at the time of writing (verify yourself): YOLOX and the RF-DETR code and most model sizes are
Apache-2.0 (confirm the weights you use, some sizes carry another licence), torchvision
models are BSD, HuggingFace models carry the licence on their model card. The Ultralytics
YOLO family is AGPL-3.0, which is a poor fit for redistributing a service; this tool does not
include an adapter for it.
