# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- `tapo_monitor` package and `tapo-monitor` CLI (`check` / `run`) — one config-driven
  daemon replacing the original per-host scripts.
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

### Fixed
- Groq vision calls were rejected by Cloudflare (HTTP 403, error 1010) because the request
  used the default `Python-urllib` User-Agent. Every call returned empty, so no scene was
  ever described or filtered. Now sends a real client User-Agent.
- RTSP snapshots are scaled to 1280 wide (lighter Groq upload and Telegram photo) and use
  ffmpeg's `-update 1` so a single fixed-name image is accepted on ffmpeg 7.x.
