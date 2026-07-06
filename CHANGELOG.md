# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- `tapo_monitor` package and `tapo-monitor` CLI (`check` / `run`) — one config-driven
  daemon replacing the original per-host scripts.
- Decoupled fast detection loop: `getEvents` polled on the already-connected client every
  few seconds while camera control runs on a slower tick, so people are seen quickly
  without a per-tick re-login (which risks the C560WS lockout).
- Live + SD-card hybrid snapshots (`sdclip.py`): when a live RTSP grab is empty (a bare
  grab often misses a subject who walks into view seconds after the event fires), a
  confirmed person triggers an SD-segment follow-up that extracts candidate frames across
  the event and picks the one the subject is in. The extraction window follows the
  camera's own event duration (`start_time`..`end_time`, clamped to 36–48 s for the
  Pi Zero download budget), and the fetch is delayed until the whole window clears
  pytapo's 60 s freshness guard — so a subject appearing well into a ~70 s event is
  caught instead of an empty first slice.
- `weather.storm_park`: opt-in flag that parks the PTZ while it rains (composes with the
  `lower_sensitivity` / `disable_tracking` strategies) so the camera stops swinging after
  raindrops and wet branches.
- Self-healing person detection: the AI person-detection toggle is re-asserted each tick,
  so a camera restart can't silently leave it off.
- Config-driven `cameras.yaml` model (`config.py`): per-camera detection sources,
  tracking, scheduling, weather strategy, enrichment and coordinator settings, with
  secrets referenced by environment-variable name only.
- Detection via the camera's on-device AI (`getEvents`): `detection.decode_events_1`
  decodes the `events_1` bitmask into named signals (motion / PIR / person) and reports
  still-unmapped firmware bits as `unknown_bits` rather than guessing their meaning.
- Per-event audit logging: every camera event is logged with its decoded signal,
  `alarm_type`, face count and the resulting verdict (person / drop), plus
  snapshot / Groq / alert outcomes.
- Face labelling: `event_info[].face_id` mapped to names via `faces.names_env`;
  unrecognized but stable IDs render as "unknown face".
- Groq vision scene descriptions on an RTSP snapshot; frames reported as an empty scene
  are dropped instead of alerted.
- Auto-tracking / SmartTrack applied in a firmware-safe order (auto-track asserted last,
  which the C560WS requires) with a self-healing re-assert.
- Weather gating (open-meteo, cached, hysteresis): lower motion sensitivity or pause
  auto-tracking in the rain.
- Astral day/night scheduling (coordinates from config) with an HH:MM fallback.
- Camera-down watchdog (🔴/🟢 Telegram) and per-camera detection-alert cooldown.

### Changed
- A recognized face breaks through the per-type alert cooldown: a face is new
  information, not a burst duplicate (live: the camera named three known faces 40 s
  after a person alert, and the cooldown silently skipped the richest event of the day).
- New per-camera `sd_span_cap`: camera events run ~2 min, but the SD follow-up window
  was capped at 48 s for the Pi Zero download budget, so a subject appearing in the
  later part of the event was never scanned. Hosts with faster I/O can now raise the
  cap per camera; frame spacing widens with the window so the per-event Groq call
  count stays flat.
- An SD follow-up that finds no subject in *any* extracted frame now sends nothing
  instead of a blank middle frame: with frames spanning the whole event window, an
  all-empty result means the detection was a false positive (in practice: passing cars
  at night misclassified as a person). The drop is still audit-logged.

### Fixed
- Groq vision calls were rejected by Cloudflare (HTTP 403, error 1010) because the request
  used the default `Python-urllib` User-Agent. Every call returned empty, so no scene was
  ever described or filtered. Now sends a real client User-Agent.
- RTSP snapshots are scaled to 1280 wide (lighter Groq upload and Telegram photo) and use
  ffmpeg's `-update 1` so a single fixed-name image is accepted on ffmpeg 7.x.
