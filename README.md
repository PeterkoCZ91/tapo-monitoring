# tapo-monitoring

A config-driven monitoring stack for TP-Link Tapo PTZ cameras (developed and validated
on the C560WS). It detects people, optionally describes the scene with an AI vision
model, and sends Telegram alerts — running happily on a Raspberry Pi.

> **Status:** consolidating from a set of per-host scripts into a single library
> (`tapo_monitor`) plus one config-driven daemon. See [`docs/capabilities.md`](docs/capabilities.md)
> for the full capability catalog and the migration plan.

## What it does

- **Day/night scheduling** from astral sunset/sunrise (coordinates from config), with a
  fixed HH:MM fallback.
- **Auto-tracking** with people-only SmartTrack at night; a static preset by day.
- **Detection** from ONVIF events, the camera's on-device AI (`getEvents`), and/or motion.
- **AI descriptions** of detections via Groq vision, with a sharpest-frame snapshot and
  an optional face crop.
- **Weather gating** — in the rain, lower motion sensitivity or pause auto-tracking so the
  camera stops chasing raindrops (open-meteo, cached, with hysteresis).
- **Telegram alerts** for detections plus operational (camera-down) notifications.
- **Multi-camera** from one config; perimeter hand-off between grouped cameras (planned).

## Privacy

This is **passive surveillance only** — it observes and notifies; it never triggers the
camera's siren/floodlight/speaker. No personal data lives in the repository: coordinates,
hostnames and secrets all come from your own configuration. Copy
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

## Layout

```text
tapo_monitor/            # the library + daemon
  config.py              # validated config model loaded from cameras.yaml
  scheduling.py          # astral day/night (+ HH:MM fallback)
  weather.py             # rain gating (cached, hysteresis); coordinates from env
  tracking.py            # auto-track / SmartTrack decisions + firmware-safe apply
  detection.py           # unified event classification (ONVIF / getEvents / motion)
  enrich.py              # AI scene description (Groq) + frame selection
  notify.py              # Telegram + camera-down watchdog
  camera.py              # lockout-aware pytapo connect + getEvents helpers
  daemon.py              # per-camera state machine (config-driven)
  cli.py                 # `tapo-monitor check|run`
cameras.example.yaml     # configuration schema (placeholders only)
docs/capabilities.md     # what Tapo detection can do + what this project adds over pytapo
systemd/                 # unit templates (generic; %H hostname, env-driven)
tests/                   # pytest suite
```

The camera-control automation (day/night/rain tracking) runs today via the daemon; the
detection → enrich → notify pipeline is being folded into the daemon loop next.

## Development

`pytest -q` runs the suite; `ruff check .` lints. CI runs both on every push and PR.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`SECURITY.md`](SECURITY.md). Licensed under
the terms in [`LICENSE`](LICENSE).
