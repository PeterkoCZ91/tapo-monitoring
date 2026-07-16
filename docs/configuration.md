# Configuration reference

One YAML file configures the fleet. Copy `cameras.example.yaml` to a git-ignored
`cameras.yaml`, then validate it before starting the daemon:

```bash
cp cameras.example.yaml cameras.yaml
tapo-monitor check cameras.yaml
```

The configuration stores environment-variable **names**, not secret values. For example,
`token_env: TELEGRAM_TOKEN` means “read the token from `$TELEGRAM_TOKEN` at runtime.”

## Minimal configuration

```yaml
telegram:
  token_env: TELEGRAM_TOKEN
  chat_id_env: TELEGRAM_CHAT_ID

loop:
  event_interval: 4
  control_interval: 60

cameras:
  - name: front
    host: "<LAN_CAMERA_IP>"
    role: tracking
    schedule: always_night

    user_env: CAM_USER
    password_env: CAM_PASSWORD
    rtsp_user_env: CAM_RTSP_USER
    rtsp_password_env: CAM_RTSP_PASSWORD

    detection:
      sources: [getevents]
      strict_people: true

    tracking:
      smarttrack: [people]
      day_preset: "2"

    weather:
      strategy: none

    enrich:
      groq: false
```

Required environment values:

```bash
export TELEGRAM_TOKEN=...
export TELEGRAM_CHAT_ID=...
export CAM_USER=...
export CAM_PASSWORD=...
export CAM_RTSP_USER=...
export CAM_RTSP_PASSWORD=...
```

The camera API and RTSP credentials may refer to the same Tapo camera account, but the
fields remain separate because deployments and firmware differ.

## Camera prerequisites

Before running the daemon:

1. enable third-party compatibility in the Tapo app where the model requires it;
2. create a dedicated camera account under the camera's advanced settings;
3. verify RTSP independently if the model exposes it;
4. set a cloud-account password only when camera SD downloads require it;
5. stop other integrations that concurrently control the same camera while testing.

Repeated invalid logins can trigger a firmware lockout. Validate credentials manually
instead of restarting the daemon in a tight loop.

## Top-level sections

| Section | Purpose | Default behavior |
| --- | --- | --- |
| `location` | Astral day/night and weather coordinates/timezone. | Empty: fixed fallback schedule; weather cannot query a location. |
| `telegram` | Names of token and chat-ID environment variables. | Empty values make delivery unavailable. |
| `groq` | Name of optional Groq API-key environment variable. | No key: captioning unavailable. |
| `faces` | Name of a local face-ID-to-label mapping environment variable. | No face labels. |
| `alerts` | Detection cooldown and outage threshold. | 120 s cooldown, 900 s outage threshold. |
| `loop` | Fast event and slow control cadence. | 4 s events, 60 s control. |
| `observability` | Digital Twin and Shadow Auditor switches. | Entirely off. |
| `cameras` | Non-empty list of camera definitions. | Required. |

### Location

```yaml
location:
  lat: <LATITUDE>
  lon: <LONGITUDE>
  tz: <IANA_TIMEZONE>
```

Use real values only in the private `cameras.yaml`. Latitude/longitude drive sunset,
sunrise and weather queries. `tz` must be an IANA timezone name.

### Notifications and optional enrichment

```yaml
telegram:
  token_env: TELEGRAM_TOKEN
  chat_id_env: TELEGRAM_CHAT_ID

groq:
  api_key_env: GROQ_API_KEY

faces:
  names_env: FACE_ID_NAMES
```

`FACE_ID_NAMES` contains a comma-separated local mapping such as
`camera-id:label,camera-id:label`. Do not place that value in YAML, logs or the repository.

### Alerts and cadence

```yaml
alerts:
  cooldown: 120
  outage_threshold: 900

loop:
  event_interval: 4
  control_interval: 60
```

- `cooldown` gates repeated alerts per camera/event class after confirmed delivery.
- `outage_threshold` avoids alerting on brief network gaps.
- `event_interval` controls `getEvents` latency on the existing client.
- `control_interval` controls ping/reconnect and camera plan re-application.

Keep `control_interval` substantially wider than `event_interval`; reducing it increases
authentication pressure without improving event latency.

## Camera fields

### Identity and role

| Field | Values | Notes |
| --- | --- | --- |
| `name` | short unique string | Used in logs, state and notifications. Required. |
| `host` | LAN address/DNS name | Required; keep real values private. |
| `role` | `tracking`, `static` | Tracking cameras follow policy; static cameras remain parked. |
| `schedule` | `astral`, `always_night`, `always_day` | Controls day/night plan selection. |
| `night_only` | boolean | Drains events but suppresses all Telegram traffic during daytime. |
| `night_vision` | `ir`, `auto`, unset | Force IR by schedule, re-assert camera auto mode, or leave untouched. |

### Credentials and streams

```yaml
user_env: CAM_USER
password_env: CAM_PASSWORD
cloud_password_env: CAM_CLOUD_PASSWORD

rtsp_user_env: CAM_RTSP_USER
rtsp_password_env: CAM_RTSP_PASSWORD
rtsp_port: 554
rtsp_stream: stream1
rtsp_timeout: 15
```

- `cloud_password_env` is optional and falls back to the camera password. Some SD download
  paths need the TP-Link account password rather than the local camera account.
- `rtsp_stream` is normally `stream1` (HD) or `stream2` (SD).
- Raise `rtsp_timeout` on slow hardware or streams with a long keyframe interval.

### Detection

```yaml
detection:
  sources: [getevents]
  strict_people: true
```

`getevents` is the production-wired daemon source. `onvif` and `motion` are accepted by the
configuration model for ongoing research but do not currently run as independent daemon
event sources; never configure them as the only source. `strict_people: true` prioritizes
camera-confirmed people while still allowing bare motion to enter the scorer/follow-up
funnel where configured.

### Tracking and detection policy

```yaml
person_sensitivity: 40

tracking:
  smarttrack: [people]
  day_preset: "2"
  night_preset: "1"
```

`smarttrack` accepts `people`, `vehicle`, `pet` and `baby` where firmware supports them.
The current control policy re-asserts person detection, disables vehicle detection and
applies auto-track last. `person_sensitivity` is an optional integer from 0 to 100; unset
leaves the existing person sensitivity unchanged.

### Weather policy

```yaml
weather:
  strategy: lower_sensitivity
  motion_normal: 60
  motion_rain: 20
  precip_threshold: 0.1
  clear_delay: 1800
  poll_interval: 900
  storm_park: false
```

Strategies:

- `none` — no weather query or weather-specific camera change;
- `lower_sensitivity` — use `motion_rain` while rain is active;
- `disable_tracking` — stop tracking while rain is active.

`storm_park` composes with the selected strategy and recalls the configured day preset
during rain. Weather needs top-level location coordinates.

### Live, SD and local-recorder media

```yaml
sd_snapshot: true
snapshot_source: sd
sd_span_cap: 120
sd_motion: false
sd_jobs_per_tick: 1
```

- `sd_snapshot` gives confirmed events an event-time follow-up when the live frame fails or
  appears empty.
- `snapshot_source: sd` downloads from the camera card.
- `snapshot_source: recording` reads a local continuous recorder and requires
  `sd_snapshot: true`.
- `sd_span_cap` bounds the event window downloaded/scanned.
- `sd_motion` also gives PIR-backed bare motion a second chance; it can be expensive.
- `sd_jobs_per_tick` adds per-camera backpressure for slow hosts.

Local-recorder environment:

```bash
export RECORDING_ROOT=/srv/recordings
export RECORDING_MAX_AGE=300
```

Expected tree:

```text
<RECORDING_ROOT>/<camera-host>/<YYYY-MM-DD>/<HH>/*.mkv|*.mp4|*.ts
```

### Event-window sampler

```yaml
sampler:
  enabled: true
  interval: 30
  max_frames: 6
  group_gap: 90
  stream: stream1
  low_score_exit: 3
  low_score: 0.15
```

The sampler takes additional live frames across a long event. Nearby events are grouped;
`max_frames` bounds work. Use it with a local scorer to avoid sending empty follow-ups.

With a local scorer, `low_score_exit: N` closes a *motion-only* group early once `N`
consecutive follow-up frames all score below `low_score` — bursts from foliage, light or
insects stop consuming grabs. Groups with a camera-confirmed person/PIR detection always
run the full window (the sampler exists to catch a subject appearing mid-event), and a
confirmed detection arriving later reopens an early-exited group. `0` disables the exit.

### Local scorer and crop

```yaml
scorer:
  url: http://127.0.0.1:8766/score
  threshold: 0.40
  timeout: 10
  tiles: 2

crop_to_subject: true
```

With `tiles > 1` the service also scores a tiles×tiles grid, but the grid only refines
*localisation*: the send-decision `person`/`animal` scores always come from the full
frame, and the best tile contributes the person `box` (for `crop_to_subject`) plus a
diagnostic `tile_person` score. Blown-up tile crops of night IR grain routinely
hallucinate 0.3–0.6 "person" scores, so tile scores never gate alerts.

```yaml
```

- `threshold` is a confidence from 0 to 1.
- `tiles: 1` scores only the whole frame; larger values also score a grid.
- `crop_to_subject` uses the best returned person box and safely falls back to the full
  frame.
- Scorer errors fail open.

`enrich.groq` controls optional captions:

```yaml
enrich:
  snapshot: rtsp
  groq: true
```

`enrich.snapshot` is retained for configuration compatibility; the active follow-up source
is selected by `sd_snapshot` and `snapshot_source`. Keep it at `rtsp` in new configs.

### Soft pan limit

```yaml
pan_limit:
  enabled: true
  margin: 0.01
  poll_interval: 6
  onvif_port: 2020
  onvif_user_env: CAM_ONVIF_USER
  onvif_password_env: CAM_ONVIF_PASSWORD
```

The guard reads current/preset pan positions over ONVIF and recalls the nearest bounding
preset when auto-track moves outside their span. It does not create a hard motor limit.
ONVIF errors are isolated from the event loop.

### Reserved coordinator fields

```yaml
coordinator:
  group:
  handoff_preset:
```

These fields are parsed for forward compatibility. Multi-camera handoff is not implemented
yet; leave them empty. Track progress in the [roadmap](roadmap.md).

## Observability

```yaml
observability:
  digital_twin: false
  probe_interval: 900
  drift_alerts: false
  ledger: false
  ledger_retention_days: 30
  shadow_match_window: 20
```

- All observability features are opt-in.
- `probe_interval` has a 60-second minimum; probes reuse the daemon client.
- Enable `digital_twin` before `drift_alerts` and inspect several cycles.
- `ledger` stores normalized metadata and decisions, never media.
- `shadow_match_window` is the intended correlation window for integrations; the current
  CLI report accepts an explicit `--window` and defaults to the same 20 seconds.

State path overrides:

```bash
export TAPO_HEALTH_STATE_FILE=/private/path/health.json
export TAPO_TWIN_STATE_FILE=/private/path/twin.json
export TAPO_LEDGER_FILE=/private/path/events.sqlite3
```

See [Observability](observability.md) for schema, privacy and rollout details.

## Suggested profiles

### Lowest overhead

- `getevents`, `strict_people: true`
- live RTSP only
- no sampler, scorer, SD follow-up or observability
- suitable when camera AI quality is already acceptable

### Reliable alert media

- `sd_snapshot: true`
- local scorer with a conservative threshold
- optional `sampler` for long events
- `sd_jobs_per_tick: 1` on slow hardware

### Observe and calibrate

- Digital Twin on, drift alerts off initially
- ledger on with bounded retention
- independent shadow worker/CLI observations
- review reports before changing detector thresholds

## Validation and safe changes

Run after every edit:

```bash
tapo-monitor check cameras.yaml
```

Change one capability at a time. Observe logs and status before enabling the next, because
model/firmware support and camera resource limits differ. The full annotated schema remains
in [`cameras.example.yaml`](../cameras.example.yaml).
