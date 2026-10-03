# Measurements

Aggregate numbers from one real deployment, published so you can judge what the project
does in practice and compare your own installation. Nothing here identifies a site, a
camera or a person: cameras are named by model only, dates are given as a range of months,
and no coordinates, polygons or places appear.

Treat every figure as an example from one installation, not as a benchmark of the
software. [Limitations](#limitations) lists what the numbers cannot tell you.

## What was measured and how

- **Deployment.** One owner, several sites, a Raspberry Pi per site, one shared local
  scoring service (YOLOX, `person` class). Camera models: C560WS (pan/tilt, outdoor-rated;
  three cameras in the labeled data, a fourth has no labels), C545D (dual lens; one camera) and C260 (one camera). The dataset does not
  record a model; the model of each camera name comes from the owner's inventory, and the
  tables below only use the three groups C560WS / C545D / C260.
- **Period.** September to October 2026. Counts of labeled frames cover the whole period;
  latency tables use roughly the same window.
- **Frames.** Every alert frame that was sent (the sent log) plus a sample of frames the
  gates held or dropped (the review log), see [Labeling](labeling.md).
- **Labels.** Each frame is `person`, `no_person` or `unsure`. A larger teacher detector
  (YOLOX-x) labeled the frames where it and the production scorer agreed clearly; a human
  labeled the rest through the labeling page. Of 2,563 labeled frames, 475 are human
  labels and 2,088 automatic ones (both models agreed). A human label always wins.
  `unsure` frames (2) are excluded from every rate.
- **Definitions.** A *false alarm* is a sent frame labeled `no_person`. A *miss* is a
  held frame (review log) labeled `person`. Rates are `count of decided frames`; every
  table gives that `n`. An *incident* is one event, grouped by incident ID or, without
  one, by frames of one camera less than 150 s apart. *First-alert delay* is event start
  to the first delivered alert.
- **Tools.** The accuracy, incident and delay figures come from `tapo-monitor label-stats`;
  the replay figures from the `replay` machinery and journal extracts, see
  [Reproduce it](#reproduce-it).

## Accuracy by score band

Person score of the production scorer at send/hold time. Bands: `low` below 0.30,
`gray` 0.30 to 0.65, `high` above 0.65, `none` no score recorded.

| Band | Labeled frames (n) | Labeled `person` | False alarms (sent) | Misses (held) |
| --- | ---: | ---: | ---: | ---: |
| high | 1,855 | 1,837 | 17 of 1,839 (0.9 %) | 15 of 15 |
| gray | 116 | 87 | 23 of 80 (29 %) | 30 of 35 (86 %) |
| low | 537 | 0 | none sent | 0 of 537 (0 %) |
| none | 55 | 1 | 54 of 55 (98 %) | none held |
| **all** | **2,563** | **1,925** | **94 of 1,974 (4.8 %)** | **45 of 587 (7.7 %)** |

Reading it:

- Above 0.65 almost everything is a person. In the gray zone the score cannot decide:
  three in ten sent frames were not a person, and most held frames were. That is why the
  labeling queue concentrates there, and why a band's rate is not a fleet rate.
- The `low` frames are a 1-in-20 random sample of frames dropped before sending; none
  of the 537 labeled ones held a person. Scaled by the sampling rate that estimates
  about 0 persons among roughly 10,700 dropped frames (an estimate, not a count).
- `none` frames (no production score, mostly motion-only events) are nearly all empty
  scenes. They are few (55), so do not generalize.
- Held frames labeled `person`, by review verdict: 35 of 40 `hold`, 7 of 7 `cooldown`,
  3 of 3 `shadow`. The cooldown and shadow rows are tiny samples.
- The fewest-errors cut on the labeled frames was `person >= 0.31`: 45 errors in 2,506
  decided frames (all false alarms, no misses), 98.2 % accuracy. It was fitted on the
  same labels, so it is optimistic.

## Accuracy by camera model

| Camera model | Labeled frames (n) | False alarms (sent) | Misses (held) |
| --- | ---: | ---: | ---: |
| C560WS (three cameras) | 2,345 | 62 of 1,802 (3.4 %) | 44 of 542 (8.1 %) |
| C545D | 98 | 22 of 84 (26 %) | 0 of 14 |
| C260 | 120 | 10 of 88 (11 %) | 1 of 31 (3 %) |

Per C560WS camera the false-alarm rate ranged from 1.9 % to 8.1 % (decided sent frames
from 34 to 1,064). The C545D and C260 figures come from only a few weeks of data and
small n, and from scenes with a lot of non-person motion, so the difference to C560WS may
be the scene, not the model.

## Accuracy by time of day

Frames split into day and night the way the monitor itself judges it (`label-stats
--config`): astronomical night at the configured location, then the camera's own
`schedule`. One location and one set of `NIGHT_*` values was used for every camera, so for
cameras at other sites the boundary can be off by some minutes around dusk and dawn. Same
labeled frames as above (2,563; every frame had a timestamp).

| Part of day | Labeled frames (n) | False alarms (sent) | Misses (held) |
| --- | ---: | ---: | ---: |
| day | 1,899 | 66 of 1,525 (4.3 %) | 33 of 372 (8.9 %) |
| night | 664 | 28 of 449 (6.2 %) | 12 of 215 (5.6 %) |

| Camera model | Day: false alarms | Day: misses | Night: false alarms | Night: misses |
| --- | ---: | ---: | ---: | ---: |
| C560WS | 37 of 1,364 (2.7 %) | 32 of 341 (9.4 %) | 25 of 438 (5.7 %) | 12 of 201 (6.0 %) |
| C545D | 21 of 75 (28 %) | 0 of 4 | 1 of 9 | 0 of 10 |
| C260 | 8 of 86 (9 %) | 1 of 27 (4 %) | 2 of 2 | 0 of 4 |

Reading it:

- The false-alarm rate on C560WS roughly doubles at night (2.7 % to 5.7 %); the decided
  night sample is a third of the day sample, so the gap is suggestive. Misses go the other
  way (9.4 % to 6.0 %), mostly held frames of a person that daylight scored in the gray
  zone: of the day misses 27 of 29 `hold` frames and 6 of 6 `cooldown` frames were persons.
- The fewest-errors cut was `person >= 0.31` for both parts (day 32 errors in 1,860
  decided frames, night 13 in 646), in-sample as before. The best cut is the same by day
  and by night, so these labels do not suggest a separate night threshold.
- C545D and C260 have 19 and 6 night frames; their night cells are not usable.

## Accuracy by delivery path

The sent log names how the frame was obtained (`path`): `live` (fetched at the event),
`sampler` (event-window sampler) or `sd` (frame cut from the camera-card recording). Only
records from the late-September window on carry the field; 798 sent and 43 held labeled
frames from earlier records have none and are listed separately. Sent frames, false alarms:

| Path | Labeled sent frames (n) | False alarms | Median person score |
| --- | ---: | ---: | ---: |
| live | 495 | 39 (7.9 %) | 0.85 (n = 478) |
| sampler | 142 | 9 (6.3 %) | 0.82 (n = 139) |
| sd | 539 | 26 (4.8 %) | 0.84 (n = 515) |
| path not logged | 798 | 20 (2.5 %) | not shown |

By camera model, sent frames with a path (false alarms of n): C560WS live 17 of 352,
sampler 3 of 135, sd 22 of 517; C545D live 17 of 69, sd 3 of 13, sampler 2 of 2; C260 live
5 of 74, sd 1 of 9, sampler 4 of 5.

Held (review) frames labeled `person`: sampler 0 of 365, sd 7 of 154, live 0 of 25, and 38
of 43 for records with no path (that group is the early gray-zone oversample, not a path).

Reading it: the median person score of the sent frame is similar on all three paths. The `live` false-alarm rate looks highest, but 17 of its 39 false alarms are
C545D, whose scene makes that camera's rate high on every path, so the pooled rate mixes
cameras with the path. The paths also see different events (the sampler and `sd`
can pick up events the live frame did not), so the rates are not a like-for-like comparison.

## Accuracy by lens (dual-lens camera)

The C545D caption names the lens that produced the frame (`wide` or `pan/tilt`) only since
the lens pick was logged; most of its labeled frames predate that.

| Lens named in the caption | Labeled sent frames (n) | False alarms |
| --- | ---: | ---: |
| wide | 5 | 0 |
| pan/tilt | 27 | 10 (37 %) |
| not named | 52 | 12 (23 %) |

n is small (32 frames with a lens, 84 decided sent frames in all). The 10 false alarms
from the pan/tilt lens are 37 %, against 0 of 5 for the wide lens, but five wide frames
cannot show a difference. Treat this as "worth watching", not a finding.

## What a different threshold would have done

For each threshold, the labeled frames that carry a production score (2,506 of 2,563; 1,924
of them `person`) were scored as if every frame at or above the threshold were alerted. A
*false alarm* is a `no_person` frame at or above it, a *missed person* is a `person` frame
below it. **In-sample**: the labels, the gray-zone oversampling and the choice of
frames to label all come from this deployment, so it shows the trade-off, not what a
threshold would do on your cameras.

| `scorer.threshold` | Frames alerted | False alarms | Missed persons | Person recall |
| ---: | ---: | ---: | ---: | ---: |
| 0.30 | 1,969 | 45 | 0 | 100 % |
| 0.35 | 1,965 | 44 | 3 | 99.8 % |
| 0.40 | 1,963 | 44 | 5 | 99.7 % |
| 0.45 | 1,961 | 44 | 7 | 99.6 % |
| 0.50 | 1,956 | 44 | 12 | 99.4 % |
| 0.55 | 1,943 | 38 | 19 | 99.0 % |
| 0.60 | 1,896 | 23 | 51 | 97.3 % |
| 0.65 | 1,854 | 17 | 87 | 95.5 % |

Reading it:

- Between 0.30 and 0.50 the threshold hardly changes either count (one false alarm and
  twelve persons). The false alarms there are scored above 0.50, so no threshold in
  this range removes them. Raising it to 0.60 or 0.65 removes 22 to 28 false alarms and
 
  costs 51 to 87 persons.
- Per camera model the same pattern holds. C560WS (n = 2,291): 13 false alarms and no
  misses at 0.30, 12 and 5 at 0.40, 12 and 12 at 0.50, 4 and 86 at 0.65. C545D (n = 96):
  20, 20, 20 and 8 false alarms, 0, 0, 0 and 1 missed. C260 (n = 119): 12, 12, 12 and 5
  false alarms, no misses.
- The labeled set contains persons only down to a score of about 0.30 because lower
  frames were a random 1-in-20 draw with no person (see above). Everything a threshold below 0.30
  would admit is invisible here.
- Frames held by a scene gate or cooldown are counted as alerted here. This sweep is about
  the score only.

## Alert volume over time

Delivered alert photos per day from the sent log, as a daily median with the number of days
in the window. A change is shown only where the setting
change is documented and dated; other differences are not attributed.

| Camera model | Window | Days (n) | Median alerts per day | Range |
| --- | --- | ---: | ---: | ---: |
| C560WS (daytime detection off) | August, before | 10 | 7 | 4 to 25 |
| same camera, daytime detection on | August, after | 8 | 32 | 23 to 47 |
| same camera, later | September to October | 31 | 83 | 52 to 136 |
| C560WS (night-dwell change) | August to September, before | 40 | 47 | 6 to 85 |
| same camera, after | September to October | 15 | 44 | 0 to 84 |

- On the first camera, turning on daytime detection (`night_only: false`) took the daily
  median from 7 to 32, a factor of about four and not a defect: daytime pedestrian traffic
  is real events. Its further rise to a median of 83 in September has no
  single documented cause in this data: several changes shipped in those weeks and the
  season changed too, so it is not attributed.
- On the second camera, a longer night dwell after auto-track did not change alert volume
  (47 to 44), as intended: the payoff is the camera-card recording, not more photos. The
  minimum of 0 in the "after" window is one day with no record at all, cause not checked.
- The other C560WS cameras, the C545D and the C260 have under 10 days of records and are
  not shown. Days with no record count as zero in the second pair of rows and are left out
  of the first pair, which understates the "before" medians there slightly.

## Incidents

An incident is what a person receiving alerts experiences. Incidents with at least one
labeled frame (n = 2,331 of 11,435 incidents; the rest are unlabeled, not negative):

| Camera model | Labeled incidents | Person incident not alerted | Alerted incident, no person |
| --- | ---: | ---: | ---: |
| C560WS | 2,118 | 30 of 1,685 (1.8 %) | 48 of 1,703 (2.8 %) |
| C545D | 97 | 2 of 61 (3 %) | 22 of 81 (27 %) |
| C260 | 116 | 1 of 79 (1 %) | 11 of 89 (12 %) |
| **all** | **2,331** | **33 of 1,825 (1.8 %)** | **81 of 1,873 (4.3 %)** |

"Person incident" means any frame of the incident is labeled `person`. "Alerted incident,
no person" counts only alerted incidents whose labeled frames are all `no_person`. Of the
33 person incidents without an alert, 21 ended in a held frame, 7 were suppressed by the
cooldown, 3 by the shadow path and 2 had no review frame at all.

## First-alert delay

Seconds from event start to the first delivered alert, per delivery path
(`label-stats` incident table, every alerted incident, labeled or not). Medians and
percentiles cannot be pooled across groups, so the camera-model table shows the range over
the cameras it covers.

| Path | First alerts (n) | Median | p90 |
| --- | ---: | ---: | ---: |
| live (frame fetched at the event) | 582 | 26 s | 45 s |
| sampler (event-window sampler) | 191 | 74 s | 169 s |
| SD recording follow-up | 830 | 63 s | 202 s |
| older records, path not logged | 9,081 | 116 s | 185 s |

| Path | C560WS (three cameras) | C545D | C260 |
| --- | --- | --- | --- |
| live | median 24 to 28 s (n = 422) | 25 s / p90 42 s (n = 83) | 24 s / p90 42 s (n = 77) |
| sampler | median 56 to 81 s (n = 181) | 87 s (n = 2) | 62 s (n = 8) |
| SD | median 58 to 209 s (n = 789) | 161 s / p90 202 s (n = 31) | 179 s / p90 190 s (n = 10) |

The slowest figures are the camera-card SD path: it waits for the camera to finalize the
clip. The recording follow-up from a local recorder (one site) is much faster than the
camera-card path.

### A configuration change, before and after

On the camera that uses a local recording follow-up, changes to how the follow-up reads
the clip and later a frame pick by largest person box (instead of sharpest) changed
first-alert delay (SD recording path, median / p90):

| Period | n | Median | p90 |
| --- | ---: | ---: | ---: |
| before | 144 | 121 s | 193 s |
| after, steady days | 182 | 57 s | 100 s |

Re-scoring the archived frames showed the chosen person box got larger (median height
96 px to 102 to 104 px of a 720 px frame by period, area ratio 1.34, 95 % bootstrap
interval 1.15 to 1.59, daytime, n = 179 before and 184 after). The live path, which was
not changed, showed no significant difference. The two periods differ in time-of-day mix,
so this is suggestive, not a controlled experiment.

## `events_1` bits against `alarm_type`

Events logged by the monitor over about four weeks on three sites (about 16,000 events,
C560WS only). The documented rule is that bit `alarm_type - 1` is set
([`events_1` bitmask](events1-bitmask.md)).

| `alarm_type` | Bits set in `events_1` | Events (n) | Rule holds |
| --- | --- | ---: | --- |
| 2 (motion) | 1 | 11,138 | yes |
| 2 | none | 2 | no (empty mask) |
| 6 (person) | 1 and 5 | 3,957 | yes |
| 6 | 5 | 845 | yes |
| 6 | 1, 5 and 8 | 96 | yes, bit 8 co-set |
| 4 (line crossing, as hypothesized) | 1 and 3 | 14 | yes |
| 4 | 1, 3 and 5 | 27 | yes |
| 4 | 1, 3, 5 and 8 | 2 | yes, bit 8 co-set |
| 9 (pet, as hypothesized) | 8 | 4 | yes |

`alarm_type` 8 (vehicle, bit 7) never occurred, so that part of the mapping is untested.
Types 4 and 9 appeared on one site only. Scoring the 33 frames matched to those events
with the production scorer: for type 4, 31 of 32 frames held a person (score median about
0.88), so it correlates with a person, but frames exist mostly because person alerts were
sent (a selection bias); for type 9 there is one frame, with a person (0.83) and no
animal, which neither supports nor refutes "pet". Events were matched to frames by a time
window, not by incident ID.

## Replay: watch zones and loitering

These were replays over archived frames and sampler logs, with the code the monitor runs
([Watch zones](watch-zones.md)). Boxes are not stored in the archive, so the frames were
re-scored with today's model; the scores are not exactly what the monitor saw at alert
time.

- **Zone filter on a camera facing a street (C560WS; 1,454 persons at score 0.45 or
  higher in 1,225 frames).** A hand-drawn polygon around the private yard dropped 82 to
  84 % of the persons (two polygon variants) and removed every person from about 82 to
  83 % of the frames (1,011 and 999 of 1,225): most alerts there were pedestrians on the
  street. The polygon was estimated from the images and only the 16 cases nearest its
  edge were checked by eye, so the exact percentage is soft. The qualitative result is
  robust: on a camera that sees a public road, a zone filter would delete most existing
  alerts, so use it as a second, different notification rather than as a filter. Zones in
  image fractions also assume a fixed view and fit badly on a camera that auto-tracks.
- **Loitering.** Replayed over about 4,600 sampler groups (15,000 scored frames, median
  frame spacing 32 s): zero triggers for 4 and for 6 consecutive frames, and never more
  than one notice per group. This is not evidence that nobody lingered. The sampler stops
  after the first alert, and the longest run of consecutive person frames in the data was
  2, so the data holds no information about loitering until the sampler keeps frames
  after the alert.

## Detector comparison

Is a newer detector worth swapping in for the YOLOX-m the scorer runs? The same labeled
frames were scored by a dozen and a half models and variants, with the tool in
[`tools/bench_detectors`](../tools/bench_detectors/README.md), which you can run on your own data.
This is one sample from one deployment and it is biased in a known direction
([how](#how-far-to-trust-it)); read it as "which models are worth trying", not as a ranking.

**Setup.** 473 hand-labeled frames (425 `person`, 48 `no_person`; `unsure` excluded), 93 of
them at night (only 8 of those empty). The score of a frame is the highest person confidence.
Models ran on a CPU through their reference code, except YOLOX-m, which was scored by the
production service. Latency was measured separately with OpenVINO on an Intel HD Graphics 530 iGPU.

### Accuracy on hand labels

AUC, and the false alarms that remain at a threshold chosen so that at most 2 % of the
425 persons are missed (so every model has about the same recall; 95 % bootstrap interval
in brackets for the best model).

| Model | AUC | Threshold (<= 2 % persons missed) | False alarms of 48 |
|---|---|---|---|
| RF-DETR Large | 0.974 | 0.194 | 8 (3 to 14) |
| RT-DETR R50 | 0.970 | 0.210 | 20 |
| RF-DETR Medium | 0.961 | 0.201 | 11 |
| YOLOX-x (the teacher of the automatic labels; biased) | 0.960 | - | 17 |
| Faster R-CNN ResNet-50 v2 (torchvision) | 0.955 | - | - |
| D-FINE L | 0.949 | - | 21 |
| RF-DETR Base | 0.942 | - | 24 |
| RF-DETR Small | 0.941 | - | - |
| D-FINE M | 0.933 | - | - |
| RT-DETR R18 | 0.910 | - | - |
| RF-DETR Nano | 0.894 | - | - |
| **YOLOX-m (production)** | **0.843** | 0.463 | **45** |
| YOLOX-s | 0.795 | - | - |

A dash means the figure was not tabulated. Two results are not reliable and are left out of any
conclusion: RT-DETR v2 scored 0.81 (its adapter was not verified) and plain DETR R50
saturated, so no threshold met the 2 % target. At fixed thresholds on the same frames, RF-DETR
Large raised 8 false alarms and missed 12 persons at 0.30, 4 and 24 at 0.45, 2 and 47 at 0.55;
YOLOX-m raised 45 and missed 7 at 0.45 (47 and 0 at 0.30, 17 and 86 at 0.65). Large on the
day frames alone (n = 380) reached an AUC of 0.983; at night (n = 93, eight empty frames) the
Large, Medium and RT-DETR R50 models scored 0.93 to 0.95, which is indicative only.

### Latency

Inference only, 50 frames, three repeats, three CPU threads, OpenVINO. Through the scoring
service on the iGPU, including JPEG decoding, resizing and HTTP, a request took p50 / p95
0.29 / 0.30 s for RF-DETR Medium and 0.45 / 0.46 s for Large (the YOLOX-m service was 0.29 s
at p95). Service start to listening took about 2.5 s with a warm kernel cache; the first
compile on an empty cache took 5 to 19 s.

| Model | Input | CPU p50 / p95 (ms) | iGPU p50 / p95 (ms) |
|---|---|---|---|
| RF-DETR Nano | 384 | 250 / 304 | 100 / 103 |
| RF-DETR Small | 512 | 505 / 540 | 184 / 192 |
| RF-DETR Medium | 576 | 659 / 715 | 239 / 246 |
| RF-DETR Base | 560 | 722 / 765 | 257 / 264 |
| RF-DETR Large | 704 | 1045 / 1145 | 384 / 396 |
| RT-DETR R50 | 640 | 1120 / 1230 | 406 / 410 |

The iGPU was 2.6 to 2.8 times faster than the CPU. Exporting the RF-DETR models to ONNX first
gave scores up to 0.19 away from the reference code; the cause was a resize with antialiasing
in the service's preprocessing (the model is trained without it). After the fix, scores through
the service matched the reference within 0.03 on 100 frames (about 0.0003 on a CPU; the rest is
probably half precision on the iGPU).

### Do the models disagree, and who is right?

On the frames no human had judged yet, 62 had one model at 0.45 or more and the other below
0.25 (YOLOX-m against RF-DETR Large). 60 of them were labeled by hand: 33 `no_person`, 26
`person`, 1 `unsure`.

- **RF-DETR saw, YOLOX-m did not** (11 frames): 10 were real people. YOLOX-m missed ten
  people that the larger model found.
- **YOLOX-m saw, RF-DETR did not** (49 frames): 32 were empty, 16 were real people.
  YOLOX-m raised 32 false alarms on static objects (bins, bagged goods, the roof of a play
  structure, and a handful of whole-frame boxes). The 16 real people RF-DETR scored low (0.08 to
  0.22), within the range of its empty frames, so it is not strictly better.
- On these 60 frames: YOLOX-m at 0.45 missed 10 of 26 persons with 32 false alarms of 33;
  RF-DETR Large at its 2 % threshold missed 12 of 26 with 2 of 33. A rule "RF-DETR Large at
  0.45 or more, or YOLOX-m at 0.45 or more with RF-DETR at 0.12 or more" missed 2 of 26 with 13
  false alarms of 33, better than either alone. That is a hypothesis from a small, biased
  sample, not a result.
- The false alarms came from **static clusters** (the same box position on many frames), so a
  second model helps where a hand-drawn ignore zone would also hide real passers-by.

### Cascade

Keeping YOLOX-m and re-scoring only the gray band (0.30 to 0.65) with RF-DETR Large re-scored
11.7 % of all frames (24.5 % of the hand-labeled ones). It did **not** work out: AUC 0.899, at
0.45 21 false alarms and 14 misses, against 4 and 24 for Large alone. 21 of the 48 empty
frames score above 0.65 in YOLOX-m, outside the band, so the second model never sees them.
This is partly the selection bias below (the negatives are frames YOLOX-m alarmed on); a
cascade is more promising in traffic where YOLOX-m's false alarms are less confident.

### How far to trust it

- **Selection bias, in YOLOX-m's disfavour.** The hand-labeled empty frames are mostly frames
  where YOLOX-m itself raised an alarm (sent and review logs), and the persons are mostly
  frames it detected. YOLOX-m therefore looks worse than it does in service (its easy true
  negatives are missing) and "matched to YOLOX-m at 0.45" is degenerate (45 of 48 false alarms).
  Models of another family gain from this. The disagreement review above is the least biased
  evidence, and even it only covers frames where the two models differ.
- **Automatic labels favour YOLOX.** They come from YOLOX-x agreeing with YOLOX-m; on all
  labels YOLOX-m's AUC is 0.997. Those figures are an upper bound for the YOLOX family and were
  not used for the table.
- **Only 48 empty frames.** Intervals are wide (8 false alarms is 3 to 14), neighbouring
  models cannot be ranked, and the night cell has 8 empty frames.
- **One site type.** Cameras of three models, mostly outdoors; no per-camera or per-weather cuts.
- **Licences.** The RF-DETR and RT-DETR models were Apache-2.0 according to their projects and
  model cards, and torchvision's are BSD; this was not independently verified here, and the
  Ultralytics YOLO family (AGPL-3.0) was deliberately left out. Check the weights you deploy.
- **Not measured.** Several hosts scoring at once, tiled scoring with a DETR model,
  other cameras at night, and the first start on a cold machine.

To repeat this on your own frames, follow the
[tool's README](../tools/bench_detectors/README.md) (hand labels, one venv, one model at a time).

## Limitations

- **One deployment.** One owner, a handful of cameras, one region and climate. Scenes
  dominate the numbers: a street, a porch and a window view give very different false
  alarm rates.
- **Samples are not independent.** Several frames come from one incident, and the review
  log is a deliberate sample (the gray zone is oversampled; low scores are a 1-in-20
  random draw). Rates describe the labeled sets, not all traffic, and no confidence
  intervals are given except where stated.
- **Labels are mostly automatic.** About 81 % are teacher labels where two models agree.
  Both are YOLO-family detectors and share blind spots, so errors both make are not
  visible. The human labels (19 %) are biased to the hard frames.
- **Threshold fitted on the same data.** The best-threshold result and the threshold
  sweep are in-sample.
- **Splits shrink n.** Day/night, path, lens and per-model cuts divide 2,563 frames into
  cells that are sometimes a few dozen; read each `n` before reading a percentage.
- **Replays use today's model.** Re-scored frames differ from the original alert-time
  scores, and replayed logs only contain what was sent or sampled.
- **Latency mixes configurations.** The window spans several restarts and setting
  changes; 85 % of first alerts come from older records with no path field, so the
  per-path tables cover only the newer 15 %.
- **Outage excluded.** A network outage of about ten hours was removed from the latency
  window; delays during outages are much longer.

## Reproduce it

Collect and label frames as described in [Labeling](labeling.md), then:

```bash
# accuracy per band, camera and day/night, incidents, first-alert delay per path
tapo-monitor label-stats ~/tapo-dataset --config cameras.yaml

# the same as JSON for your own analysis
tapo-monitor label-stats ~/tapo-dataset --json

# let the teacher label the easy frames, then label the leftovers
tapo-monitor autolabel ~/tapo-dataset --model /path/to/yolox_x.onnx
tapo-monitor label ~/tapo-dataset

# what a different config would have done to recorded events
tapo-monitor replay cameras.yaml --hours 168 --compare other.yaml --summary-only
```

Pass `--config` (with your `NIGHT_*` environment) so frames are split into day and night
by your location rather than a clock fallback. Group cameras by model yourself in the JSON
output. Delivery path, lens and alert volume come from the sent log (`sent-log/index.jsonl`:
the `path` field, the lens in the caption, one record per delivered alert); the threshold
sweep is `score >= t` over the labeled frames in the JSON/`labels.jsonl`.
