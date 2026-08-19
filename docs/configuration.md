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
| `alerts` | Detection cooldown, outage threshold and event-API recovery. | 120 s cooldown, 900 s outage threshold, 300 s event-API alert. |
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
  stall_threshold: 900
  event_failure_threshold: 300
  event_restart_threshold: 900
  event_restart_enabled: true

loop:
  event_interval: 4
  control_interval: 60
```

- `cooldown` gates repeated alerts per camera/event class after confirmed delivery.
- `outage_threshold` avoids alerting on brief network gaps.
- `event_failure_threshold` alerts when a connected camera's `getEvents` call keeps failing. Network reachability alone is not enough:
  a camera can answer configuration calls while its event endpoint is unavailable.
- `event_restart_threshold` requests at most one API reboot during one event-API failure
  episode. Set `event_restart_enabled: false` when reboots would interrupt a recorder or
  require operator approval.
- `stall_threshold` guards the daemon itself: if every tick has *raised* for this long, a
  🔴 goes out. The camera watchdog runs inside the tick, so when the tick is what broke,
  only this one is left to notice — without it a daemon logging an exception every poll
  looks exactly like a quiet night. It shares the process it guards, so it deliberately
  covers that one failure and not the others: a tick that *hangs* never returns to it, and
  a crash loop under `Restart=always` resets the timer on each restart. Out-of-process
  liveness (systemd `WatchdogSec`) is what covers those.
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
rotate: 0
```

- `cloud_password_env` is optional and falls back to the camera password. Some SD download
  paths need the TP-Link account password rather than the local camera account.
- `rtsp_stream` is normally `stream1` (HD) or `stream2` (SD).
- Raise `rtsp_timeout` on slow hardware or streams with a long keyframe interval.
- `rotate` (0/90/180/270, clockwise) straightens a physically mis-mounted camera at frame
  capture across every source (RTSP, local recording, SD), so the scorer, subject crop and
  Telegram all see an upright frame. Use it when the camera's own flip setting is unavailable.

### Detection

```yaml
detection:
  sources: [getevents]
  strict_people: true
```

`getevents` is the production-wired daemon source for mains cameras, and `hubpoll` (below)
for battery cameras that record to a hub. `onvif` and `motion` are accepted by the
configuration model for ongoing research but do not currently run as independent daemon
event sources; never configure them as the only source. `strict_people: true` prioritizes
camera-confirmed people while still allowing bare motion to enter the scorer/follow-up
funnel where configured.

### Battery cameras on a hub (`hubpoll`)

```yaml
detection:
  sources: [hubpoll]

hub_host: 192.0.2.60
hub_user_env: HUB_EMAIL         # Tapo account e-mail (env var name, not the value)
hub_password_env: HUB_PASSWORD  # Tapo account password
go2rtc_src: gate                # go2rtc stream name for this camera
hub_poll_interval: 20           # seconds between hub polls
hub_device_mac: null            # optional: pin the hub-side addressing
hub_device_id: null
```

A battery camera with no usable SD keeps no event index of its own: its recordings — and
therefore its detections — live on the hub it is bound to, and it sleeps between events.
So `hubpoll` reads new clips from the hub and grabs the alert frame from a **go2rtc**
sidecar, which speaks the camera's native protocol (these cameras serve no usable RTSP).
The camera is awake for the duration of a real event, which is exactly when the frame is
wanted.

Notes and constraints:

- **Addressing is discovered.** The hub's paired-device list hands over each camera's
  device id and MAC at startup; the camera is matched by that MAC, that id, or by an alias
  equal to the camera's `name`, and a hub with exactly one camera needs no hint at all.
  With several cameras and no match the daemon refuses to guess — pin `hub_device_mac`.
- **One held session per hub.** The hub's handshake is rate-limited while queries inside an
  established session are reliable, so cameras sharing a hub share one session, and every
  failure backs off exponentially. Being evicted (the phone app takes the slot) is normal
  and never raises an alert.
- **No ICMP watchdog.** A sleeping camera answers almost no pings, so `hubpoll` cameras are
  excluded from the unreachable-camera alert; their liveness comes from the hub poll.
  They also skip the stok login entirely — there is none to make.
- **No `rotate`.** go2rtc delivers a finished JPEG and there is no capture-time filter
  behind it, so a rotation here is rejected rather than silently ignored.
- On the first pass the cursor starts at *now*: a hub full of stored clips is not replayed
  as an alert storm. Clips are scored and gated exactly like bare motion elsewhere.

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
sd_motion_span_cap: 48
sd_motion: false
sd_jobs_per_tick: 1
```

- `sd_snapshot` gives confirmed events an event-time follow-up when the live frame fails or
  appears empty.
- `snapshot_source: sd` downloads from the camera card.
- `snapshot_source: recording` reads a local continuous recorder and requires
  `sd_snapshot: true`.
- `sd_span_cap` bounds the event window downloaded/scanned.
- `sd_motion_span_cap` bounds the first window for unconfirmed motion/PIR; one empty
  result retries with `sd_span_cap`. Confirmed person events use the full window directly.
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
  motion_send_threshold: 0.60

crop_to_subject: true
```

With `tiles > 1` the service also scores a tiles×tiles grid, but the grid only refines
*localisation*: the send-decision `person` score always comes from the full frame; the
returned `animal` score is audit telemetry only. The best tile contributes the person
`box` (for `crop_to_subject`) plus a
diagnostic `tile_person` score. Blown-up tile crops of night IR grain routinely
hallucinate 0.3–0.6 "person" scores, so tile scores never gate alerts.

`motion_send_threshold` adds multi-frame corroboration for **bare (non-PIR) motion**.
An empty IR scene hallucinates a 0.3–0.6 "person" score on one frame and not the next,
while a real subject persists across frames. So, for non-PIR motion:

- a frame `>= motion_send_threshold` sends immediately (a clear single frame);
- a frame in `[threshold, motion_send_threshold)` is *held* until a second frame in that
  band corroborates it within the sampler window (needs `sampler.enabled`), otherwise it
  is dropped when the group closes;
- camera-confirmed people and PIR-backed motion are unaffected — they keep the immediate
  path, so a confirmed person is never delayed or dropped.

Unset (the default) keeps the legacy behaviour: any motion frame `>= threshold` sends.

- `threshold` is the minimum **person** confidence from 0 to 1; animal confidence never
  triggers an alert.
- `tiles: 1` scores only the whole frame; larger values also score a grid.
- `crop_to_subject` uses the best returned person box and safely falls back to the full
  frame.
- `crop_min_frac` sets the smallest crop the zoom may produce (see below).
- Scorer errors fail open (one automatic retry, then raw passthrough — never a silent drop).

### How small the zoom may get

```yaml
crop_to_subject: true
crop_min_frac: 0.12     # default 0.22
```

A padded box around a distant person is tiny, and an image that small arrives as a postage
stamp — so the crop has a floor, expressed as a fraction of the frame. The floor is also a
cap on the zoom, and which of the two matters depends entirely on how far the scene
reaches. On a camera watching a yard where people cross at 30–90px wide, the default 0.22
is the binding constraint on *every* alert: it forces a 282px crop around a 40px subject,
so the person covers a few percent of the delivered photo and the rest is scenery.

Lower it only as far as the delivered pixels allow. With `crop_from_native` the crop is cut
from a frame several times wider, so the same fraction buys proportionally more pixels: at
0.12 on a 4K camera the crop is ~460px of real detail, while 0.10 drops most crops under
400px, which starts to look like a thumbnail. Measure before choosing — the archived
whole-scene copies in the sent log (`TAPO_SENT_LOG_DIR`) can be re-scored offline, so the
floor can be picked from that camera's own subject sizes rather than guessed.

### Cropping at native resolution

Frames are normally downscaled to 1280 wide at capture, which is where it is cheapest. A
crop taken from such a frame is exactly as coarse as the frame it came from: a figure
occupying 5% of the width is ~64px across at 1280, but ~190px at 4K.

`crop_from_native: true` grabs the frame at the camera's own resolution, crops it, and
reduces only the result. Full-resolution bytes never leave the device — both the zoom sent
to Telegram and the whole-scene copy kept in the sent log are downscaled after the crop.

The reduction happens at the grab, not at the send, so only the crop ever sees the native
frame: the scorer, the captioner and Telegram all keep receiving delivery-width images.
That matters because the service resizes every request to its own input size regardless, so
a 4K frame buys no accuracy while costing the *shared* scorer 2–3x per request (measured
4.7–7.0 s versus 2.4 s, against a default `scorer.timeout` of 10 s — a timeout degrades
that frame to unfiltered passthrough). Both frames come from a single grab, so nothing
moves between where the subject is scored and where it is cropped.

It is off by default because the grab is the expensive part, and the cost depends entirely
on the hardware. Measured on two live cameras, both offering 3840×2160 on `stream1` and
1280×720 on `stream2`, three grabs each:

| Device | Stream grabbed | Time per grab |
| --- | --- | --- |
| Pi 4 Model B | `stream1`, 4K, downscaled | 2.3–2.5 s |
| Pi 4 Model B | `stream1`, 4K, native | 2.46–2.49 s |
| Pi Zero 2 W | `stream1`, 4K, downscaled | 4.5–9.7 s (median 5.4) |
| Pi Zero 2 W | `stream1`, 4K, native | 5.3–6.0 s (median 5.9) |
| Pi Zero 2 W | `stream2`, 720p, downscaled | 3.3 s |
| Pi Zero 2 W | `stream2`, 720p, native | 3.3 s |

Compare within a stream, not across streams: the flag changes whether the grab is scaled,
never which stream is grabbed. On the same 4K stream the native grab costs about +0.5 s on
a Pi Zero — and is the *steadier* of the two, the scaled variant having produced the single
worst sample of the run (9.7 s). The much larger gap between `stream2` and `stream1` is the
price of the higher-resolution stream itself, which a camera sampling `stream1` pays either
way.

On top of the grab the flag adds one local reduction and one dimension probe per frame
(~0.8 s on the Pi 4, ~1.0 s and ~1.3 s on the Pi Zero), and leaves the scorer's own cost
unchanged. Note this is per *frame*, not per alert: the sampler grabs several per event.

The decode dominates and the downscale is nearly free, so wherever the stream really is
higher-resolution, the detail costs little. A stream that is already at or below the
delivery width is detected right after the grab and skips the twin altogether — enabling
the flag on a camera whose hot path is a 720p substream therefore costs only the probe,
not a second encode. Still worth measuring on your own hardware: the grab runs under
`rtsp_timeout` (default 15 s), and the reduction under its own 20 s cap.

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

## Reliability

```yaml
reliability:
  enabled: true
  auto_fix: true
  allowed_repairs: [person_detection, vehicle_detection, smarttrack]
  storage_health: true
  latency_metrics: true
  recorder_max_age: 300
```

- `enabled` adds layered health, recorder continuity and aggregate latency observations to
  the Digital Twin state.
- `auto_fix` is limited by `allowed_repairs`; unknown or unsupported capabilities never
  trigger a write.
- The allow-list covers only person detection, vehicle detection and SmartTrack. Firmware,
  network, credentials, storage formatting and PTZ calibration are never self-healed.
- `recorder_max_age` marks a local recorder stale after the configured number of seconds;
  it does not treat normal loop-recording disk usage as a failure.

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
