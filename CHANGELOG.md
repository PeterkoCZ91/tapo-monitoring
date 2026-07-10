# Changelog

All notable changes to this project are documented here.

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
