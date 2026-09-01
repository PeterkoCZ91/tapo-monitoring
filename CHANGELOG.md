# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
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
- `tapo-monitor version` and `tapo-monitor selfcheck`. Deployed hosts are rsync/tar copies
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
