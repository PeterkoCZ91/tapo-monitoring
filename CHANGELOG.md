# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- `night_window.py` — astral-based `is_night()` (sunset/sunrise window) with HH:MM fallback when env vars are missing or `astral` is not installed
- `groq_watch.py` — daytime daemon that polls `pytapo.getEvents()`, downloads SD-card clip via `pytapo.Downloader`, runs Groq vision, and posts a Telegram alert; falls back to RTSP snapshot when the SD pipeline fails
- `tapo.py` — CLI for ad-hoc inspection (`events`, `clip`, `recordings`, `status`, `face`)
- `tests/` — pytest suite covering pure logic in `camera_automation.py` and `person_monitor.py`
- `camera_automation.py` — `DAY_PRESET` / `NIGHT_PRESET` env override for day/night home positions
- Face ID enrichment in alerts via `event_info[].face_id` mapped through `FACE_ID_NAMES`
- Groq zoom validation — skip zoom photo when the camera auto-tracked off-target
- `systemd/groq-watch.service` unit

### Changed
- `camera_automation.py` — day/night switching now uses `night_window.is_night()` (astral sunset−30 → sunrise+30, location via env) instead of hardcoded HH:MM
- Telegram caption time now reflects the **event time** rather than the delivery time (groq_watch.py: from `event["start_time"]`; person_monitor.py: from `event["start_time"]` for the getEvents fallback, ONVIF path keeps `now` since it is near-realtime)
- Pre-existing scripts and discovery tools (`person_monitor.py`, `camera_automation.py`, `scripts/*`) — moved from old "Added" section after sustained production deployment

### Fixed
- `groq_watch.py` — `dl_start = max(clip startTime, raw_start)` to avoid duplicate frames from blind clip start
- `groq_watch.py` — `Tapo(..., TAPO_PASSWORD)` as the 4th param fixes HTTP 401 on SD download (was passing email)
- `camera_automation.py` — `write_state()` failure is now fatal instead of silent
