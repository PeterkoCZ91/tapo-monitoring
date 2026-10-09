# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- Sampler lateness is logged: a frame that runs more than 10 s after its due time logs
  `sampler <cam> frame N late by X s` (scheduling is unchanged). The card path's caption names
  the frame time (`frame HH:MM:SS`) when it is the live grab that stands in for a failed card
  read; a frame cut from the card keeps the event time.
- With `crop_from_native`, the sent log now keeps the native frame the crop was cut from, cut
  to at most 2560 px wide at a lighter JPEG quality (about 0.5 MB for a noisy 4K frame
  against about 4.7 MB native) instead of the 1280 px delivery copy, so a small crop can be
  reviewed against the whole scene. If the reduction fails, the delivery copy is archived.
- Alerts that went out unscored are now visible and less likely: when the scorer is down,
  a sampler frame is held (up to the last frame of its group) instead of sent if the group
  already holds scored low frames (`scorer.on_error: hold`, default; `open` restores the old
  passthrough). An unscored send is captioned `(unverified)`, audited with
  `reason=scorer_unavailable degraded=true` and counted as `sent_unscored`. A sampler caption
  names the frame time (`frame HH:MM:SS`) when it is more than a minute after the event, and
  the sampler no longer waits for the camera control client for RTSP grabs.
- `crop_min_px` (default 400, 0 = off): with `crop_from_native`, a crop narrower than this is
  sent as a second "zoom" photo after the whole frame instead of as the alert. An unscored
  frame is never cropped.
- Scorer: a request whose body is shorter than its `Content-Length` is answered 400 and counted
  as `short_body`; the `inference_error` log names body length, `Content-Length` and the JPEG
  start marker; request latency now includes the upload; the client retries a timed-out, 400 or
  500 request once (worst case about twice `scorer.timeout`).
- `tools/label_selection.py` builds a labeling dataset (images, `index.jsonl` with optional
  per-model `boxes`, `teacher.jsonl`, selection report) from two shadow logs and their
  `disagree/` directories: three-model disagreement, small persons and random controls,
  skipping frames already labeled by hand (teacher labels do not count); `--dry-run` reports
  only. See "Build a labeling set from two shadow targets" in `docs/operations.md`.
  `--image-dir` (repeatable) takes images for frames missing from `disagree/` from dataset
  directories such as `sent-log`/`review-log`, so small persons and controls get pictures.
  The controls (`--control`, default 20 each) are reserved within `--limit` before the
  disagreeing categories, and `--source-share` caps only the rest, so a busy source cannot
  crowd them out; `threshold` and `small` (at most `--small`, default 20) are picked before
  `strong`, which takes the rest. Records get `camera` when a dataset index names the camera
  of the frame's source. Each `index.jsonl` record also lists `models`, the models that ran on the
  frame; the report counts selected frames per image source type and log rows dropped for
  lacking `frame`.
- Opt-in box labeling in `tapo-monitor label`: an `index.jsonl` record with a `boxes` list
  (`[x1,y1,x2,y2,score,model]`, normalized) shows the boxes colored by model and score; click
  to cycle person / no person / unmarked, drag to draw a missed person, `Tab` / `p` / `n` /
  `x` / `Enter` from the keyboard. Verdicts land in a `boxes` field of the `labels.jsonl` line
  beside the unchanged whole-frame `label` (suggested from the boxes, never overriding a typed
  one). `label-stats` summarizes box labels per model and computes per-model recall of
  hand-drawn persons (incl. small ones) and false boxes, tunable with `--box-score` and
  `--box-iou`. An optional `models` list in the index record (models that ran, even with no box) decides which models count for recall; without it the old rule applies and the report marks recall as possibly overstated. Frames without `boxes` behave as before. Czech texts included.
  Boxes are thin outlines drawn just outside the object with the tag on the selected box
  only, so a distant person stays visible; hold `h` to see the picture without any boxes.
- Optional second shadow target in the scorer service (`TAPO_SCORER_SHADOW2_URL`, `_LOG`, `_NAME`,
  `_TOKEN_FILE`/`_TOKEN`, `_QUEUE`, `_TIMEOUT`, `_DISAGREE_DIR`, `_DISAGREE_MAX`) with its own
  queue, worker and log (same row format plus `shadow_name`) and a `shadow2` block in
  `/metrics`, so two candidates can be compared at once. Off by default; the first shadow, the
  replies and the `/metrics` keys are unchanged. `tools/shadow_report.py` accepts a second log,
  reports both and compares them on matching frames.
- `tools/provision_shadow_scorer.sh` builds or updates a shadow scorer service
  (`tapo-scorer-shadow-NAME.service`, rendered from `systemd/tapo-scorer-shadow.service.in`)
  with one command: validated arguments, detector settings as `Environment=` lines, optional
  `--token-file` (path only), idempotent `--dry-run` that reports `changed=0`, and a success
  check on the HTTP status of `/health`. See "Provision a shadow scorer" in `docs/operations.md`.
- Cameras reading `snapshot_source: recording` can tune the follow-up timing with
  `recording_early_span` (first look, default 24 s, 6-60) and `recording_ready_margin`
  (default 15 s, 5-60); the defaults equal the previous fixed values. The `frame selection`
  log line gains `offset=<s>`, the picked frame's seconds from the event start (log only).
- Shadow log rows gain `frame`, the first 16 hex characters of the SHA-256 of the frame body,
  so a row can be matched to a saved frame. Only written when the shadow log is enabled; older
  logs stay readable by `tools/shadow_report.py`.
- The fleet snapshot's `recorder` aggregate ranks `degraded` above `ok` above `unknown`: a
  camera with no recording directory (or no recording root) no longer masks a checked camera,
  so the digest stops reporting "recorder unknown" for an ok + unknown fleet.
- The scorer's `scoring failed: inference_error` warning now names the exception class and
  the normalized source id (no traceback, no frame content); metrics and the 500 reply are
  unchanged.
- Shadow log rows gain `primary_boxes` and `shadow_boxes` (person boxes as normalized
  `[x1, y1, x2, y2, score]`, best first, at most 10, 3 decimals; left out when a reply carries
  none), so a frame-level label can be traced to the object a detector hit. The host reply and
  `/metrics` are unchanged. `tools/shadow_report.py` adds "box clusters" (static box centres
  repeated in at least 5 frames of one source, per model) and, for disagreeing frames, whether
  the two models' boxes overlap (IoU >= 0.3); rows from the old format still work.
- Ensemble mode: `TAPO_SCORER_ENSEMBLE_TRUST` lets the scorer trust the second detector from a
  lower score than the host threshold. A shadow score in `[TRUST, HOST)` is remapped linearly
  into `[HOST, HOST + BAND)` (`used: shadow_marginal`, boxes and score list from the shadow),
  so hosts accept it at their usual threshold and hold band. New `TAPO_SCORER_ENSEMBLE_HOST`
  (the old `_HIGH` stays as an alias) and `TAPO_SCORER_ENSEMBLE_BAND` (0.10); TRUST defaults to
  HOST, which keeps the previous behaviour. Validation is now `0 < LOW < TRUST <= HOST <= 1`.
- Scorer service: partially decoded video frames (flat grey with a few decoded blocks) are
  recognised on a small luma copy and answered with zero scores, empty boxes and
  `artifact: true` without inference; `/metrics` counts them as `artifact`;
  `TAPO_SCORER_ARTIFACT_FILTER=0` turns it off. See `docs/operations.md`.
- `tools/recording_audit_estimate.py`: stratum-weighted estimate of missed persons and false
  alarms per 1000 frames for several decision rules from a partly labeled score table, with
  seeded bootstrap intervals, day/night split, an independent completeness check and bias
  warnings; `--lang en|cs`, `--json`. See `docs/operations.md`.
- `tools/recording_audit_estimate.py`: visit-level estimate (`--visit-gap`, default 30 s) of
  how many labeled person visits each rule catches with at least one frame, with Wilson 95 %
  intervals, next to the per-frame estimate. The dataset index of `recording_audit.py
  dataset` now carries `frame`.
- Optional ensemble mode of the scorer service (`TAPO_SCORER_ENSEMBLE=gate`, default off, needs
  the shadow URL): each `/score` runs the primary inference and the shadow service in parallel
  and returns one combined `person` (shadow >= 0.45, or primary >= 0.45 confirmed by shadow
  >= 0.12, otherwise capped just below 0.45), with boxes from the deciding model and an
  `ensemble` block in the reply. A failing, unauthorized or slow shadow
  (`TAPO_SCORER_ENSEMBLE_TIMEOUT`, 2 s) yields the primary reply unchanged. `/metrics` gains
  `ensemble` counters; the shadow log and disagreement frames keep working. See
  `docs/operations.md`, "Ensemble mode".
- `tools/recording_audit.py`: estimates detection completeness from the continuous local
  recordings instead of the kept frames. `plan` draws a seeded day/night sample of 10-minute
  windows and reports the bytes to copy, `score` cuts a frame every N seconds and scores it with
  one or two scorer services (rate-limited, token sent to remote scorers only) into a
  `scores.jsonl` with `cand`/`lowrand`/`lowrest` strata, `dataset` builds a labeling dataset
  of the top candidates and a random low-score sample. See `docs/operations.md`.
- `tools/shadow_report.py`: reads the shadow-run log (counts, errors, timing, 2x2 agreement and
  disagreement breakdowns by hashed source, hour and score band, Pearson/Spearman) and, with
  `--labels` and `--disagree-dir`, compares candidate decision rules on hand-labeled
  disagreements; `--lang en|cs`, `--json`. See `docs/operations.md`.
- `tools/bench_detectors/`: a benchmark for comparing person detectors on your own hand-labeled
  frames (`manifest.py`, `run.py` with RF-DETR, HuggingFace DETR-family, torchvision Faster R-CNN,
  YOLOX and scoring-service adapters, `evaluate.py` with AUC, false alarms and misses at fixed
  thresholds and at "at most 2 % of persons missed" with seeded bootstrap intervals, day/night,
  a cascade simulation and an export of disagreement frames for labeling). Dependencies live in
  the tool's own `requirements.txt`, not in the package extras. The first published results are in
  `docs/measurements.md`, "Detector comparison".
- Optional shadow run of a second detector inside the scorer service (default off, replies
  unchanged). With `TAPO_SCORER_SHADOW_URL` and `TAPO_SCORER_SHADOW_LOG` set, every scored
  frame is queued (8 deep by default, `TAPO_SCORER_SHADOW_QUEUE`; dropped and counted when full) after the reply and sent to another
  scorer service with a timeout; one JSON line per frame records both scores, durations and the
  error class, never the URL, token or image. `TAPO_SCORER_SHADOW_DISAGREE_DIR` keeps frames on
  which the models disagree (one at 0.45 or more, the other below 0.25), capped by
  `TAPO_SCORER_SHADOW_DISAGREE_MAX`. `/metrics` gains `shadow` counters. See
  `docs/operations.md`, "Shadow run of a second detector".
- `scorer.ignore_boxes` (off by default): rects (fractions of the frame) of a static box the
  model reports as one big person, e.g. a swing roof; a person is dropped only when its box
  matches a rect in size and place (IoU >= 0.75, counted in `ignored_boxes`), so people
  standing in front of the object, or large ones elsewhere, still count (an `ignore_zones`
  rect or a low `max_person_box` would lose them). `scorer.ignore_boxes_mode: observe` drops
  nothing and audits `would_drop_ignore_box boxes=N` next to the send. Config is validated
  like `ignore_zones`; `observe` needs `ignore_boxes`.
- `scorer.max_person_box` (off by default): a fraction of the frame; a person whose box is
  larger than it in both width and height is dropped like an ignore-zone person (counted in
  `oversized_persons`), after `ignore_zones` and before `alert_zones`. Catches the occasional
  whole-frame person box; use 0.9 or more, mind the pan/tilt lens of dual-lens cameras.
- Optional shared token for the scorer service (default off, behaviour unchanged). With
  `TAPO_SCORER_TOKEN_FILE` (a 0600 file, preferred) or `TAPO_SCORER_TOKEN` set on the service,
  `/score` and `/metrics` need `Authorization: Bearer <token>` (constant-time compare, 401
  without detail) while `/health` stays open; a bad token file stops the service at start.
  Clients send the header when `scorer.token_env` names a variable holding the token
  (`<NAME>_FILE` may name a file): live scoring, sampler and SD paths, scene probe, shadow scan,
  the daily digest's `/metrics`, `label --scorer-token-env`, `tools/replay_zones.py --token-env`,
  `tools/fleet_status.sh` and `tools/check_scorer_rollout.sh`. The token is never logged or
  stored; a 401 is logged once as a configuration error and does not count towards scorer
  failover. See `docs/operations.md`, "Shared token", and `docs/configuration.md`.
- The scorer service can run more than one detector family: `TAPO_SCORER_ARCH=yolox`
  (default, behaviour unchanged) or `detr` for NMS-free DETR-style ONNX exports with
  `logits` and `boxes` outputs (RF-DETR, D-FINE, RT-DETR), configured by
  `TAPO_SCORER_DETR_PERSON_ID` (COCO80 or COCO91), `TAPO_SCORER_DETR_ACTIVATION`
  (`softmax`/`sigmoid`), `TAPO_SCORER_DETR_NORMALIZE` and `TAPO_SCORER_INPUT_SIZE`; an
  unknown value stops the service at start. `/score` is unchanged; `/health` and
  `/metrics` gain `arch`. See `docs/operations.md`, "Swapping the detector model".
- `tools/soak_sim.py`: an accelerated soak that runs the real `daemon.loop_step` against fake
  cameras on a fake clock for N simulated days (`--days`, `--seed`, `--cameras`) with generated
  people, motion, rain, camera/scorer/network outages and restarts, and `--features` for the observe
  modes, `hub_clip_worker`, host failover and scorer failover. Reports RSS growth, threads, state-file
  sizes, exceptions and slow ticks, and exits non-zero past `--max-rss-growth-mb`/`--max-threads`
  and the other limits. See `docs/operations.md`, "Accelerated soak simulation".
- `tools/soak_sim.py --wrapper` runs through `daemon.tick` (stall watchdog, injected failing
  ticks, the `/status` view built and timed every few ticks); file counts of the sent log,
  review log, `panlimit-log` and `sdjob_*` temp dirs are tracked with
  `--max-files-growth-per-day`, retention is checked against simulated file times, and
  `--inject filesleak` plants a leak. CI runs a short three-seed soak in a parallel job.
- `tools/soak_report.sh`: one read-only report per host for a soak test (service state and
  package fingerprint, starts, error lines, alerts per camera and path, first-alert
  median/p90, drop/hold/cooldown and `would_*` counts, memory and CPU), with `--user`,
  `--json` and `--baseline` deviation marking. See `docs/operations.md`, "Soak report".
- Observe mode for the opt-in alert features (default `enforce`, behaviour unchanged):
  `scorer.alert_zones_mode`, `sampler.loiter_mode` and `scorer.vehicle_mode` accept
  `observe`, which computes the feature and writes an audit line instead of acting:
  `would_drop_outside_alert_zone` (people inside/outside, filters nothing), `would_loiter`
  (with `minutes`, no notice sent) and `would_send` with `etype=vehicle` (nothing sent, no
  cooldown armed). Each needs its feature enabled.
- `tapo-monitor label-stats --camera-alias OLD=NEW` (repeatable): counts the records of a
  renamed camera under its new name in the per-camera, incident and day/night tables.
- `scorer.url` may be a list of candidate scoring services (off by default; a string works
  as before). After 3 consecutive transport failures (timeout or connection, not an HTTP 4xx
  on an image) the monitor tries the other candidates with `GET /health` and switches to the
  first that answers; while on a non-primary it checks the primary's `/health` every 5
  minutes and switches back when it recovers. One `scorer switch` warning and one audit line
  (index only); the choice is kept in the health state file. The URL is read through a
  single `scorer.active_url` / `scorer.score`.
- `host` may be a list of candidate addresses (off by default; a string works as before).
  After two failed ping passes the monitor tries the other candidates with a TCP connect to
  the API port, switches to the first that answers as the same camera (MAC from
  `getBasicInfo`, learned once), reopens the Tapo session on it, and logs the switch once
  with an audit line. The active address is read through a single `camera.active_host`.
- `docs/measurements.md`: anonymized accuracy, miss, first-alert latency and replay figures from
  one real deployment, with their limits and the commands to reproduce them.
- `scorer.lens_ignore_zones` (off by default): `ignore_zones` per lens of a dual-lens camera
  (`wide`, `pan_tilt`; only for an `event_profile` with a pan/tilt lens). A frame with a
  known lens is scored with the global zones plus its lens's zones, a frame without one
  (SD card, recording) with the global zones only.
- `tapo-monitor label --scorer-url URL` (off by default): draw the person boxes the scoring
  service finds on the frame being labeled (red from 0.45, yellow from 0.10, with scores).
  A scorer failure leaves the page working without boxes.
- `tapo-monitor label --lang cs|en` (default `en`): Czech or English labeling page and
  `/stats`; the texts are kept in one dictionary per language.
- `hub_clip_worker` (off by default): download hub clips and cut their frames on a
  background thread per hub instead of the main loop. Queued clips are saved with the
  runtime state before the hub cursor moves past them, survive a restart, and fall back to
  a live frame when a job fails or overruns.
- `scorer.alert_zones`: polygons (fractions of the frame) inside which a person must
  stand, by the bottom centre of its box, to count. Refused on pan/tilt-lens profiles; a
  camera that tracks or has a pan limit loads with a warning, because its framing moves.
- `sampler.loiter_frames` / `loiter_miss_tolerance` (off by default): one "someone has
  been here for about N min" notice per group when a person is on K consecutive follow-up
  frames; with `alert_zones`, only people inside a zone count.
- `scorer.alert_vehicles` / `vehicle_threshold` (off by default): a separate vehicle alert
  with its own cooldown from the live poll, the sampler and the SD/recording follow-ups
  (never when the window shows a person, never twice for one event), zone-checked when
  zones are set. The scoring service now returns `vehicle`, `vehicle_scores` and `vehicle_boxes`;
  update the service before the daemons.
- `tools/replay_zones.py`: offline tool to see where people's feet land, test polygons
  against saved frames and replay loitering settings. See
  [watch zones](docs/watch-zones.md).
- Passive monitoring-host Wi-Fi snapshots alongside network probes: signal, link
  rate, power saving and available retry/failure counters with reset-aware deltas.
  The digest labels the watcher or gateway radio and unsupported observations.
- Enabled ledger DB/WAL sizes in the daily digest, measured without SQLite operations.
- Generic journal and syslog size/age policy guidance in [log retention](docs/log-retention.md).
- Network history now caches a 24-hour digest summary with measurement coverage and
  the worst UTC hour, without extra probes. SD transfers log their start and finish,
  including failures, so operators can compare transfer intervals with network history.
- The review digest reads that network cache locally, including a configurable
  gateway cache path, and marks stale or invalid measurements as unknown.
- Standalone network watcher (`tools/network_watch.py`): five ICMP samples per target
  about every two minutes, with packet loss, mean/p95/max RTT and jitter summaries.
  Each host measures its default router separately from configured cameras or hubs;
  sleeping battery cameras are excluded from the ping target list. Daily history is
  bounded by 30 days and 64 MiB, and a private SSH inventory provides a fleet summary.
  Probe failures, missing history and old measurements remain visible. Includes
  optional systemd user units and a cron-compatible quiet mode; no camera login is
  needed. See [network measurements](docs/network-watch.md).
- `sd_early_span` (camera card, 6-30, default 18): length of the `sd_early_look` first
  window; a smaller one is due and downloaded sooner.
- `scorer.ignore_zones`: rects (fractions of the frame) whose people do not count, for a
  static person-shaped object that "confirms" every event nearby. The scoring service now
  returns `person_boxes`, one box per entry of `person_scores`; the daemon removes the
  people at least 80 % inside a zone and scores the best remaining one.
- `sampler.keep_sampling_on_defer` (off by default): a camera-confirmed person whose live
  frame is below the threshold is still queued for the SD/recording follow-up and still
  arms the cooldown, but the burst stays open: the sampler keeps sampling it, and a
  sampler or later live frame whose person score reaches the threshold may pass the
  cooldown, so the person arrives within seconds instead of with the follow-up minutes
  later. Any other frame (nobody in it, a tamper/pet photo of nobody, the scorer down)
  stays under the cooldown as before. Once a person photo of the passage is delivered the
  queued follow-up is dropped (`reason=passage_already_alerted`); when none is, it sends
  as before. Needs a local scorer. A replay of three cameras' journals traced lost bit-19
  people to that defer silencing the rest of the burst.
- `scorer.person_bit_skips_hold` (off by default): a live motion frame whose event carries
  `events_1` bit 5 skips the corroboration hold and sends at `>= threshold`, as the
  sampler already did for such a burst. Routing every bit-5 event as a person was replayed
  on three cameras' journals and rejected: it lost more alerted passages than it rescued.
- Opt-in outbox for alerts Telegram did not take (`outbox:` block, off by default). A
  failed real send is kept on disk (frame + JSON sidecar, keyed by incident) and, once
  Telegram answers again, delivered late: one outage summary per camera, then the photos
  that showed a person (`⏳ zpožděno o …`, capped by `max_photos`), the rest to the review
  log. Unscored frames are scored at drain time (other than a camera `person` event, an
  unscored one waits up to six hours for the scorer); an incident delivered by any other
  path, or deliberately not sent by the SD follow-up or sampler, is removed first; late
  photos (sent-log `path: outbox`) arm no cooldown; each tick drains a bounded amount. Before,
  the in-memory retries gave up after ten minutes, so a night-long internet outage lost
  every alert of the night.
- Opt-in early look for camera-card follow-ups (`sd_early_look: true`, off by default).
  A card window of 24 s or more is read in two parts, as the local recording already is:
  the first 18 s from the start of the camera's recorded segment, due about 87 s after
  the event instead of 105 s and a shorter download, and the rest of the window only
  when that look shows nobody, at the time the whole window was read before. The card
  download now honours an offset into the segment (the rest window starts 18 s in rather
  than at the segment start again). On two sites the photo a card follow-up sent lay in
  the first 12 s of its window in about 90 % of cases. With it on, a failed card segment
  lookup is tried once more after 5 s (the C560WS sometimes answers a fresh session's
  first calls with `-40214`, and the read then fell back to a guessed window that comes
  back empty). An early look that read no frames is followed by a read of the whole
  window; one that showed frames of nobody followed by an empty rest is dropped, never
  replaced by a live grab.
- One `SD read` log line per camera-card read: window, offset, segment lead, bytes, and
  the connect, download and extraction seconds.
- `sd_fresh_guard` (camera card, default 60, 10-120): the seconds a card window's end
  must be in the past before pytapo downloads it. Every card due time follows it. The
  default is pytapo's own guard; lower it only on a camera where a trial shows complete
  clips.

### Changed
- Docs: `events_1` bit 5 is the camera's person class (alarm code 6), not a PIR sensor, and
  bits 3 / 7 / 8 are named line crossing / vehicle / pet, corrected from the Tapo app's own
  list of event codes.
- Daily background retention compacts expired archive metadata, cleans media even
  without new events, and prunes the event ledger in bounded transactions.
- The labeling page reads the physical key (`code`), so `1`/`2`/`3` and `u` work on a layout
  whose top row types other characters without Shift (for example a Czech keyboard).
- The camera-card segment lookup error is printed with the read's output (the journal
  shows it when a read comes back empty); before it was swallowed.
- Docs: `vedio_type` is the clip's trigger code (`2` motion, `6` person, `8` vehicle, `9` pet),
  not "event clip"; added the reported `searchVideoWithUTC`/`player_id` note for newer firmware.

### Fixed
- Labels of a `recording_audit.py dataset` now pair with the `recording_audit_estimate.py`
  score table by window and index, which fixes the "labels matched no frame" warning.
- Hub clip temp dirs (`sdjob_*`) no longer outlive their frame when a failed alert is
  retried (sent, given up, too old, frame gone) or when a clip result arrives after its
  queue entry was settled; they used to stay until the next daemon start.
- Fleet digest health now distinguishes fresh verified camera health from cumulative
  self-heal refusals since daemon start. A health observation before a new refusal or
  a stale/missing observation cannot certify recovery.
- One visit, one message, also behind a motion alert: a person SD/recording follow-up is
  now skipped (`reason=sd_within_cooldown`) when the camera delivered, within the
  cooldown, a photo whose person score reached the camera's threshold, whatever its
  event type. Before, only a confirmed person alert counted, so a sampler motion photo of
  a person (0.69) was followed 36 s later by the card photo of the same second (0.79).
  A motion photo of nobody, or an unscored one, still lets the person on the card
  through. The fact is recorded at delivery and persisted with the cooldowns; state
  written by an older version behaves as before. On the busiest site, 32 of 921
  delivered person follow-ups in 80 days were such duplicates, and none followed a
  motion photo of nobody.

## [0.8.1] - 2026-09-27

### Added
- Packaging for PyPI: `pip install tapo-monitor` (extras `mqtt`, `scorer`, `onvif`). The
  package metadata now carries an SPDX license with the license file, classifiers and
  project links, the README uses absolute links so they work on the PyPI page, and the
  wheel holds only the `tapo_monitor` package. A published GitHub release builds the
  sdist and wheel and uploads them through PyPI Trusted Publishing
  (`.github/workflows/release.yml`, no token in the repository); the one-time setup is in
  `docs/releasing.md`.

- Container images: a multi-stage `Dockerfile` whose default target is the monitor
  (Alpine, ffmpeg, `[mqtt]` extra, ~240 MB) and whose `scorer` target is the YOLOX scoring
  service (Debian slim, `[scorer]` extra, model mounted at `/models`, port 8766, health
  check on `/health`). Both run as a non-root user and keep all state under a `/data`
  volume (`XDG_STATE_HOME`, `STATE_DIR` and `HOME` point there). Ships with
  `docker-compose.example.yml`, `tapo.env.example` (variable names and placeholders only)
  and `.dockerignore`. A `Docker images` workflow publishes both for amd64 and arm64 to
  the GitHub Container Registry on every published release. See `docs/docker.md`.

## [0.8.0] - 2026-09-27

### Highlights
- **Home Assistant over MQTT:** an opt-in, publish-only bridge with MQTT discovery. Each
  camera becomes a device with person/motion sensors, reachability, privacy mode,
  "detection off", health and the last alert, ready for automations. Tested against a live
  Home Assistant.
- **Support for new cameras from their owners:** `tapo-monitor report` writes an
  anonymized report of what a camera model answers; shared through the new issue form,
  each accepted report becomes a regression test.

### Added
- Opt-in MQTT bridge with Home Assistant discovery (`mqtt:` block, extra
  `pip install 'tapo-monitor[mqtt]'`). Each camera appears as a device with person and
  motion binary sensors, reachability, privacy mode, "detection off", Digital Twin health,
  last alert time and score, and — only with `publish_images: true` — the last alert photo;
  the daemon publishes its own availability through the MQTT last will. Publish-only (no
  subscriptions, no commands), retained state published on change, its own thread with a
  bounded drop-oldest queue so a slow or absent broker never delays the main loop, and a
  full resync on every reconnect. Without the block nothing is imported and nothing
  changes. See [docs/mqtt.md](docs/mqtt.md).
- `tapo-monitor report --host IP`: an anonymized, shareable JSON report of what a camera
  model answers, so new models can be supported from community reports. No `cameras.yaml`;
  credentials from `TAPO_USER`/`TAPO_PASSWORD` or a prompt; one session with the API
  deny-list on; read-only twin getters (per lens on a multi-lens camera) with the camera's
  error code per getter; recent events with relative times; `--watch SECONDS` polls
  `getEvents` while the owner walks past; `--rtsp` ffprobes `stream1`/`stream2`. Values
  pass an allow-list (everything else is `<redacted>` and listed), and a self-check refuses
  to write a file that still contains an IP, MAC, long hex id or e-mail address.
  `report --summarize FILE` digests a report offline. See `docs/camera-reports.md`.
- A *Camera report* GitHub issue form, and `tests/fixtures/cameras/`: every accepted
  report there is a contract test (schema, leak self-check, equal to the current
  sanitizer's output, events through the real normalizer and classifier, `--summarize`).
  First fixtures: C260 (fw 1.2.3) and C545D (fw 1.1.7, converted from the earlier
  capture). `report.resanitize` re-cleans an existing report offline.

### Changed
- The report's allow-list keeps more harmless settings when they look like an enum or a
  number: image scene and full-colour modes, codec and bitrate defaults, stream quality
  names, OSD font/date display, exact SD byte counts, alert type names and boolean flags.
  Identity fields, schedule times, the card's record start time and OSD label text stay
  withheld. A getter answering a bare value (`getLensDistortionCorrection`) is kept when
  its name is on the list, and a value an earlier sanitizer layer withheld is now listed
  in `redacted_keys`. The self-check no longer takes a byte count such as `115203047424B`
  for a hex id.
- Twin probes that fail now keep the camera's numeric `error_code` (never the message).

## [0.7.0] - 2026-09-27

### Highlights
- **The owner hears when a camera stops watching:** `privacy_notice` and `detection_notice`
  send one Telegram message when privacy mode, motion or person detection is switched on or
  off in the app; `follow_app_notifications` silences alerts while the app's own
  notifications are off.
- **One visit, one message:** a card follow-up no longer lands a second behind a live alert
  of the next passage.
- **Dual-lens C545D, both lenses kept healthy:** the self-heal reads and repairs each lens,
  captions name the lens and the number of people, and the calls that take the camera's API
  down are refused in the client.
- **Collect-only cameras:** `telegram_alerts: false` records alerts without sending them,
  for a new site that should gather data before it notifies.
- **A file-descriptor leak fixed** that cut one host off its camera and Telegram after a day.

### Fixed
- Dual-lens self-heal: on a C545D the repairs sent without `chn_id` reached the wide lens
  only, so the pan/tilt lens's person detection, motion sensitivity and vehicle-off were
  never repaired. With `event_profile: c545d` the control pass now reads motion, person and
  vehicle detection per lens and writes (`chn_id`) only to a lens that drifted; motion
  sensitivity goes as the number alone. `detection_notice` reports a lens that is off on
  its own and names it. Single-lens cameras send exactly the calls they did.
- File-descriptor leak: every pytapo client owns an asyncio loop that pytapo never closes, and
  the daemon replaces its clients on each control pass. A client still referenced elsewhere
  kept its loop (an epoll fd and a socket pair); one host hit the 1024 limit after 23 hours
  and could no longer reach its camera or Telegram. Dropped clients are now closed explicitly.
- One visit, one message: a person SD/recording follow-up whose read came back after
  another passage of the same camera had alerted live arrived a fraction of a second behind
  that alert. It is now held to the confirmed cooldown: skipped, audited as `sd` `cooldown`
  (`reason=sd_within_cooldown`) and kept in the review log. Three such pairs in 997
  delivered person follow-ups across the fleet.

### Added
- Alert captions name the lens of a dual-lens camera that took the photo
  (`· wide lens` / `· pan/tilt lens`) and, when the scorer found two or more people at the
  camera's threshold, how many (`· 2 people`). The scorer response gains `person_scores`
  (one confidence per person, full frame, after non-maximum suppression); a daemon talking
  to an older scorer simply shows no count.
- `privacy_notice` (per camera): a Telegram message within one control pass when a camera
  enters or leaves privacy mode, persisted across restarts; the privacy switch is now read
  on static cameras too when it is set.
- `telegram_alerts: false` (per camera): data-collection mode — alerts are decided,
  recorded and counted as live, but no photo is sent; system notices still go out.
- API deny-list: every camera client refuses `checkDetectEventState` and
  `getInfLampCapability` (each took the local API down for ~12 s on a C545D), also inside
  `multipleRequest` batches and hub envelopes; twin probes skip them as `denied_method`.
- `detection_notice` (per camera): one Telegram message when motion or person detection
  is switched off in the app and one when it is back on. Person detection is read before
  the self-heal re-enables it, so a switch flipped off is reported as one "switched it
  back on" message, at most once a day per camera.
- `follow_app_notifications` (per camera): while the Tapo app's notification switch
  (`getMsgPushConfig`) is off, alerts are recorded but not sent; an unreadable switch
  never silences.

## [0.6.0] - 2026-09-25

### Highlights
- **Quality per visit, not per frame:** `label-stats` groups frames into incidents and
  reports missed visits and the first-alert delay per delivery path.
- **A labelling workflow:** frame collection across hosts, a teacher model that pre-labels
  the easy frames, a local labelling page, and a small sample of below-threshold frames.
- **Opt-in alert tuning:** `scorer.night_threshold` and a `sampler.hold_expiry` policy,
  both replayable with `tapo-monitor replay --compare` before they go live.
- **Faster follow-ups:** on the busiest site the first recording follow-ups after the
  change left a median 56 s after the event (119 s before); frames are scored concurrently
  and while being extracted; SD and recording reads run off the main loop.
- **Better photos:** `sd_frame_pick: largest` picks the frame where the person is closest.
- **Operations:** log-disk telemetry and low-space warning, counterfeit SD card warning,
  automatic rollback of a crash-looping deploy, user-unit deploys.
- **New hardware:** the dual-lens Tapo C545D (`event_profile: c545d`), and a public
  [local API reference](docs/tapo-local-api.md).
- **Upgrade note:** unknown configuration keys now fail at startup; every new behaviour
  is opt-in and defaults keep 0.5.0 behaviour.

### Added
- Dual-lens Tapo C545D support. Its `getEvents` entries carry `events_1` per lens under
  `chn_events`; every polled event is now normalized to a top-level `events_1` (OR of the
  lenses) plus `channels` before the watermark and classification, and audit lines show
  `channels=1,2`. Single-lens events are passed through untouched. A new per-camera
  `event_profile` (`default` | `c545d`) picks how the bits are read: on a C545D
  `alarm_type` 6 / bit 5 (value 32) is a person (observed on 8 person walks; the model has no PIR),
  while `default` keeps it as the C560WS PIR. With `c545d`, an event on the pan/tilt lens
  keeps the scheduled preset recall and the pan-limit guard off that lens for 180 s after
  the event (new motion-arbiter reason `linkage`), because the firmware turns it after
  the person itself, and the digital twin reads the lens layout, the linkage state and
  both lenses' detection switches (`chn_id`). Opt-in `lens_pick_stream` grabs the other
  lens as well on a pan/tilt event and keeps the frame with the larger subject. See
  `docs/capabilities.md` §7.
- The digital twin marks storage degraded, with a "counterfeit or defective SD card"
  warning in the twin state and the journal, when `getSDCard` reports `detect_status:
  dilatant_suspect` (fake capacity), on any model.
- Per-camera `sd_frame_pick` (`sharpest` | `largest`, default `sharpest`) and
  `sd_dense_start` for the SD and recorder follow-ups. `largest` sends the
  above-threshold frame with the biggest person box. It skips a frame more than 3x
  blurrier than the sharpest one and keeps the sharpest unless the box is at least 1.25x
  larger. Without a box for every frame it picks the sharpest. `sd_dense_start` adds a
  frame every 2 s over the event's first 12 s; left unset, it follows `largest`. The
  old frames are all still taken. Both constants come from 45 recorder events: with the
  guards, 15 of the 28 events that had a subject changed their pick, the subject's box
  height grew by a median of 1.32x, and no pick got smaller. Candidates above 3x blur
  were smeared or cut off at the frame edge. The SD download subprocess takes the dense
  window as two optional trailing arguments, so the argv without them is unchanged.
- Sent-log index records name the delivery path that sent the frame in `path` (`live`,
  `sampler`, `sd`, `hubpoll`, `hubpoll_retry`, `hold_rescue`, `hold_expiry`), and
  `label-stats` / `/stats` report, per path of the first delivered frame, how many alerted
  incidents it delivered first and their delay from event start (n, median, p90), under a
  new `first_alert` key in every incident block. Older records count as `unknown`.
- Sent-log and review-log index records of a camera event's frame (every delivery path, holds,
  sampled and hub-clip drops) carry `incident` and `event_start`, so one visit's frames group
  by ID. Frames with no camera event omit both.
- `sampler.hold_expiry` (`off` | `observe` | `send`, default `off`) and
  `sampler.hold_expiry_min_score`: a held marginal motion frame whose corroboration never
  came can now be sent from the review log when its group closes (`hold_expiry_send`), or
  only audited as `would_send`/`hold_expiry_observe`; the pan-limit rescue still goes first.
  The floor defaults to the threshold in force; the policy needs
  `scorer.motion_send_threshold` and warns at startup without `TAPO_REVIEW_LOG_DIR`. The
  group now keeps its best-scoring archived held frame rather than the last one.
- `scorer.night_threshold`: per-camera scorer threshold applied while the camera's night is
  on (astral night with its `schedule` applied); unset keeps `threshold` around the clock.
  `tapo-monitor replay` and the shadow scan apply it by the time each event or frame was
  handled, so `replay --compare` can evaluate a night-only change.
- `tapo-monitor label` / `label-stats`: local frame-labeling page over collected sent/review-log
  frames (append-only `labels.jsonl` keyed by image SHA-256) and false-alarm/miss/threshold stats.
  Binds `127.0.0.1` by default. See `docs/labeling.md`.
  With `--config cameras.yaml` both report the best threshold by day and night (overall and per
  camera, marked "too few labels" below 20 decided frames or 5 of either class), and misses
  are split into held frames and sampled drops. Frames are also grouped into incidents (by
  incident ID; older records by camera and a 150 s gap), reporting incidents with a person,
  how many were alerted, the missed ones, and the delay from event start to the first
  delivered alert (median, p90).
- The review log also keeps a random sample of below-threshold frames (live, sampler and
  SD/recording drops; `TAPO_REVIEW_DROP_SAMPLE`, default 5 %, at most
  `TAPO_REVIEW_DROP_MAX_PER_HOUR` per camera and hour, default 6), so possible misses keep
  reaching the labelling queue. Records carry `sample_rate`; the digest counts them apart.
- `tapo-monitor autolabel`: a larger teacher model labels the frames it and the
  production scorer agree on, so the labeling page only shows disagreements, biggest
  first.
- `tools/collect_frames.sh` pulls every host's sent and review logs into one dataset
  that outlives the hosts' retention, for labelling with `tapo-monitor label`.
- Daily digest fleet block: size and file count of the sent, review and pan-limit logs and
  the free space under them. The daemon warns once on Telegram when free space falls below
  `TAPO_LOG_DISK_MIN_FREE_MB` (default 1024, 0 off) and re-arms after recovery.
- Incident IDs: every audit line and sent-log entry carries `incident=<camera>-<start>`,
  derived from the camera event, and `tapo-monitor incident <id>` prints the chain from the
  ledger and the sent log.
- `tapo-monitor replay`: runs a recorded ledger window through the production mute,
  cooldown and scene-group gates, read-only; `--compare` shows events whose outcome a
  candidate config would change. It also replays recorded SD, sampler and hub deliveries
  (so their cooldowns count), re-applies a candidate `scorer.threshold` to recorded scores,
  measures the scene gate's reach (`--scene-reach`, `--summary-only`), and models
  corroboration holds: a live `hold` is `suppressed(hold)`, and each recorded
  `hold_expired` is re-decided under the replayed `sampler.hold_expiry`, so
  `replay --compare` estimates the alerts the policy adds per camera.
- Alert work in flight survives a restart: hub delivery retries, pending SD follow-ups and
  alert cooldowns are kept in `runtime.json` beside the health state and restored on start,
  so a deploy neither drops a queued alert nor re-sends the same passage.
- One arbiter for the motor (`tapo_monitor/motion.py`): the scheduled preset recall and the
  ONVIF pan-limit guard both ask it before moving. Privacy mode blocks every move; a
  `track_hold` blocks the scheduled recall; the guard overrides a hold after
  `pan_limit.hold_grace` seconds out of bounds (default 20, `0` = the old immediate recall).
  Refused moves are logged once per stretch and counted per camera (`motion_refusals`, also
  in `/status`).
- The daily digest's fleet block reports motor moves the arbiter held back and what the
  last restart carried over from `runtime.json`.
- Hub clip delivery is retried: a failed Telegram send goes to a bounded queue (4 attempts,
  600 s TTL) instead of being lost after the cursor moved on, and the hub cursor is kept in
  `hub_cursor.json` so a restart no longer skips clips from the downtime (capped at 4 h).
- Every hub clip decision (send or drop, with its reason) is audited with the clip's
  `video_type` and length; cooldown drops now carry the score too. Below-threshold hub clips
  go to the review log when one is configured.
- `inactivity_alert_days` (1-90, off by default) for hub cameras: one text notice when the hub
  has indexed no clip for that long. Checked at most once a day; state survives restarts.
- `events_1` bit 8 is decoded as `linecrossing`, so it no longer shows up as an unknown bit.
  It does not change the alert type.
- `hubpoll` refuses to poll clips from a camera the hub reports as recording 24/7
  (`plan_24h_record: true`, e.g. a C460 with 24/7 Capture enabled). The whole detection
  model assumes an indexed clip is a triggered recording; on a continuously-recording
  camera every stored segment would otherwise be scored and alerted on as plain footage.
  The daemon logs a warning once and leaves the camera resolved but idle rather than
  guess from `video_type`, whose values are not yet confirmed against ground truth.
- `light_trigger`: per-camera option to trigger the camera's white lamp / LED floodlight
  on person or motion detection within an optional clock window (`"HH:MM-HH:MM"`).
  `light_trigger.mode: firmware` instead switches the camera to smart night vision inside
  the window (and back to IR outside it), so the firmware lights the lamp itself; the
  polled toggle lands after the `getEvents` lag, often after the person has left the frame.
- `enrich.light_status`: query the camera's white lamp (full-color night-vision light) at
  alert time and note it (🔦) in the Telegram caption. Off by default — one extra API call
  per alert, meaningful only on cameras with that hardware/feature; a failed or unsupported
  query just leaves the caption as it was.
- Per-camera `quiet_hours` (`"HH:MM-HH:MM"`): an all-Telegram mute (detections and
  outage notices) applied INSIDE a fixed local clock window, so the camera reports outside
  it — the inverse of `night_only`, which reports only during the astral night. Mutually exclusive with `night_only`
  (both define the same mute gate; setting both is a config error).
- A night dwell for tracking cameras: `tracking.back_time` (the camera's own return timer,
  written with the auto-track assert) and `tracking.track_hold` (seconds the control pass
  leaves the preset recall alone while events keep arriving). Auto-track turns the lens
  onto whoever walks past, and until now two timers pulled it straight back — the firmware
  after 30 s and our recall on the next control pass — so the SD clip, which is what
  actually gets reviewed afterwards, spent the passage showing an empty preset instead of
  the subject. Both halves are needed: the shorter timer decides, so raising either alone
  changes nothing, and `track_hold` without `back_time` is warned about at startup.
  The hold is honoured only while the plan actually tracks. With tracking off the recall
  is the only thing that repairs a drifted aim — one camera sat pointed at asphalt for two
  days because nothing could bring it back — so a hold can never reach the day, a
  storm-parked camera or a static one. One unbroken hold is capped at `track_hold`, which
  keeps the preset (the only tilt correction) coming round on a busy night, and `pan_limit`
  is untouched: a dwell never licenses the lens to sit outside its preset span.
- `ldc` (Lens Distortion Correction): per-camera boolean option asserting on-sensor barrel distortion
  correction for wide-angle 4K sensors (e.g. C560WS, C260). Straightens vertical and horizontal
  perspective lines across the scene, boosting YOLO person detection confidence near frame edges.
  Monitored continuously for configuration drift via Digital Twin (`video.ldc.enabled`).
- `tamper_detection` and `tamper_sensitivity` (`low`/`normal`/`high`): per-camera options re-asserting
  tamper monitoring every control tick so that lens spray, physical covering, or camera redirection
  cannot remain silently disabled. In the alert funnel, tamper events bypass visual subject confidence
  gates to alert immediately. Watched as a `critical` drift key in the Digital Twin.
- `whitelamp_force_time` (5–300 s) and `whitelamp_intensity` (1–100 %): per-camera options overriding
  the 300 s (5 minute) firmware floodlight default on detection triggers, allowing polite, short night
  illumination pulses (e.g. 30 s). Watched for drift in the Digital Twin (`light.whitelamp.force_time`).
- `set_osd_safe`: wraps OSD updates via standard `executeFunction` JSON-RPC rather than pytapo's raw
  `performRequest`, preventing connection drops and firmware IP lockouts on outdoor models.
- `tapo-monitor learn-face <name>`: CLI helper listening for live on-device face detection events to
  capture stable `face_id`s and output matching `FACE_ID_NAMES` entries.
- `faces.ignore_known`: suppresses alerts when all detected faces belong to enrolled household members,
  audited as `reason=known_face` (the name stays in the journal, never in the ledger) while keeping
  unconfirmed or unknown faces alertable.
- SD follow-ups now select the sharpest above-threshold subject frame, as recorder
  follow-ups already do, instead of stopping at the first accepted frame. Hub clips
  supply up to six candidates from one download with a shared decoding time budget.
  Missing sharpness measurements fall back to detection score; scorer failure retains
  a confirmed candidate when available. Selection logs identify the chosen candidate.
  Live and sampler snapshots are unchanged; full-frame sharpness is not a guarantee
  that a moving subject is sharp.
- Type checking and coverage, both wired into CI. `mypy` runs at the rung an unannotated
  package can hold — assignments a name cannot keep, calls that cannot match a signature —
  which is the fault class that has reached production here (a package copied without one
  module raised `AttributeError` for hours while the tests, which never took that branch,
  stayed green). Four findings were real enough to annotate: a `reason` that is `None` on
  a match, a telemetry dict whose failure branch reports a string where the success branch
  reports floats, and two untyped empty collections. Coverage reports at 88 % with a floor
  of 86 %, present so a module's tests cannot silently stop running — which is exactly what
  happened to the scorer's 34 tests before the `[dev,scorer]` install was fixed.
- The shell layer has tests. Nearly a thousand lines of bash deploy, roll back, provision
  and verify the fleet, and the only thing standing behind them was shellcheck, which
  reads syntax and not intent — the config-path bug below shipped past it and was found by
  an operator instead. `tests/tools/*.bats` runs those scripts for real against stubbed
  ssh, systemd, curl and ping, in a throwaway `$HOME`, so a wrong path or a swallowed
  argument fails on a workstation rather than on a host that is now half-deployed. The
  blocks that normally run over ssh are covered too — the stub executes them here, against
  a directory playing the host, which is the only way to test the config snapshot, the
  atomic switch and the pruning at all. CI runs them beside shellcheck.
- `tools/fleet_status.sh` reads the whole fleet into one comparable table. The daily pass
  was a few dozen ad-hoc ssh one-liners, phrased differently every morning, so its result
  could never be laid beside yesterday's — and half the questions worth asking were the
  ones nobody typed twice. One row per host now carries the unit (including the
  `auto-restart` crash loop `is-active` reports as healthy), the running fingerprint
  against the `TAPO_EXPECTED_FINGERPRINT` its env file names, whether the digest went out,
  the host-watch timer, disk/load/temperature and last night's pan-guard and sent-log
  counts, with the scorer's `/health` and the movement in its counters since the previous
  run. It only reads: no restart, no deploy, no sudo, nothing written on any host. Hosts
  come from `TAPO_FLEET_HOSTS` the way `WATCH_TARGETS` feeds `host_watch.sh`, are probed
  in parallel so one sleeping Pi cannot hold up the table, and an unreachable one is a row
  and a finding rather than the end of the run.
- `tools/provision_scorer.sh` builds the shared scorer host from the checkout. That host
  serves every camera's frames and was the last one whose unit existed nowhere but on its
  own disk, so losing it meant reconstructing the command line from memory. The script
  renders `systemd/tapo-scorer.service.in` with the host's user, working directory and
  interpreter — rendering rather than copying because systemd expands `${VAR}` in an
  `ExecStart` argument but never in the executable itself — writes `scorer.env` once,
  wires the failure notifier, and verifies the result through `/health` and
  `check_scorer_rollout.sh`. It installs only what differs and restarts only when
  something that affects the service changed, so re-running it against the live scorer is
  safe; `--bootstrap` creates the venv on a fresh host and `--dry-run` shows the diffs.
- The scorer reports its own failures. It had no Telegram credentials, which made its
  crash loop the one nobody would hear about, and `Restart=always` alone would retry it
  forever in silence. The unit now gives up after five starts in five minutes and
  `OnFailure=` hands the reason to the fleet's notifier. Credentials live in
  `/etc/tapo-monitor/notify.env`; the script never takes a token as an argument, because
  argv is world-readable in `/proc`.
- A scenario test harness (`tests/scenario.py`) that drives the real `loop_step` through
  multi-tick stories with a fake clock, camera, ONVIF and notifier.
- Configuration: `coordinator.camera_order` must name cameras of its own group, and a
  renamed key can be accepted with a warning for one release (`RENAMED_KEYS`).
- `docs/tapo-local-api.md`: a public reference for the camera's local API beyond pytapo —
  parameter conventions, error codes in plain words, SD card states (including
  `dilatant_suspect` counterfeit cards and `formatSdCard`), event and recording searches
  (`channel`, `chn_events`, the `getEvents` window), face recognition at API level, methods
  pytapo does not wrap, and the calls that take a camera's API down. Every finding names
  the model and firmware it was seen on.
- The scorer can run YOLOX on an Intel iGPU through OpenVINO
  (`TAPO_SCORER_PROVIDER=openvino-gpu`, about 2.6x faster than CPU on the reference host);
  unset keeps CPU, and a GPU start failure falls back to CPU with a warning. The rendered
  unit gets `SupplementaryGroups=render`.
- `deploy_release.sh --health-wait SECS` (default 30): after the restart the unit must stay
  active without two or more automatic restarts, or the deploy re-points `current` at the
  release that was live before (recorded in `.previous_release`) and restarts into it.
- Recorder evidence on camera outage: with `RECORDING_ROOT` set, an outage that reaches
  `alerts.outage_threshold` preserves the camera's newest two nonempty recorder segments,
  with a checksummed manifest, under `<RECORDING_ROOT>-incidents/`. See
  `docs/incident-preservation.md`.
- Multi-camera scenes: the ledger persists measured scene events (lead/follow pairs with
  their event-time delta; a direction only when `coordinator.camera_order` gives the
  measured order), and each camera's clock offset is sampled from the digital twin probe
  and reported in the digest's fleet block, which flags a skew over 5 s. A bounded
  handoff-lease state machine exists for `handoff_preset` but is not wired to move cameras.

### Changed
- SD and local-recorder follow-ups are read off the main loop. A background thread per
  camera downloads or extracts the window, scores the frames and picks one; the loop
  submits a due follow-up and, on a later tick, decides and sends it. A camera-card read
  used to hold every camera on the host for a median 70 s (up to 150 s): no `getEvents`
  poll, no sampler grab, no pan guard. Two reads of one camera never overlap. The scene and
  alert gates are asked again when the read is back, so a passage the live pass or the
  sampler alerted meanwhile is not sent twice. An entry stays queued (and in
  `runtime.json`) while it is read; a stop does not wait for the read and the entry is
  read again after the start. Job temp dirs are now `sdjob_<pid>_*`, and a start removes
  those of a daemon that is gone.
- A recording follow-up scores each frame as soon as ffmpeg has written it, so decoding
  and scoring overlap instead of running one after the other.
- Frames of one SD, recording or hub-clip sequence are scored concurrently (up to 4 at a
  time): identical scores, but a remote site's six-frame look took 3.5 s instead of 5.9 s.
- Recording follow-ups (`snapshot_source: recording`) come sooner: the window is read 15 s
  after it ends instead of 60 s (a live recorder trails the clock by 2-3 s), and a window
  over 28 s first reads the event's opening 24 s, sending as soon as a subject is there and
  reading only the remainder otherwise (audited as `sd retry reason=early_look=...`).
- Frame sharpness is judged on the scorer's subject box when every candidate frame has one
  (Laplacian variance of the crop); without boxes it falls back to full-frame blur as before.
  Below-threshold frames of one sequence share a single audit line.
- Privacy mode is read on the control pass itself (one getter on the connected client),
  so a parked lens gets no recall on the very pass it parks and its aim is restored on
  the first pass after, instead of up to one twin probe (900 s) later. A guard recall the
  camera refuses as a motor refusal is counted, not treated as an ONVIF failure.
- A stop signal inside a tick now waits for the tick to finish (a second signal, or one
  between ticks, still exits at once), so a restart never cuts a preset recall or a
  Telegram send in half. On exit the audit-ledger queue gets a bounded 5 s to drain, and
  the flush `logging.shutdown()` performs is bounded too.
- The status endpoint serves a view the main loop publishes after every tick instead of
  reading live daemon state from its own thread.
- `trigger_whitelamp` retries a transient status read once, never fires the toggle when the
  state is unreadable, and re-reads the state after a failed toggle.
- Auto-track setup reads `back_time` first and skips the write when it already matches; a
  refused combined call warns once per camera and tries the `auto_track_target` namespace.
- A muted camera drops its open sampler group instead of resurrecting it after the window.
- The scorer's metrics settings moved out of `ExecStart` into `scorer.env`, where the
  process reads them itself. The replaced hand-written unit passed them as command-line
  flags, which is the arrangement that turns an undefined `${VAR}` into an argparse exit
  before the model loads.
- **Unknown configuration keys now fail at startup** (they were a warning in 0.5.0), with
  the full key path and the closest real key. Run `tapo-monitor check cameras.yaml` before
  upgrading a host.
- The daily digest judges scorer failures by their rate over the last day (degraded above
  1 %) instead of the lifetime count, which a handful of failures out of a few hundred
  thousand requests used to trip.
- The SD download timeout scales with the requested window (at least 150 s), and the
  reachability ping sends three echoes instead of two, to filter out jitter.
- `tools/fleet_status.sh` falls back to `systemctl --user` when the service is not a system
  unit and finds a probe venv when the default one is missing.
- `deploy_release.sh` and `rollback_release.sh` keep `TAPO_EXPECTED_FINGERPRINT` current
  when the host has opted in: the release they just switched to is by definition the
  intended one, and a manual update step that must follow every deploy is a step that
  will eventually be forgotten — turning the drift check into a false alarm the day
  after it was needed most. Update-only; a host without the line stays unenrolled.

### Fixed
- A deploy to a host running tapo-monitor as a systemd user unit no longer rolls back a
  release that started fine: `deploy_release.sh`, `rollback_release.sh` and
  `check_monitor_rollout.sh` take `--user`, which sends every systemctl/journalctl query on
  the host to the user manager (health check, env-file lookup, unit state, journal) and
  makes the default restart `systemctl --user restart <unit>`. Without it the system
  manager reported the unit `inactive` and the rollout check a false `unit: FAILED`.
- One network outage no longer produces a pile of notices: the event-API watchdog stands
  down while the camera is off the network, so an offline camera gets its 🔴/🟢 pair and
  nothing else — no "event API unavailable" before the outage alert, no event-API reboot
  right after the camera returns, and "restored" reports the real duration, not 0 s.
- The pan guard's hold grace restarts after an ONVIF read failure instead of recalling the
  lens with no grace on return.
- Hub clip downloads now enforce stream completion integrity (`download_clip`). If a
  download stream terminates early, stalls, or receives an error, the incomplete video
  file is unlinked and rejected rather than treated as a valid clip.
- The hub session no longer breaks against H200 firmware (observed on 1.7.5) that
  disconnects a reused HTTP connection on the second request of an otherwise-valid
  session. The authenticated session now opens a fresh TCP connection per HTTP request
  while keeping the same encryption key, and shutdown cancels pending work and closes
  the HTTP client even when the protocol's own close call fails, instead of leaving a
  socket the next handshake has to wait out.
- The SD follow-up no longer scores frames the camera recorded while the pan-limit guard
  had its aim off the allowed span. The guard fixes where the lens points; it cannot
  unrecord what is already on the card, and the follow-up re-scores that recording about
  two minutes later. In one case a frame of an IR-lit obstacle close to the lens
  scored 0.63 and was delivered as a person — eleven seconds after the recall had already
  corrected the camera, and while the live path's corroboration gate was correctly holding
  the same view at 0.56. Such frames are now skipped before they reach the scorer, audited
  as `panlimit_window`. Frames carry their capture epoch in their own name because that is
  the only channel that survives the download subprocess, and a name without one is scored
  as before: unknown must never cost a real detection.
- `check_monitor_rollout.sh` finds the config under the release layout. It tested for
  `cameras.yaml` through one path and then recorded another: the kernel resolves `..`
  against the directory `current` points at, bash's logical `cd` against the symlink's own
  parent. The walk-up therefore located the file and handed the selfcheck a path with none,
  so every post-deploy check on a release-layout host reported a config failure the host
  did not have — the one check meant to catch a bad deploy, failing on good ones.
- `deploy_release.sh` no longer reopens the ref slot for an explicit `HEAD`. It used the
  default value as its "was a ref given" sentinel, so `deploy_release.sh <host> HEAD <ref>`
  quietly shipped `<ref>` — a deploy of something the command line does not name, which is
  the kind of mistake that is only noticed by the fingerprint on the far side.
- `provision_scorer.sh --help` prints the header block and stops there. The fixed line
  range it ended on had already slipped past the last comment line and printed
  `set -euo pipefail` as if it were help text.

### Removed
- `systemd/tapo-scorer@.service`. The instance form promised a unit that could be enabled
  per service user, but its paths still had to be hand-edited, and no host ran it —
  `tools/provision_scorer.sh` renders the real unit instead.

## [0.5.0] - 2026-09-01

### Added
- Deploys land in timestamped release directories and a symlink switch makes them live:
  `tools/deploy_release.sh` stages from `git archive` (never the working copy), snapshots
  the host's config and env file into the release, runs `selfcheck` inside it, flips
  `~/tapo-monitor/current` atomically and verifies the fingerprint through the symlink;
  `tools/rollback_release.sh` re-points it. Rollback stops being "find the right tarball".
  The package gained a `__main__` entry point so the unit can start from an absolute
  interpreter regardless of cwd.
- The hosts can watch each other. The digest heartbeat cannot report that its own host is
  dead, so `tools/host_watch.sh` plus a two-minute timer lets one host ping its peers (and
  optionally poll an HTTP health endpoint) and alert through the Telegram credentials it
  already has — three consecutive misses to alert, one alert per cooldown, a recovery
  message when the peer returns. The topology lives in each host's env file, not here.
- An opt-in JSON status endpoint (`observability.status_port`): the daemon serves package
  version and fingerprint, last tick and the per-camera twin summary on localhost by
  default — binding wider is the operator's explicit choice, not a shipped default.
- Unknown configuration keys are warned about, with the full key path and the closest
  real key. A mistyped key silently took its default — a dropped `rotate` costs about a
  third of the person score, which is a silent alert killer. The known keys are derived
  from the dataclasses themselves so the check cannot rot, and the free-form sections
  (`telegram`, `groq`, `faces`) stay opaque on purpose. Warn-only for now; the hard fail
  waits until the warnings have soaked in production.
- The daily digest's fleet block carries the running package fingerprint, and when
  `TAPO_EXPECTED_FINGERPRINT` names the intended release a mismatch is a failed check
  that takes the OK headline with it. Silent fingerprint drift has been found by hand
  twice; the digest is where it should have been visible.
- A hold that a pan-limit recall broke is sent instead of dropped. A marginal motion
  frame waits for a second candidate before it may alert; when the guard recalls the
  camera mid-wait, the subject leaves the frame and the second candidate structurally
  cannot arrive — the hold then expired as if the scene had been empty, and a real person
  at p0.62 was lost exactly this way (2026-08-31 19:57). The group now remembers the
  archived review-log copy of its held frame, and an expiring hold whose camera was
  recalled between the hold and the expiry sends that copy through the normal alert path
  — same cooldown gate, same delivery accounting, audited as `send`/`hold_rescue_recall`.
  No knob: only cameras running `pan_limit` ever record a recall.
- The pan-limit guard archives one frame per intervention. Deep-night recalls were
  undecidable after the fact: nothing below threshold is archived, so whether the guard
  pulled the camera off a wall or off a person was guesswork. Just before recalling, it
  grabs the out-of-bounds view (the recall is what erases the evidence) into a
  `panlimit-log` directory next to the review log — deliberately not *into* the review
  log, which the daily digest reads and twenty guard frames a night would flood — named
  by camera, axis and position, kept two days. Best-effort: a failed grab is a debug
  line, never a skipped or delayed recall.
- The soft PTZ guard can bound tilt, not just pan (`pan_limit.tilt`, off by default).
  Auto-track moves tilt too, and nothing corrected it: `pan_limit` clamped one axis and a
  recalled preset was the only thing that touched the other. Derived from presets the way
  the pan bound is — with a window, because tilt needs one: presets are where people park a
  camera to look at something unusual, and a single one aimed high stretches the bound over
  nearly the whole travel. Six presets on one camera spanned 1.79 of the 2.0 its motor can
  reach because one pointed at the sky. `tilt_min`/`tilt_max` exclude those from becoming a
  bound without inventing a position: every bound stays a real preset, and a window leaving
  fewer than two candidates disables the tilt guard rather than clamping to a guess.
- Privacy mode is part of the digital twin. It is the one switch that stops a camera
  watching at all — the lens parks, nothing is recorded or detected, and every motor call
  comes back `MOTOR_BUSY` — and nothing in the package read it. Two cameras spent nine
  hours like that before anyone noticed, and the only trace was a preset recall failing
  once a minute. Someone switching it on is legitimate; not being able to tell that they
  did is not. Reported as critical drift, which fires on the transition rather than every
  pass, so it says so once when it goes on and once when it comes back.
- `selfcheck` reports where the journal is written. Two hosts were found writing theirs to
  RAM, one keeping thirteen hours of history and losing even that on reboot, after months
  of passing every other gate. Nothing announces it: journald decides at start-up, so
  creating `/var/log/journal` on a running host changes nothing, and drop-ins merge by
  filename across directories, so a vendor `Storage=volatile` outranks an `/etc` drop-in
  that sorts before it. It reads the two journal directories rather than shelling out, so
  the check stays offline, and it warns instead of failing — a journal in RAM does not stop
  the daemon, it stops anyone investigating the daemon a day later, and failing would block
  a rollout over a host that runs perfectly well.
- A fleet block in the daily review digest, so one message a day says the fleet is alive.
  Every other Telegram message is a transition, which left "everything works" expressed as
  silence — indistinguishable from a dead host or an expired token. It reports camera
  reachability, the daemon's tick, the shared scoring service (asked once a day; when it
  dies, alerts stop at every site at once), the recorder's newest file, the day's alert
  counts from the sent log, and any self-heal a camera is refusing. It claims `Fleet OK`
  only for what it actually checked — an unchecked subsystem gets no line, an unknown
  camera is named as unchecked — and any failed check removes the headline, because a
  heartbeat that says OK while a camera is down is worse than no heartbeat.
- `tapo-monitor version` and `tapo-monitor selfcheck`. Deployed hosts were rsync/tar copies
  rather than git checkouts, so a host could not say which code it was running; `version`
  answers that with a digest over the deployed module set, which also makes a half-copied
  package visibly different from the tree it came from. `selfcheck` imports every module,
  loads the config, asserts the credential env vars that config names are actually set, and
  looks for `ffmpeg` — the four ways a deploy has produced a running-but-useless daemon.
  It prints env var names, never values.
- `tools/check_monitor_rollout.sh`, the post-deploy check the daemon never had: unit state
  (an `auto-restart` sub-state is the crash loop `is-active` hides), package fingerprint,
  `selfcheck`, and the journal since the restart. Liveness is reported, not asserted — the
  daemon logs per decision, not per poll, so a quiet camera is not a failure.
- `OnFailure=pi-failure-notify@%n.service` plus `StartLimitBurst=5`/300 s on both units.
  The notifier and its 30-minute cooldown were already in the repository, wired to nothing,
  so a crash loop retried forever in complete silence.
- A startup warning naming static cameras that configure a `night_preset`. A static camera
  never tracks, so it stays parked at `day_preset` around the clock and the night key is
  never recalled while the config reads as if a second view were being held.
- An ffmpeg budget for the shadow scan (`--extract-budget`, default 18000 s), split as an
  even share per camera. The per-segment timeouts bound one call each, but a full day is 96
  segments per camera, so a slow host could otherwise decode all night. Skipped segments are
  counted per camera and flagged in the run summary: a trimmed run must not look like a
  complete one.
- Durable shared-scorer metrics with bounded p50/p95 latency windows, restart-safe
  aggregate counters, seven-day journal rotation and optional pseudonymous source buckets.
- Bounded shadow-scan scene extraction remains enabled in the normal nightly pass, with
  seek-sample fallback only after a scene pass fails or times out.
- `hubpoll`, a detection source for battery cameras that record to a hub instead of to
  their own SD card. Such a camera has no event index of its own and sleeps between
  events, so the daemon reads new clips from the hub (a standalone pass, since the sampler
  only advances groups the getEvents path creates) and grabs the alert frame from a go2rtc
  sidecar over HTTP. Scorer, cooldown gate and sender are reused unchanged. Nothing changes
  for existing cameras: the source is inert until a camera opts into it.
- `hubclient`, the hub session client behind it. Newer hub firmware does not expose these
  cameras through the child-device family — that one covers sub-GHz sensors and doorbells —
  so they come from a paired-general-device list and their recordings are addressed by
  device id plus MAC rather than by channel. One session is opened and held (the handshake
  is what the hub rate-limits, not the queries inside a session) and every failure backs
  off; eviction by the phone app is expected and never alerts.
- `snapshot.capture_go2rtc`, a single-frame JPEG grab from a go2rtc source, for cameras
  with no usable RTSP. Empty bodies and unreachable sidecars leave no orphan temp file.
- The alert frame for a hubpoll camera comes from the hub clip itself: it is downloaded
  over the hub's media port and a frame extracted from the MPEG-TS. The clip *is* the
  event — measured at 3–5 s and ~3 MB — whereas a live grab lands 20–30 s later, once the
  camera is asleep again and the scene is empty, which is exactly the empty-scene false
  alert this project has already paid for. The sidecar grab remains as the backup, and a
  clip frame is scored and gated like any other, so the fallback is no way past the
  threshold.
- A startup warning when a `hubpoll` camera is configured and `ffmpeg` is not on the
  daemon's PATH. Both frame paths shell out to it, and it is looked up per event, so a
  daemon started with a minimal PATH detects everything and alerts on nothing. One site ran
  two days that way: 13 real detections, all dropped as `no_frame`, clips downloading fine
  the whole time.

### Changed
- A `role: static` camera is parked at its `day_preset`, recalled every control tick. It
  was planned no preset movement at all, which left the one camera class that nothing else
  ever moves with no automatic way back from a nudge — the failure was live for two days
  (2026-08-18..20) on a camera that ended up aimed at asphalt. The recall is a no-op while
  the camera already holds the preset, so it only costs anything when it is needed.

- `scorer.score_image` is called with `source_id` directly. Two reflective
  `inspect.signature` probes (in the daemon and in the shadow scan) asked at runtime
  whether a same-package function accepted the argument; the injected-callable contract is
  now explicit, and test doubles accept the keywords the production caller actually sends.

### Fixed
- The repair allow-list means one thing. `auto_fix` and `allowed_repairs` bounded the
  guard repairs even while `reliability.enabled` was false, so trimming the list under a
  disabled block silently switched off re-assertions that predate the whole feature —
  person detection on, vehicle detection off, the people-only tracking filter. A disabled
  reliability block is now inert: the guard repairs run as they always had, and the two
  keys constrain them only while the block is enabled.
- The scorer's metrics journal rotates on size (32 MiB default) as well as age, so a
  burst of fat records cannot outgrow a disk between two age checks — the second repair
  to this rotation; the age check itself was fixed earlier in this release. Rotation
  never touches the state sidecar, so cumulative counters still survive restarts.
- The ledger sanitiser scrubs IPv4 addresses and session-token shapes itself instead of
  trusting every caller to do it first. pytapo error strings carry the camera address and
  the session token, and one unsanitised call site is all it takes; floats, versions and
  camera names are provably untouched.
- Every delivered Telegram text notification leaves a journal line — drift alerts and
  recoveries, camera outage 🔴/🟢, the daemon's own stall watchdog and the event-API
  notices. Only failed deliveries were logged, so from the host a delivered drift alert
  and one that never went out looked identical (the 2026-09-01 fleet review could not
  verify drift-alert delivery at all). Same repair the review digest already got: the
  line says kind, camera and count, never the message body.
- A refused preset recall is logged once and then counted, not repeated every control
  pass. A camera that refuses every recall wrote 1104 identical warnings in ten hours,
  which buries the log it is supposed to be improving. Dropping the warning would be the
  wrong repair — it exists because a silently refused recall once left a camera aimed at
  the ground for two days — so identical refusals are counted and the count rides along
  the next line, which comes when the reason changes, when the camera recovers, or after
  half an hour. The warning also names the camera now; on a host with two of them it did
  not, and the preset number was the only clue.
- The capability snapshot derives its groups from the probe tables instead of repeating
  them. The hand-kept copy meant adding a probe in a new group raised `KeyError` deep
  inside `collect_snapshot` rather than simply working.
- A preset recall is retried once through a stale transport. pytapo does not announce that
  its session expired: the camera answers `motorMoveToPreset` with
  `ERR_CODE_NULL_TRANSPORT`, and the library retries only the cruise-conflict code, so the
  refusal propagated and the camera stayed off-target until the next control pass. The next
  request re-authenticates, so a second attempt is worth making. A recall that both attempts
  fail is still reported — the retry must not turn a genuinely stuck camera back into a
  silent one.
- An auto-track assertion the camera refuses is logged. `apply_plan` asserts auto-track
  last and verifies it, but the control pass discarded that answer, so a camera quietly
  rejecting auto-track every tick produced no log line at all — the same silent failure the
  preset recall had before it got its warning.
- CI runs the scorer-service tests, which it had never run. `tests/test_scorer_service.py`
  opens with `pytest.importorskip("numpy")` and numpy lives in the `scorer` extra, while CI
  installed only `[dev]` — so 34 tests covering the one component whose death stops alerts
  at every site at once were skipped as a whole module, and the build stayed green. Local
  runs passed them only because a developer machine happened to have numpy. CI installs
  `[dev,scorer]` now, and a step asserts that module is collected rather than skipped,
  because a silently absent module is what hid this.
- CI tests both Python versions the fleet actually runs. The host with the recorder is on
  3.12 and both Pis are on 3.13; testing one meant a 3.13-incompatible change could ship
  green and break two of three sites, with the deploy already done.
- A camera refusing one of the three bounded self-heals is reported instead of swallowed.
  They are idempotent re-assertions sent every control pass inside bare excepts, so a
  camera that rejects them looked exactly like one that accepted them — the same blindness
  the preset recall had until it was made to speak, and with the same consequence: person
  detection stuck off demotes every person to bare motion. Refusals are logged and counted
  per repair so the daily digest can say so.
- The digest's shadow-scan line reports segments *covered*, not merely present. A run that
  spent its decode budget reported the same segment total as a complete one, so the line
  read as full coverage while a camera had three quarters of its day unscanned.
- The camera reachability probe sends more than one echo. These cameras drop 2-4 % of
  ICMP echoes on a radio whose own gateway drops none, and a single-packet probe on the
  60 s control pass turned each lost packet into a warning *and* a hole: a camera that
  fails the probe is dropped from the client map, so event polling, the sampler and the
  follow-up drain all skipped it until the next pass. ping exits 0 when any echo is
  answered, so the retry costs a healthy camera nothing and a genuinely offline one still
  fails them all - the outage threshold that raises the real alarm is untouched.
- The daily review digest logs the digest it sent. Only failures were logged, so a
  delivered digest left no trace in the journal and the only evidence the channel still
  worked was its state file: silent-when-healthy is indistinguishable from dead.
- Below-threshold motion on a recorder-backed camera reaches the recorder again. The
  second look is the reason `snapshot_source: recording` exists - a live frame can score
  0.05 on a subject the recording shows at 0.83 - and it was unreachable: `empty` is
  `score < scorer.threshold`, the corroboration gate is handed that same threshold as its
  confirm level, and its drop verdict returned one branch before the deferral. So every
  frame that qualified for a look was dropped just short of it, roughly 500 motion events a
  day per camera. The look now runs before the gate; marginal frames still wait for
  corroboration and clear frames still send live, unchanged.
- A deferred motion burst no longer stands the live sampler down. The recorder look is
  extra evidence, not a replacement, so marking the burst as sent traded six live frames
  for one recorder window that may find nothing.
- Unconfirmed motion follow-ups ask the alert gate before sending. The send only recorded
  itself in the gate afterwards, so with the sampler still working the same burst the same
  passage could reach the phone twice, minutes apart. A same-passage alert retires the
  entry, since no later tick can make it sendable; a plain wall-clock cooldown puts it back
  on the queue instead of discarding it.
- The shadow scan analyses keyframes only (`-skip_frame nokey`). A 15-minute 4K HEVC
  segment holds roughly 18000 frames against 300 keyframes, and scene changes live between
  keyframes, so decoding every frame bought nothing: measured on the production host, a
  quiet segment ran past 200 s without reaching a verdict and was killed at the 60 s scene
  timeout, leaving two seek frames in place of the pass. Scaling ahead of `select` only ever
  cheapened the filter, never the decode. Keyframe-only decode finishes the same segment in
  30-59 s, so the whole day is analysed instead of sampled. The scene timeout rises to 90 s
  because bright daylight segments measured 58-59 s, right at the old ceiling.
- Both shadow-scan budgets are shares per camera instead of one run-wide counter spent in
  `cameras.yaml` order. Cameras are processed sequentially, so the first one could take
  everything: on a two-camera host the first spent the whole decode budget and the second
  got 25 of its 96 segments — 74 % of the day unscanned — while the frames actually scored
  split 726 to 99. Unspent share rolls forward, so a share is a floor and not a cap and a
  cheap camera still funds a busy one.
- The metrics journal now rotates. Its age was measured from the file mtime, which every
  append refreshes, so a journal written once a minute never reached the retention window
  and grew without bound while `retention_files` stayed unused. The age now comes from the
  oldest record in the file; content this process did not write still falls back to mtime.
- A scorer host whose `scorer.env` predates the durable-metrics settings no longer
  crash-loops. The four `TAPO_SCORER_METRICS_*` values are read from the environment by
  the process instead of being interpolated into `ExecStart`, where an undefined `${VAR}`
  expands to nothing and argparse exits before the model loads. Unset, blank and
  unparseable values keep the defaults — observability must never be why a service refuses
  to start.
- A `getEvents` failure no longer puts the camera's address in the event ledger. Only the
  exception class is recorded there; the message, which carries the host and sometimes a
  session token, stays in the local journal where it is diagnosable. As a side effect the
  reason is no longer dropped whole: a long failure text used to exceed the ledger's value
  limit and be discarded, leaving the record with no reason at all.
- A malformed scorer metrics state file no longer prevents startup, and metrics write
  failures cannot turn a successful scoring request into an HTTP error.
- The scorer performs one orderly persistence flush on shutdown instead of duplicating it
  from both the signal handler and the shutdown path.
- A failed frame extraction is logged with its reason instead of at debug level, and the
  hubpoll retry now says "no frame from the clip" rather than "clip download failed" — the
  download is one of two steps there, and naming the wrong one pointed a real investigation
  at the hub for two days.

## [0.4.0] - 2026-08-08

### Fixed
- The live alert pass dropped its frame through a cleanup helper that knew nothing about
  the native original a `crop_from_native` camera carries, leaking one full-resolution
  JPEG per event. All four cleanup sites now share one twin-aware helper.
- A crop rect scaled into the native frame's coordinates could overflow it when the two
  frames did not share an aspect ratio (a rotated or letterboxed stream). That surfaced
  only as a failed ffmpeg and an alert quietly missing its zoom; the rect is now clamped.

### Changed
- Live alerts take the same route as the sampler and the SD follow-up: cropped to the
  subject for Telegram, whole scene to the sent log. Previously the live pass posted the
  frame as-is, so it never zoomed and discarded the native grab it had just paid for.
- A too-tall crop is widened towards the scene's aspect ratio, so a standing figure no
  longer arrives as a vertical sliver. Widening stops at 60% of the frame width, keeping
  the zoom on tall subjects; the rect now rounds instead of truncating.
- `crop_from_native` skips the native twin when the grabbed stream is already at delivery
  width, so enabling it on a camera whose hot path is a 720p substream costs only the
  dimension probe. The documented cost is corrected: measured within one stream, a native
  grab is ~+0.5 s on a Pi Zero 2 W, not 3x.
- Alert captions mark the subject with a paw when the scorer's animal confidence clears
  0.6. A dog walker scores high on *both* person and animal (measured 0.91/0.80), so the
  animal score is read on its own rather than compared against the person one. Gating is
  unchanged — the threshold still reads person alone.
- The sent log records the camera name and both scores, and its index is rotated on the
  same retention window as the frames instead of growing without bound.

## [0.3.0] - 2026-08-07

### Added
- `crop_from_native` takes the `crop_to_subject` zoom from a native-resolution grab
  instead of cropping a frame that was already reduced to 1280 wide — a distant figure is
  ~64px across at 1280 and ~190px at 4K. The frame is reduced at the grab and the native
  original rides along with it, so the scorer, the captioner and Telegram keep receiving
  delivery-width images and only the crop spends the detail; the cleanup helper removes
  both, so a sampler that discards most of its frames leaks nothing. Off by default: the
  grab costs nothing extra on a Pi 4 but 2.5–3.5× on a Pi Zero 2 W, where the worst case
  approaches `rtsp_timeout`. Requires `crop_to_subject`. Measurements in
  `docs/configuration.md`.
- A dead-man's switch for the daemon itself (`alerts.stall_threshold`, default 900s):
  when every loop iteration has raised for that long, a 🔴 goes out and a later healthy
  tick sends 🟢. The per-camera outage watchdog runs inside the tick, so a fault in the
  tick suppresses the very alerting meant to report it — the daemon then keeps logging
  errors while Telegram stays silent, which is indistinguishable from a calm night.

### Changed
- `crop_to_subject` cameras now archive the uncropped frame in the sent log while still
  sending the zoom to Telegram, so a false positive can be reviewed against the whole
  scene instead of a close-up. The alert send path also unlinks the crop it creates,
  which previously leaked one temp file per cropped alert.

## [0.2.0] - 2026-08-05

### Added
- Per-camera `rotate` (0/90/180/270) straightens a physically mis-mounted camera at frame
  capture across all sources (RTSP, local recording, SD), so the scorer, crop and Telegram
  all see an upright frame — for cameras whose firmware doesn't expose a flip setting.
- Multi-frame corroboration for bare (non-PIR) motion (`scorer.motion_send_threshold`):
  a single marginal frame (`[threshold, motion_send_threshold)`) is held until a second
  frame corroborates it within the sampler window, cutting empty-scene night false
  positives without delaying camera-confirmed people or PIR-backed motion. Default off.

- Expired corroboration holds now leave an audit trail (`action=drop reason=hold_expired`
  with the last held score), so threshold tuning can count discarded holds like any other
  outcome instead of inferring them from unsent group closures.

### Changed
- The scorer client now retries once before degrading to raw passthrough, so a single
  transient timeout no longer flips a whole event burst to unfiltered sends — but only
  when the first attempt failed fast; a hung scorer is not retried, so a sick service
  cannot double every frame's stall. Telegram photo sends also retry once before being
  reported as undelivered.
- A confirmed detection whose live frame is empty no longer queues an SD follow-up when
  the same event burst already delivered an alert, removing the duplicate second photo
  of one passage. Only actual deliveries count: a queued follow-up or a failed send never
  suppresses the person safety net.
- Opt-in review log (`TAPO_REVIEW_LOG_DIR`) archives the frames motion corroboration
  *held* (never sent), so a hold can be verified as an animal/empty scene rather than a
  missed person. Weekly retention by default (`TAPO_REVIEW_LOG_RETENTION_DAYS`, default 7).

### Documentation
- Reworked the GitHub landing page and added a documentation index, architecture guide,
  configuration reference and firmware-aware troubleshooting runbook. Operational,
  opt-in, researched and planned capabilities are now labelled explicitly.
- Operations runbook now documents the frame-level calibration aids (sent-frame archive and
  the `scene_probe` on-demand scorer).

### Fixed
- `scene_probe` now applies the camera's `rotate` at capture, so calibration scores are
  measured on the same upright frames production scores.
- Tiled scoring no longer gates alerts: with `scorer.tiles > 1` the send-decision
  person/animal scores come from the full frame only, while the best tile still supplies
  the person box for subject crops (plus a diagnostic `tile_person`). Blown-up tile crops
  of night IR grain hallucinated 0.3–0.6 "person" scores and tripled night false alerts.

- Local scorer alert gating now uses only `person` confidence. Returned animal confidence
  remains audit telemetry, so an animal cannot trigger a person notification.

### Added
- Opt-in sent-frame archive (`TAPO_SENT_LOG_DIR`): every photo delivered to Telegram is
  copied to a timestamped JPEG with an index line and pruned after
  `TAPO_SENT_LOG_RETENTION_DAYS` (default 2 days), so false positives can be reviewed as
  images. Inert when the variable is unset and never blocks a send.
- `scene_probe` diagnostic (`python -m tapo_monitor.scene_probe cameras.yaml <cameras>`):
  grabs a live frame from named cameras and scores it internally — full-frame person/animal
  plus the best-tile score — without sending anything to Telegram, for calibrating scorer
  sensitivity on demand.
- The scorer's `GET /metrics` endpoint exposes aggregate request, inference and latency
  counters without retaining image, camera or client data.
- Per-camera `always_day` and `always_night` schedules now override the shared astral
  decision consistently; astral calculation prefers the configured location over legacy
  environment variables.
- Sampler low-score early exit (`sampler.low_score_exit` / `sampler.low_score`): a
  motion-only event group closes once N consecutive follow-up frames score below the
  "nothing there" mark, instead of grabbing the full window on empty bursts.
  Person/PIR-confirmed groups always run the full window, and a confirmed detection
  reopens an early-exited group. Off by default.
- API reconnects after a failure streak are now logged
  (`connect <camera> succeeded after N failure(s)`), so recovery time after an outage
  is visible in the journal, not just the failures.
- Opt-in Camera Digital Twin: redacted safe-getter capability snapshots on an existing
  camera session, layered network/API/events/RTSP/storage health, desired-state drift,
  atomic private persistence, deduplicated new/recovered drift alerts and human/JSON
  `tapo-monitor twin-status` output.
- Opt-in Shadow Detection Auditor: a private SQLite ledger for normalized camera events,
  pipeline decisions and independent local scorer/recorder observations, with retention,
  deterministic one-to-one correlation, `shadow-record` ingestion and human/JSON
  `shadow-report` output. No frames, stream URLs or credentials are stored.
- Unauthenticated ICMP reachability probes before camera login, with network uptime kept
  separate from API/authentication backoff. Observed online/offline totals, reconnects and
  pending recovery notifications survive daemon restarts in an atomic private state file;
  `tapo-monitor status` reports current intervals and cumulative observed availability.
- Confirmed Telegram delivery semantics: failed outage/recovery notices are retried, failed
  SD sends remain queued, failed sampler sends keep their event group open, and a failed
  live send never arms the cooldown (it falls back to SD or sampler when configured).
- Scorer tiling (`scorer.tiles`) for distant subjects plus optional `crop_to_subject` alert
  photos, using the scorer's best person box to send a readable padded zoom while retaining
  the full frame whenever cropping is unsafe or unavailable.
- Per-camera `night_vision`: `ir` forces IR/black-and-white night vision on the astral
  night schedule (day/colour by day), re-asserted each control tick — a colour night mode
  under a streetlight uses a slow shutter that smears moving subjects, while IR's faster
  shutter keeps the event frame sharp; `auto` re-asserts the camera's own day/night switch.
- Per-camera `snapshot_source: recording`: the SD follow-up extracts its candidate frames
  from a local 24/7 recorder's mkv instead of the camera SD — full stream1 resolution even
  when live detection runs on stream2, and it sends the sharpest above-threshold frame
  (ffmpeg `blurdetect`) rather than the first. Reuses the SD follow-up queue (requires
  `sd_snapshot: true`) and reads the recorder tree from `RECORDING_ROOT` — the same env var
  the live recorder-fallback already uses — falling back to the SD/live path when unset.
- Per-camera `pan_limit`: a soft PTZ pan-limit for cameras whose auto-track overshoots.
  The local Tapo API has no angular limit or motor-position readout, but ONVIF exposes both
  position and preset positions, so the daemon polls the current pan and recalls the camera
  to its bounding preset whenever it drifts outside the span of the camera's presets. The
  presets define the allowed range; ONVIF hiccups are logged and never stall the loop.

## [0.1.0] - 2026-07-10

### Added
- `tapo_monitor` package and `tapo-monitor` CLI (`check` / `run` / `audit-log`) — one
  config-driven daemon replacing the original per-host scripts.
- Config-driven `cameras.yaml` model: per-camera detection sources, tracking, scheduling,
  weather strategy and enrichment, with secrets referenced by environment-variable name only.
- Detection via the camera's on-device AI (`getEvents`): the `events_1` bitmask is decoded
  into named signals (motion / PIR / person); unmapped firmware bits are reported as
  `unknown_bits` rather than guessed at.
- Optional local YOLO scorer service and HTTP client: frames are POSTed to a (shareable)
  scorer whose `person` / `animal` confidence gates send/drop; Groq stays caption-only.
  The response includes per-class confidences for other HTTP callers of the same service.
- Event-window sampler: follow-up RTSP grabs across long camera events, scored locally,
  so late-entering people are still caught.
- Live + SD-card hybrid snapshots: an empty live grab triggers an SD-segment follow-up
  sized from the event's own duration; if no extracted frame shows a subject, nothing is
  sent (a blank-frame alert usually meant a false positive such as a passing car).
- `sd_motion` (SD second chance for PIR-backed bare motion), `sd_jobs_per_tick`
  (slow-host backpressure) and `sd_span_cap` (wider SD windows on fast hosts).
- `night_only` mode for after-hours-only alerting while still draining daytime events.
- Decoupled fast detection loop: `getEvents` polled every few seconds on the existing
  session while camera control runs on a slower tick — no per-tick re-login (C560WS
  lockout risk).
- Auto-tracking / SmartTrack applied in a firmware-safe order (auto-track asserted last)
  with self-healing re-asserts, including the AI person-detection toggle.
- Weather gating (open-meteo, cached, hysteresis): lower motion sensitivity or pause
  auto-tracking in the rain; `weather.storm_park` additionally parks the PTZ.
- Astral day/night scheduling (coordinates from config) with an HH:MM fallback.
- Face labelling via `faces.names_env`; a recognized face breaks through the per-type
  alert cooldown (a face is new information, not a burst duplicate).
- Groq vision scene descriptions on the chosen snapshot; empty-scene frames are dropped.
- Camera-down watchdog (🔴/🟢 Telegram) and per-camera detection-alert cooldowns.
- Structured per-event audit logging plus the `audit-log` summarizer for threshold
  calibration.
- systemd templates for the monitor daemon and the scorer service; optional
  local-recorder snapshot fallback (`RECORDING_ROOT`, `RECORDING_MAX_AGE`).

### Fixed
- Groq vision calls were rejected by Cloudflare (HTTP 403) because the request used the
  default `Python-urllib` User-Agent; a real client User-Agent is sent now.
- RTSP snapshots are scaled to 1280 wide and written with ffmpeg's `-update 1` so a
  single fixed-name image is accepted on ffmpeg 7.x.
