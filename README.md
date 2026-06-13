# tapo-monitoring

A config-driven monitoring stack for TP-Link Tapo PTZ cameras (developed and validated
on the C560WS). It detects people from the camera's on-device AI, optionally describes the
scene with an AI vision model, and sends Telegram alerts — running happily on a Raspberry Pi.

> **Status:** a single library (`tapo_monitor`) plus one config-driven daemon
> (`tapo-monitor run`). The day/night/rain camera control and the detection →
> enrich → notify pipeline both run in the daemon loop today. See
> [`docs/capabilities.md`](docs/capabilities.md) for the full capability catalog.

## What it does

- **Day/night scheduling** from astral sunset/sunrise (coordinates from config), with a
  fixed HH:MM fallback. One source of truth shared by every component.
- **Auto-tracking** with people-only SmartTrack at night and a static preset by day,
  applied in a firmware-safe order (SmartTrack/sensitivity/preset first, auto-track
  asserted last — the C560WS drops the master switch otherwise).
- **Detection** from the camera's on-device AI via `getEvents`: the `events_1` bitmask is
  decoded into named signals (motion / PIR / person) and only AI-confirmed people (bit 19)
  or recognized faces alert under `strict_people`. ONVIF and motion classifiers exist in
  `detection.py` for future wiring.
- **Face labelling** — `event_info[].face_id` is mapped to names via a configured env var;
  unrecognized but stable IDs show as "unknown face".
- **AI descriptions** of detections via Groq vision on a single RTSP snapshot (scaled);
  frames the model reports as an empty scene are dropped instead of alerted.
- **Weather gating** — in the rain, lower motion sensitivity or pause auto-tracking so the
  camera stops chasing raindrops (open-meteo, cached, with hysteresis).
- **Telegram alerts** for detections (rate-limited per camera) plus operational
  camera-down 🔴/🟢 notifications.
- **Audit logging** — every camera event is logged with its decoded signal, `alarm_type`,
  face count and the resulting verdict (person / drop), so detection behaviour is always
  auditable.
- **Multi-camera** from one config; perimeter hand-off between grouped cameras (planned).

## Privacy

This is **passive surveillance only** — it observes and notifies; it never triggers the
camera's siren/floodlight/speaker. No personal data lives in the repository: coordinates,
hostnames, face names and secrets all come from your own configuration. Copy
[`cameras.example.yaml`](cameras.example.yaml) to `cameras.yaml` (git-ignored) and fill in
your values; point secret fields at environment variable names rather than inlining tokens.

## Quickstart

```bash
pip install -e ".[dev]"          # install the package + dev tools
cp cameras.example.yaml cameras.yaml
$EDITOR cameras.yaml             # set hosts, coordinates, capabilities
export TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... GROQ_API_KEY=...

tapo-monitor check cameras.yaml  # validate config + print a summary
tapo-monitor run cameras.yaml    # start the daemon
pytest -q                         # run the test suite
```

A systemd template is in [`systemd/tapo-monitor.service`](systemd/tapo-monitor.service).
Secrets are read from the environment variables named in `cameras.yaml` (e.g.
`TELEGRAM_TOKEN`), so the config file itself stays safe to share.

## Camera prerequisites & auth

Tapo cameras gate their local API; expect these one-time steps:

- **Enable third-party access.** Recent firmware refuses local logins unless
  *Tapo Lab → Third-Party Compatibility* is turned on in the Tapo app (some models
  instead require the camera to be blocked from the internet). Without it, every login
  fails.
- **Create a dedicated camera account.** In the Tapo app, *Advanced Settings → Camera
  Account*, set a username/password. These are the credentials the package logs in with
  (`user_env` / `password_env`) and the RTSP account (`rtsp_user_env` /
  `rtsp_password_env`) — usually the same pair.
- **Cloud password.** A few operations (notably SD-card clip download) need your TP-Link
  *account* password, not the camera account — set `cloud_password_env` if you use them.
- **Lockout.** Repeated failed logins lock out the source IP for ~30 minutes, and the
  first login right after a reconnect often fails before a retry succeeds. The daemon
  retries with exponential backoff so it never hammers a struggling camera — but double-
  check credentials before restarting in a loop.
- **RTSP.** Frames are pulled over RTSP (`stream1` HD, `stream2` SD) on port 554 by
  default; override with `rtsp_stream` / `rtsp_port`.

## Layout

```text
tapo_monitor/            # the library + daemon
  config.py              # validated config model loaded from cameras.yaml
  scheduling.py          # astral day/night (+ HH:MM fallback)
  weather.py             # rain gating (cached, hysteresis); coordinates from env
  tracking.py            # auto-track / SmartTrack decisions + firmware-safe apply
  detection.py           # event classification + events_1 bitmask decode
  snapshot.py            # RTSP frame capture (ffmpeg)
  enrich.py              # Groq scene description + face labelling + frame selection
  notify.py              # Telegram + camera-down watchdog + alert gating
  camera.py              # lockout-aware pytapo connect + getEvents helpers
  monitor.py             # detection pipeline (collect → snapshot → enrich → notify)
  daemon.py              # per-camera state machine tying control + pipeline together
  cli.py                 # `tapo-monitor check|run`
cameras.example.yaml     # configuration schema (placeholders only)
docs/capabilities.md     # what Tapo detection can do + what this project adds over pytapo
systemd/                 # unit templates (generic; %i user, env-driven)
tests/                   # pytest suite
```

## Development

`pytest -q` runs the suite; `ruff check .` lints. CI runs both on every push and PR.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`SECURITY.md`](SECURITY.md). Licensed under
the terms in [`LICENSE`](LICENSE).
