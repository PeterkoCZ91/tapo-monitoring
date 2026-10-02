# Measurements

Aggregate numbers from one real deployment, published so you can judge what the project
does in practice and compare your own installation. Nothing here identifies a site, a
camera or a person: cameras are named by model only, dates are given as a range of months,
and no coordinates, polygons or places appear.

Treat every figure as an example from one installation, not as a benchmark of the
software. [Limitations](#limitations) lists what the numbers cannot tell you.

## What was measured and how

- **Deployment.** One owner, several sites, a Raspberry Pi per site, one shared local
  scoring service (YOLOX, `person` class). Camera models: C560WS (pan/tilt, outdoor),
  C545D (dual lens) and C260.
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
| C560WS (four cameras) | 2,345 | 62 of 1,802 (3.4 %) | 44 of 542 (8.1 %) |
| C545D | 98 | 22 of 84 (26 %) | 0 of 14 |
| C260 | 120 | 10 of 88 (11 %) | 1 of 31 (3 %) |

Per C560WS camera the false-alarm rate ranged from 1.9 % to 8.1 % (decided sent frames
from 34 to 1,064). The C545D and C260 figures come from only a few weeks of data and
small n, and from scenes with a lot of non-person motion, so the difference to C560WS may
be the scene, not the model.

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

| Path | C560WS (four cameras) | C545D | C260 |
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
- **Threshold fitted on the same data.** The best-threshold result is in-sample.
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
# accuracy per band and per camera, incidents, first-alert delay per path
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
output.
