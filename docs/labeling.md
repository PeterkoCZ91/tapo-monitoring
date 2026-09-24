# Labeling alert frames

The sent log (`TAPO_SENT_LOG_DIR`) keeps every photo that went out and the review log
(`TAPO_REVIEW_LOG_DIR`) every frame corroboration held back — see
[Operations](operations.md#inspecting-alert-frames). Neither says whether a person was
actually in the frame. `tapo-monitor label` adds that: a local page that shows the
collected frames one at a time and records a human verdict, and `tapo-monitor
label-stats` turns the verdicts into a false-alarm rate, a miss estimate and the
threshold that would have separated the labeled frames best.

## Collecting a dataset

`tools/collect_frames.sh` pulls every host's sent and review logs into one directory and
is safe to run nightly (for example from a user timer):

```bash
tools/collect_frames.sh ~/tapo-dataset host-a host-b    # SSH aliases of the hosts
```

It never deletes: frames stay after the host prunes them, and each host's
`index.jsonl` is merged into the local one rather than copied over it, so entries the
host has already forgotten are kept. Raise `TAPO_SENT_LOG_RETENTION_DAYS` /
`TAPO_REVIEW_LOG_RETENTION_DAYS` on the hosts if the collector cannot run daily. Any
other layout works too — a plain `rsync -a host:…/sent-log dataset/host/` is enough.

Every `index.jsonl` under the directory is read, recursively. A record with a `verdict`
field is a review frame; any other record is a sent frame. Records whose JPEG was pruned
before the copy are skipped. The same image copied twice is shown once.

## Let a teacher model label the easy frames first

Nobody labels hundreds of frames by hand. A larger detector than the production scorer
scores every frame once and labels the ones it agrees on:

```bash
tapo-monitor autolabel DATASET_DIR --model /path/to/yolox_x.onnx --threads 2
```

- production and teacher both at or above 0.65 → `person`; production below the gray
  zone and teacher below 0.20 → `no_person`; frames without a production score need the
  teacher alone at ≥ 0.80 / ≤ 0.10;
- everything else — disagreements and the gray zone — is left for the labeling page,
  which now shows the biggest disagreement first and the teacher score next to the
  production one;
- automatic labels carry `"by": "auto:<model>"` and the teacher score; a human label
  always wins, and `label-stats` says how many labels were automatic;
- teacher scores are cached in `teacher.jsonl` by image hash, so a rerun (for example
  after the nightly collection) only scores new frames.

Agreement is not ground truth: two detectors can share a blind spot. It only takes the
frames nobody needs to look at out of the queue. The teacher needs the scorer extras
(`onnxruntime`, `Pillow`); on a host that also runs the production scorer, keep
`--threads` low and run it under `nice`. YOLOX-x (Apache-2.0) is published on the
[YOLOX releases page](https://github.com/Megvii-BaseDetection/YOLOX/releases).

## Labeling

```bash
tapo-monitor label dataset/                 # http://127.0.0.1:8791/
tapo-monitor label dataset/ --port 8800 --bind 192.0.2.10
```

The page shows the frame, its camera, time, source (sent/review) and person score, with
four actions: ✅ person (`1`), ❌ no person (`2`), ❓ unsure (`3`) and skip (`space`).
`u` or Backspace undoes the last action. It works on a phone.

Unlabeled frames are queued in the order that pays off fastest:

1. the gray zone, person score 0.30–0.65, where the threshold itself is uncertain;
2. a random sample of low scores (below 0.30) — possible misses. `--low-sample N` caps
   it, `--seed` makes the order repeatable;
3. high scores (above 0.65) — possible false alarms, nearest the gray zone first;
4. frames without a score.

Restarting the tool resumes where you stopped: already-labeled frames are not queued.

The server binds `127.0.0.1` by default. It has no authentication, so binding a wider
address (a VPN interface, say) is the operator's explicit choice. It serves only frames
listed in an index that resolve inside the dataset directory; `..`, absolute paths and
symlinks pointing outside are refused.

## The label file

Verdicts are appended to `<dataset_dir>/labels.jsonl`, one line each:

```json
{"path": "host-a/sent-log/20260101-220000-000000.jpg", "sha256": "…", "label": "no_person",
 "labeled_at": 1767301200.0, "score": 0.71, "camera": "front", "source": "sent"}
```

`label` is `person`, `no_person` or `unsure`. The latest line per `sha256` wins, so a
frame keeps its label when it is re-collected under another path. Undo is one more line
(`"undo": true`) carrying the previous label, or `unlabeled`; nothing is ever rewritten.
Images are only read, never modified or deleted.

## Stats

`tapo-monitor label-stats dataset/ [--json]` and the `/stats` page report, overall, per
score band and per camera:

- label counts;
- **false alarms**: sent frames labeled `no_person`, out of decided sent frames;
- **misses**: review (held) frames labeled `person`, out of decided review frames;
- the **best threshold**: the `person >= t` cut with the fewest errors over the labeled
  frames that have a score. Ties go to the lower threshold (fewer misses).

`unsure` is counted but left out of both rates and the threshold. The numbers are
estimates over what was labeled — the queue deliberately oversamples the gray zone, so
treat a band's rate as that band's, not the fleet's.
