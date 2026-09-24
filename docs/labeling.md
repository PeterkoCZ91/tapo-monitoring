# Labeling alert frames

The sent log (`TAPO_SENT_LOG_DIR`) keeps every photo that went out and the review log
(`TAPO_REVIEW_LOG_DIR`) every frame corroboration held back plus a random sample of the
frames dropped below the threshold — see
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

Held frames only cover the band just under the send line. A person the scorer rated
p0.05 is dropped outright, and without a copy of that frame no label can ever count it as
a miss. So the review log also keeps a random sample of the frames dropped below
`scorer.threshold` — on the live pass, by the sampler and in SD/recording frame
selection (the best frame of a dropped sequence, not every frame):

```bash
export TAPO_REVIEW_DROP_SAMPLE=0.05       # share of dropped frames kept; 0 turns it off
export TAPO_REVIEW_DROP_MAX_PER_HOUR=6    # per camera and clock hour
```

The rate is flat across scores on purpose: the labelling queue already stratifies by
score, and a flat rate keeps the weighting trivial. Each sampled record has
`"verdict": "drop"`, a `path` (`live`, `sampler`, `sd`) and `"sample_rate"`, so one
labelled sampled frame stands for `1 / sample_rate` dropped ones. The hourly cap bounds
the disk use on a rainy night; when it bites, the real rate that hour was lower than
`sample_rate`, so a weighted estimate is a lower bound. Hub-clip drops are archived in
full and carry no `sample_rate` — read that as a rate of 1.

Every record of a frame taken for a camera event — sent, held, sampled or hub-clip drop —
carries `incident` (`<camera>-<event start>`) and `event_start` (the event's start in
whole epoch seconds), so the frames of one visit and its delivered alert group by ID.
Records written before these fields existed, and frames with no camera event behind them
(shadow-scan finds), have neither.

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

`tapo-monitor label-stats dataset/ [--json] [--config cameras.yaml]` and the `/stats`
page report, overall, per score band and per camera:

- label counts;
- **false alarms**: sent frames labeled `no_person`, out of decided sent frames;
- **misses**: review (held) frames labeled `person`, out of decided review frames;
- the **best threshold**: the `person >= t` cut with the fewest errors over the labeled
  frames that have a score. Ties go to the lower threshold (fewer misses).

`unsure` is counted but left out of both rates and the threshold. The numbers are
estimates over what was labeled — the queue deliberately oversamples the gray zone, so
treat a band's rate as that band's, not the fleet's.

### Held and dropped frames

A review frame's index `verdict` says why it was not sent, and the two mean different
things: a person in a **held** frame (`hold`, corroboration waited) is a hold error; a
person in a **dropped** frame (`drop`, below the threshold — hub clips, and a random
sample of below-threshold frames when the host archives one) is a real miss. Once
anything other than held frames has been labeled, the text output adds a *misses by
review verdict* block, and the JSON carries it always under `verdicts`, in every
summary block (overall, per band, per camera):

```json
"verdicts": {"hold": {"person": 30, "decided": 33, "rate": 0.91, "sampled": 0,
                      "estimated_person": null, "estimated_decided": null},
             "drop": {"person": 2, "decided": 40, "rate": 0.05, "sampled": 35,
                      "estimated_person": 20.0, "estimated_decided": 355.0}}
```

A sampled drop carries its `sample_rate` in the index; each such frame then stands for
`1 / sample_rate` dropped frames, and `estimated_person` / `estimated_decided` are the
labeled counts scaled that way (unsampled frames count once). They are estimates, and
only of the part of the sample that has been labeled; the raw `person` / `decided`
counts next to them are what was actually seen. A label whose frame has no `verdict`
(an older dataset) counts as `unknown`.

### Day and night

```bash
tapo-monitor label-stats dataset/ --config cameras.yaml [--json]
tapo-monitor label dataset/ --config cameras.yaml          # same split on /stats
```

With `--config`, every labeled frame with a time (`ts` in its index) is placed in day
or night the way the daemon would have judged it: the site's night from the config's
`location` (in its timezone, as `replay` does), then the camera's own `schedule`
(`always_night` / `always_day`); a camera not in the config gets the site's night, and a
frame without a time is `unknown`. The output adds a table of the best threshold — with
its n, person / no_person counts, errors, false alarms and misses — overall, for day,
night and unknown, and per camera for day and night. The JSON adds `day_night`:

```json
"day_night": {"day": {…summary…, "threshold": {…}}, "night": {…}, "unknown": {…},
              "cameras": {"front": {"day": {…}, "night": {…}}},
              "min_support": {"decided": 20, "per_class": 5}}
```

Every best threshold (including the overall one) also carries `person`, `no_person` and
`supported`. A slice with fewer than 20 decided frames with a score, or fewer than 5 of
either class, is marked **too few labels**: the minimum-error cut on a handful of frames
sits wherever one odd frame happens to be, which is noise, not a threshold to ship.
Check any threshold taken from here with `tapo-monitor replay --compare` before
changing the config.

Without `--config` the output is what it always was, apart from the verdict block
once dropped frames are labeled and the incident section appended at the end (and
`verdicts` / `supported` / `incidents` as new JSON keys).

### Incidents

Frame rates hide what the person holding the phone cares about: was each visit alerted,
and how late. So `label-stats` (and `/stats`) end with a section per **incident**, built
from every indexed frame — labeled or not, since a delivery counts even when nobody
labeled its frame.

Frames are grouped per host (the directory holding its `sent-log` / `review-log`) and
camera:

- a record with an `incident` ID (`<camera>-<event start>`) belongs to that incident;
- a record without one joins the camera's previous incident when it is at most 150 s
  after that incident's last frame, else it starts a new one. 150 s is above the alert
  cooldown (120 s) — a subject still in view is photographed again right after it — and
  above the sampler's `group_gap` (90 s). Two visits closer than that merge, which can
  hide a miss behind an alerted neighbour but never invents one;
- in a period where only some paths wrote IDs, an ID frame arriving within the gap of an
  incident without an ID takes that incident over, so one visit is not counted twice;
- a record without `camera` (older hosts) takes the host's camera when the host only
  ever named one, else it is grouped as `unknown`.

Per incident: **person** when any frame is labeled person, **no_person** when every
labeled frame is no_person, otherwise unsure or unlabeled. **Alerted** when a sent frame
was delivered (`delivered` missing counts as delivered; `false` — a failed send — does
not). The start is the camera's event start: a record's `event_start`, else the start in
the incident ID, else the event time printed in an older sent frame's caption (read in
this machine's local time and trusted only when it is at most an hour before the frame),
else the first frame's time. The **delay** runs from there to the first delivered alert.

```
incidents: 212 from 480 frames (one incident per incident ID, else frames of a camera less than 150 s apart), 95 with a labeled frame:
group         incidents  labeled  person  person alerted  missed       false alarms  delay median  delay p90
all           212        95       80      72              8/80 (10.0%) 2/74 (2.7%)   40 s          95 s
camera front  …
day           …
night         …
missed person incidents by the verdicts of their review frames: hold 6 (person labeled in 5), drop 2 (person labeled in 2)
```

- **missed**: person incidents without a delivered alert, of person incidents;
- **false alarms**: alerted incidents labeled no_person, of alerted incidents with a
  decided label;
- **delay**: median and p90 (nearest rank) over alerted person incidents; the note
  under the table says how many were timed from an event start rather than a first
  frame;
- the last line splits the missed incidents by the verdicts their review frames had and
  says in how many a frame of that verdict was labeled person: a person in a held frame
  means the corroboration hold swallowed the visit, one only in dropped frames the
  threshold did; `no review frame` means only an undelivered send was archived.

With `--config`, day and night rows follow, judged on the incident start as for frames.
Without any labeled incident the section says so instead of printing a table of zeros.
A visit none of whose frames was archived cannot appear at all, so the missed rate is a
lower bound. The JSON adds a top-level `incidents` key; the other keys are unchanged:

```json
"incidents": {"gap_seconds": 150.0, "frames": 480,
  "all": {"total": 212, "labeled": 95,
          "counts": {"person": 80, "no_person": 12, "unsure": 3, "unlabeled": 117},
          "alerted": 150, "person_alerted": 72,
          "missed": {"count": 8, "person": 80, "rate": 0.1},
          "false_alarms": {"count": 2, "decided": 74, "rate": 0.027},
          "delay": {"n": 72, "from_event_start": 70, "median": 40.0, "p90": 95.0},
          "missed_verdicts": {"hold": {"incidents": 6, "frames": 9, "person_incidents": 5},
                              "drop": {"incidents": 2, "frames": 2, "person_incidents": 2}}},
  "cameras": {"front": {…same block…}},
  "day_night": {"day": {…}, "night": {…}, "unknown": {…}},
  "missed": [{"id": "front-1767301200", "host": "host-a", "camera": "front",
              "start": 1767301200.0, "frames": 3,
              "verdicts": {"hold": {"frames": 2, "person": 1}}}]}
```

`day_night` is there only with `--config`; `missed` lists every missed person incident
(`id` is null for one grouped by time), oldest first, to look at by hand.
