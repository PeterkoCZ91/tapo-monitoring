# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- Multi-frame corroboration for bare (non-PIR) motion (`scorer.motion_send_threshold`):
  a single marginal frame (`[threshold, motion_send_threshold)`) is held until a second
  frame corroborates it within the sampler window, cutting empty-scene night false
  positives without delaying camera-confirmed people or PIR-backed motion. Default off.

### Changed
- The scorer client now retries once before degrading to raw passthrough, so a single
  transient timeout no longer flips a whole event burst to unfiltered sends. Telegram
  photo sends also retry once before being reported as undelivered.
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
