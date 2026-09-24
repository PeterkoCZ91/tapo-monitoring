# Labeling alert frames

The sent log (`TAPO_SENT_LOG_DIR`) keeps every photo that went out and the review log
(`TAPO_REVIEW_LOG_DIR`) every frame corroboration held back — see
[Operations](operations.md#inspecting-alert-frames). Neither says whether a person was
actually in the frame. `tapo-monitor label` adds that: a local page that shows the
collected frames one at a time and records a human verdict, and `tapo-monitor
label-stats` turns the verdicts into a false-alarm rate, a miss estimate and the
threshold that would have separated the labeled frames best.

## Collecting a dataset

Copy the sent-log and review-log folders off the hosts into one directory, in any layout:

```bash
mkdir -p dataset/host-a dataset/host-b
rsync -a host-a:/var/lib/tapo/sent-log   dataset/host-a/
rsync -a host-b:/var/lib/tapo/review-log dataset/host-b/
```

Every `index.jsonl` under the directory is read, recursively. A record with a `verdict`
field is a review frame; any other record is a sent frame. Records whose JPEG was pruned
before the copy are skipped. The same image copied twice is shown once.

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
