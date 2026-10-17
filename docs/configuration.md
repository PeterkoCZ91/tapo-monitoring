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
| `mqtt` | Outbound MQTT bridge with Home Assistant discovery. | Absent: off, nothing imported. |
| `outbox` | Keep alerts Telegram did not take and deliver them late. | Absent: off; a failed send is retried in memory for ~10 min, then lost. |
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
  A delivered photo whose person score reaches the active person threshold also suppresses
  a duplicate person SD/recording follow-up within that cooldown, even when the first
  photo came from a motion event. An empty or unscored motion photo does not suppress it.
  This delivery fact survives restart; older runtime state without it keeps the old behaviour.
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

### Host failover (optional)

`host` may be a list of candidate addresses for a camera that can show up on another
network (a different router or subnet); a plain string behaves exactly as before.

```yaml
cameras:
  - name: yard
    host: ["203.0.113.10", "198.51.100.20"]
```

The first entry is the primary. The monitor keeps one active address per camera and every
component reads it from one place (`camera.active_host`): the pytapo session, RTSP,
ONVIF, ping, the SD download and the scene probe.

- Trigger: the existing per-pass ping. After two consecutive failed passes the other
  candidates are tried in order with a plain TCP connect to the API port (443); no extra
  ping loop. The first that answers becomes active.
- The pytapo session is closed and reopened on the new address (still one session), and
  the ONVIF pan-guard client is rebuilt.
- Identity: the camera's MAC is learned once from `getBasicInfo` after the first login.
  After a switch the MAC at the new address must match; a different device is refused,
  the old address is restored and the candidate is not retried for ten minutes. If no MAC
  could be read (nothing learned yet, e.g. the camera was never up since the start, or the
  call failed) the switch is kept and logged as `unverified`. Limits: all candidates must
  accept the same credentials, and TCP on 443 only proves that something answers.
- Visibility: one `host switch` warning (with the addresses, journal only) and one
  `audit ... action=host_switch host_index=N` line (index, never an address). `/status`
  shows `host_index` for such cameras. The choice is saved in the health state file and
  restored on restart if it is still in the list.
- Not covered: the recorder paths (`RECORDING_ROOT/<host>`, outage clip preserver, shadow
  scan) keep using the first configured `host`; the CLI commands (`report`, `twin` probes)
  connect to the first address; MQTT and `hub_host` are separate settings. The camera
  never switches back on its own: to go back to the primary, take the failed-over address
  out of the list and restart (a saved choice that is still listed is restored).

### Identity and role

| Field | Values | Notes |
| --- | --- | --- |
| `name` | short unique string | Used in logs, state and notifications. Required. |
| `host` | LAN address/DNS name, or a list of them | Required; keep real values private. A list turns on [host failover](#host-failover-optional). |
| `role` | `tracking`, `static` | Tracking cameras follow policy; static cameras stay parked at `day_preset`, re-asserted every control tick. |
| `schedule` | `astral`, `always_night`, `always_day` | Controls day/night plan selection. |
| `night_only` | boolean | Drains events but suppresses all Telegram traffic during daytime. |
| `quiet_hours` | `"HH:MM-HH:MM"`, unset | Mutes INSIDE a fixed local clock window (the camera alerts outside it): detections are drained silently and all Telegram, including camera-down notices, is suppressed. The inverse gate of `night_only`, which reports only during the astral night. Wraps past midnight if end ≤ start. Mutually exclusive with `night_only`. To keep reporting all day and only switch the white lamp on in a night window, use `light_trigger` instead. |
| `night_vision` | `ir`, `auto`, unset | Force IR by schedule, re-assert camera auto mode, or leave untouched. |
| `ldc` | boolean, unset | On-sensor Lens Distortion Correction (LDC) to straighten wide-angle edges. |
| `tamper_detection` | boolean, unset | Re-asserts tamper / anti-masking detection every tick. Tamper events bypass scorer. |
| `tamper_sensitivity` | `low`, `normal`, `high` | Tamper detection sensitivity level (default `normal`). |
| `whitelamp_force_time` | 5–300, unset | Scaled duration (seconds) for floodlight pulse on detection (overrides 300 s default). |
| `whitelamp_intensity` | 1–100, unset | Floodlight pulse brightness percentage. |

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

### Data collection, privacy and detection notices

Per-camera switches:

```yaml
    telegram_alerts: false          # decide and record every alert as usual, send no photo
    privacy_notice: true            # one Telegram message when privacy mode goes on / off
    detection_notice: true          # one message when motion/person detection goes off / on
    app_notifications_notice: true # announce changes to the app notification switch
    follow_app_notifications: true  # no alert photos while the Tapo app's notifications are off
```

- `telegram_alerts: false` keeps a camera in data-collection mode: every decision, the
  sent log, cooldowns, audits and the incident statistics are exactly what a live camera
  produces, but no alert photo leaves the host. System notices (privacy, outage, drift,
  disk) still go to Telegram. Unlike `quiet_hours`, the pipeline keeps working, so the
  collected data stays representative.
- `privacy_notice: true` reads the privacy switch on every control pass (every
  `control_interval`, 60 s by default) — also on a static camera that never moves its lens
  — and sends "🔒 camera '…' is in privacy mode — not watching" once when the lens is
  parked and "🔓 … watching again" when it comes back. The last announced state is kept in
  the runtime state, so a restart neither repeats nor misses a change; a failed send is
  retried on the next pass.
- `detection_notice: true` reads motion and person detection on every control pass (two
  getter calls) and tells Telegram when either is switched off in the Tapo app:
  - Motion detection is never re-asserted by the daemon, so it works like the privacy
    notice: "🚫 camera '…': motion detection is switched off" once, "✅ … is back on"
    once. A fresh start announces only "off".
  - Person detection is re-asserted ON every pass (the self-heal against a switch that
    drops by itself). It is read *before* that, so a switch flipped off in the app is
    still seen: "↩️ … person detection was switched off — switched it back on" is ONE
    message, not an off/on pair. These are capped at one per camera per 24 h; repeats in
    between (something turning it off before every pass, or on every restart) are
    counted into the next message. Only when the self-heal is refused by the camera or
    not allowed (`reliability.allowed_repairs` without `person_detection`) does the
    switch stay off; that is announced once as off and once as back on.
  - On a dual-lens camera (`event_profile: c545d`) both switches are read per lens, from
    the same reading the per-lens self-heal uses (no extra call). A switch off on one lens
    only is announced as off and names it ("… person detection was switched off on the
    pan/tilt lens — switched it back on").

  The last announced state is kept in the runtime state; a failed send is retried.
- `app_notifications_notice: true` reports when the camera's Tapo app notification
  switch goes off or comes back on, without changing Telegram photo delivery. Keep
  `follow_app_notifications: false` for notice-only behavior. It reads the same local
  API switch described below, not the phone's Do Not Disturb mode or an app-only
  schedule. Unknown readings do not trigger a notice. A fresh start reports only off;
  successful announcements survive a restart, and failed sends retry next pass.
- `follow_app_notifications: true` reads the Tapo app's notification switch for the
  camera (`getMsgPushConfig`, `chn1_msg_push_info.notification_enabled`) on every control
  pass. While it is `off`, alert photos are handled exactly as with
  `telegram_alerts: false` — recorded, not sent — and system notices (outage, privacy,
  detection, drift) still go out. The change takes effect within one control pass. An
  unreadable or unknown answer never silences anything and is logged once per camera;
  fields next to the switch that tapo-monitor does not interpret (a notification
  schedule, if a firmware exposes one) are logged once and ignored. Tested firmware
  exposes only the on/off switch over the local API, no schedule: a schedule the app
  keeps is not visible to the daemon.

## Dual-lens cameras (C545D)

```yaml
event_profile: c545d       # default: "default"
rtsp_stream: stream2       # wide lens, 1280x720: the fast live grab
lens_pick_stream: stream7  # optional, default unset: also grab the pan/tilt lens
lens_album: true           # optional, default false: send both lens frames as an album
sampler:
  enabled: true
  stream: stream6          # follow-up grabs from the pan/tilt lens
```

`event_profile` says how a camera model's `getEvents` fields are read (the table is
`EVENT_PROFILES` in `tapo_monitor/detection.py`). `default` is the C560WS/C260 reading
every camera had before. `c545d`:

- reads `alarm_type` 6 / `events_1` bit 5 (value 32) as a **confirmed person** — the
  C545D has no PIR and sends that pair for a person (observed, n=10) — so it alerts on the
  person path, like the AI-person bit under `strict_people`. On `default` the pair stays
  PIR-backed bare motion;
- knows channel 2 is a pan/tilt lens the firmware moves itself (dual-cam linkage): an
  event on it keeps the scheduled preset recall and the pan-limit guard off the lens for
  180 s after the event ends, whether auto-track is on or not;
- makes the digital twin read both lenses (`chn_id`) and the linkage state;
- makes the self-heal work per lens. A setter sent without `chn_id` reaches the wide lens
  only, so each control pass reads motion, person and vehicle detection of both lenses
  (three getter calls with `chn_id: [1, 2]`) and writes only to a lens that drifted:
  motion sensitivity (the number alone — a write carrying the `sensitivity` label is
  overridden by the label), person detection on (with `person_sensitivity` when set) and
  vehicle detection off. A lens that cannot be read is written anyway. A camera in tune
  gets the three reads and no write; a single-lens camera keeps the chn-less writes it
  always had. Tamper, LDC, day/night, night-vision mode and white lamp are still sent
  without `chn_id` and reach the wide lens only; SmartTrack and auto-track are
  device-wide.

Events of either shape are normalized for every camera: a `chn_events` entry becomes a
top-level `events_1` (OR of the lenses) plus `channels`, which audit lines show as
`channels=1,2`.

`lens_pick_stream` is off unless set. When an event fired on the pan/tilt lens, the live
pass grabs `rtsp_stream` and this stream, scores both and keeps the frame with the larger
subject (the `sd_frame_pick: largest` rule), or the higher score when a box is missing.
It requires an `event_profile` with a pan/tilt lens and `scorer.url`, costs a second grab
and a second scoring on those events only, and must differ from `rtsp_stream`.

`lens_album` (default `false`, needs `lens_pick_stream`) sends both lens frames as one
Telegram album instead of the picked frame alone, when the subjects differ: both frames
score at or above `scorer.threshold` and the scorer counts a different number of people in
each (`persons`). Box overlap is not used, because the lenses have different fields of
view. A scorer that reports no count never triggers an album. The album is one message
(caption on the first photo, `· wide + pan/tilt lenses`), one cooldown and one incident
ID; both frames go to the sent log under that incident, whole (no subject crop). If
Telegram refuses the album, the picked frame is sent alone as before. Otherwise nothing
changes.

The alert caption of a dual-lens camera names the lens that took the photo, e.g.
`👤 2026-09-27 12:00:00 · pan/tilt lens`. The lens follows from the stream the frame was
grabbed from (`stream1`/`stream2`/`stream8` wide, `stream6`/`stream7` pan/tilt), on the
live, lens-pick and sampler paths and on a live fallback of the SD follow-up. A frame from
the SD card or a local recording names no lens: nothing says which lens it shows.

The C545D keeps its event index on the SD card, so `sources: [getevents]` needs a card.
`role: static` (auto-track off) does not turn the firmware's lens linkage off: the
pan/tilt lens still turns after a person by itself. See
[capabilities](capabilities.md#7-dual-lens-cameras-tapo-c545d).

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
inactivity_alert_days: null     # optional, 1-90: one Telegram notice after this many days without a clip
hub_clip_worker: false          # optional: download hub clips on a background thread
```

`hub_clip_worker: true` moves the clip download and frame extraction off the main loop.
By default each new clip is fetched on the loop (a 10 s stall timeout per download, one
retry, then a live frame), so while it runs no other camera on the host is polled. With
the option, a new clip is queued and saved with the runtime state before the hub cursor
moves past it, and one worker thread per hub downloads it (a hub tolerates a single
session, so downloads never overlap); the loop then scores, gates and sends exactly as
before. A job that fails, returns no frame or runs past 150 s is replaced by a live
frame, a queued clip is read again after a restart, and one older than ten minutes is
dropped. Scoring and the Telegram send stay on the loop. Off by default and unchanged
when off.

A battery camera with no usable SD keeps no event index of its own: its recordings — and
therefore its detections — live on the hub it is bound to, and it sleeps between events.
So `hubpoll` reads new clips from the hub and grabs the alert frame from a **go2rtc**
sidecar, which speaks the camera's native protocol (these cameras serve no usable RTSP).
The camera is awake for the duration of a real event, which is exactly when the frame is
wanted.

Notes and constraints:

- **Inactivity notice (optional).** `inactivity_alert_days: N` (1-90, default off) sends one
  Telegram text when the hub has indexed no clip for the camera for more than N days. The
  newest clip is asked from the hub at most once a day (a failed query sends nothing and is
  retried after an hour); after a notice the next needs a newer clip or another N days of
  silence. It honours `night_only`/`quiet_hours`. State: `hub_inactivity.json` beside the
  hub cursor file.
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
- **24/7-capture cameras are not supported yet.** `hubpoll` assumes every indexed clip is a
  triggered recording. If the hub reports a camera as recording continuously (e.g. a C460
  with 24/7 Capture enabled, unlike the event-only C410 this path was built against), the
  daemon logs a warning once and never polls that camera for clips, rather than alerting on
  every recorded segment.

### Tracking and detection policy

```yaml
person_sensitivity: 40

tracking:
  smarttrack: [people]
  day_preset: "2"
  night_preset: "1"
  back_time: 180
  track_hold: 180
```

`smarttrack` accepts `people`, `vehicle`, `pet` and `baby` where firmware supports them.
The current control policy re-asserts person detection, disables vehicle detection and
applies auto-track last. `person_sensitivity` is an optional integer from 0 to 100; unset
leaves the existing person sensitivity unchanged.

#### Night dwell: how long the camera keeps watching

Auto-track swings the lens onto whoever walks past and then two separate timers pull it
back: the camera's own `back_time` (30 s out of the box on the C560WS) and our preset
recall on the next control pass (≤ 60 s). Whichever is shorter wins, which is why raising
one alone does nothing at all.

That matters wherever the SD recording is what actually gets reviewed. The Telegram frame
says *someone was here at 02:14*; the clip is where you find out what they were doing —
and a lens pulled home after 30 s spends the rest of that clip showing an empty preset.

`back_time` is written in the same `setTargetTrackConfig` request that asserts the
auto-track switch, deliberately: `setSmartTrackConfig` silently clears that switch, so
nothing may run between `apply_smarttrack` and the assert, and a separate call for the
dwell would sit in exactly that gap. A camera that refuses the combined payload still gets
tracking asserted the ordinary way, and a `back_time` that reads back unchanged is logged
rather than assumed — a camera quietly keeping its 30 s is indistinguishable from one that
took 180, and in that state the dwell is worth nothing.

`track_hold` holds our own recall for the same stretch. Every fresh event re-arms it, so a
passage keeps the lens on its subject, but one unbroken hold never runs longer than
`track_hold`: a through-location fires events all evening and would otherwise never see
its preset recalled again — and that recall is the only thing that corrects tilt, since
`pan_limit.tilt` is off by default. Held recalls are logged per pass.

**Night only, and that is the whole safety story.** The hold is honoured only while the
plan actually tracks, which on a `role: tracking` camera means night, not parked in a
storm. With tracking off, the preset recall is the *single* mechanism that repairs a
drifted or nudged aim: one camera here sat pointed at asphalt for two days (2026-08-18..20)
because nothing in the system could bring it back. A hold that leaked into the day would
rebuild that failure silently, so it is gated on the plan, not on a clock.

`pan_limit` keeps working throughout, but gives the hold a short grace. A dwell does not
license the camera to sit staring into a wall or a neighbour's window: if auto-track drags
it outside its preset span, the guard recalls it once the lens has been continuously out of
bounds for `pan_limit.hold_grace` seconds (default 20; `0` recalls at once, the behaviour
before the grace existed). Without a running hold the guard recalls within a
`poll_interval` as always. Both paths ask one arbiter (`tapo_monitor.motion`) before
moving: privacy mode blocks every move, the hold blocks the scheduled recall, the guard
overrides the hold after the grace. Refused moves are counted per camera. The control pass
reads the privacy switch itself (one `getPrivacyMode` call on the already-connected client,
for cameras with a preset or the guard), so a recall is skipped on the very pass privacy
goes on and the aim is restored on the first pass after it goes off; an unreadable switch
falls back to the twin's last probe and is never taken for "parked".

Setting `track_hold` without `back_time` is warned about at startup — the firmware would
still swing the lens home on its own timer, so the hold buys no footage.

### Hardware enhancements (LDC, tamper, whitelamp)

```yaml
ldc: true
tamper_detection: true
tamper_sensitivity: normal
whitelamp_force_time: 30
whitelamp_intensity: 100
```

- `ldc` (`true`/`false`, default unset) — enables on-sensor Lens Distortion Correction to remove
  wide-angle fisheye/barrel distortion. Straightening perimeter lines prevents edge-of-frame
  distortions from lowering YOLO person detection confidence.
- `tamper_detection` (`true`/`false`, default unset) and `tamper_sensitivity` (`low`/`normal`/`high`, default `normal`) —
  re-asserts tamper monitoring every control tick so that lens spray, physical covering, or
  camera diversion cannot remain quietly turned off. In the alert funnel, tamper events bypass
  visual object confidence gates and alert immediately.
- `whitelamp_force_time` (5 to 300 seconds, default unset) — overrides the 300-second (5 minute)
  firmware floodlight timer on detection triggers, allowing polite, short night pulses (e.g. 30 s).
- `whitelamp_intensity` (1 to 100 %, default unset) — sets the floodlight brightness level.

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
sd_early_look: false
sd_early_span: 18
sd_fresh_guard: 60
```

- `sd_snapshot` gives confirmed events an event-time follow-up when the live frame fails or
  appears empty.
- `snapshot_source: sd` downloads from the camera card.
- `snapshot_source: recording` reads a local continuous recorder and requires
  `sd_snapshot: true`. A window is read 15 s after it ends (the recorder trails the
  wall clock by a few seconds). A window longer than 28 s is read in two parts: the
  event's first 24 s as soon as they are on disk, then the rest at the usual time only
  when the first part shows no subject, so a subject already in view is alerted about
  40 s after the event starts. The camera card (`sd`) keeps one window behind pytapo's
  60 s freshness guard unless `sd_early_look` is on.
- `sd_early_look: true` (camera card only, off by default) splits a card window of 24 s
  or more the same way: the first 18 s from the start of the camera's recorded segment
  (frames at 0, 6 and 12 s) are downloaded as soon as they clear the freshness guard,
  about 87 s after the event starts instead of 105 s, and the download itself is about
  half as long. Only when that look shows no subject is the rest of the window read, from
  18 s into the same segment, at the time the whole window would have been read. On two
  sites the photo a card follow-up sent lay in the first 12 s of its window in about
  90 % of cases, so most photos stay the same and arrive earlier; the rest arrive about
  as late as before. An early look that reads no frames at all (a failed download or
  lookup) is followed by a read of the whole window from the start, at the usual time.
  When the look showed frames of nobody and the rest yields none, the follow-up is
  dropped like any window without a subject, never replaced by a live grab. Each read
  opens its own camera session, so an empty early look costs a second login a few
  seconds later. With the early look on, a failed segment lookup is also tried once more
  after 5 s. A card read logs one `SD read` line with its window, bytes and the
  download and extraction seconds.
- `recording_early_span` (`snapshot_source: recording`, default 24, 6-60) is the length in
  seconds of the first look at the event's opening seconds, and `recording_ready_margin`
  (default 15, 5-60) is how long after a window's end it is read. The defaults are the
  previous fixed values, so omitting both changes nothing. A window must be at least one
  4 s frame interval longer than the first look to be split. Values outside the ranges are
  a `ConfigError`. Lowering the margin reads the window sooner but the recorder trails the
  wall clock by a few seconds and the event start comes from the camera clock, so too low
  a margin can cut the end of the window short; lowering the span makes the first look
  due sooner but moves subjects first visible later into the retry, which is read after
  the rest of the window. Change them one camera at a time and compare first-alert times.
- `sd_early_span` (camera card, default 18, 6-30, only with `sd_early_look`) is the length
  of the early look in seconds. The look is due `span + guard + 9` s after the event
  starts, so 12 is due 6 s sooner than 18 and downloads a third shorter, but covers only
  the frames at 0 and 6 s; a window needs at least one more 6 s frame beyond the look to
  be split. Keep 18 unless a trial shows the photos still come from the shorter look.
- `sd_fresh_guard` (camera card, default 60, 10–120) is the number of seconds a card
  window's end must be in the past before it is downloaded (pytapo's own guard is 60).
  Every card due time moves with it. The camera lists a segment while it is still
  recording, but about 10–20 s behind, and the read is clipped to what is listed. So a
  guard that is too low gives a shorter clip with fewer frames, not an error. Keep the
  default unless a trial on your camera shows complete clips.
- `sd_span_cap` bounds the event window downloaded/scanned.
- `sd_motion_span_cap` bounds the first window for unconfirmed motion/PIR; one empty
  result retries with `sd_span_cap`. Confirmed person events use the full window directly.
- `sd_motion` also gives PIR-backed bare motion a second chance; it can be expensive.
- `sd_jobs_per_tick` adds per-camera backpressure for slow hosts.
- `sd_frame_pick` chooses among the follow-up's frames that pass the scorer threshold:
  - `sharpest` (default) sends the one whose person crop is least blurred;
  - `largest` sends the one with the biggest person box. Two guards keep that from sending a
    worse photo: a frame more than 3x blurrier than the sharpest one is skipped, and the
    largest must have at least 1.25x the sharpest frame's box area, otherwise the sharpest
    one is sent. When the scorer returns no box for one of the frames, `sharpest` is used.
  - `tallest` (opt-in, under trial) ranks by box height instead of area, so a person
    side-on or mid-stride does not win on width alone. It uses the same blur guard, and the
    tallest must be at least 1.1x as tall as the sharpest frame's box. Frames whose box
    touches the frame edge (feet or head cut off) are set aside unless every frame has one.
    Without a box for every frame, `sharpest` is used.
  The blur score rates a small, distant subject as sharp, so with `sharpest` a person walking
  away from the camera tends to be sent when they are already far off.
- `sd_dense_start` adds a frame every 2 s over the event's first 12 s. The event fires when
  motion starts, and a subject who walks away is largest in those first seconds, which a 6 s
  grid can skip. Frames on the normal grid are still taken. This adds 3 to 6 frames per
  follow-up, and each one costs a seek and a scorer call. Left unset, it is on for
  `largest` and `tallest`, off for `sharpest`. With `sharpest`, the extra early frames can make the
  follow-up send a later, smaller subject. Only a window that starts at the event gets the
  dense frames; the rest of a window after the recorder's early look keeps the normal
  spacing.

- `sd_score_parallel` limits concurrent scoring uploads within each SD/recording
  follow-up to 1–4 (default **1**). Sequential uploads keep dense opening frames from
  competing for bandwidth and timing out on a slow uplink. On a fast link, increase
  this to 2 or 4 to reduce batch time. It also limits scoring during local recording
  extraction; live and sampler requests keep their own timeouts and can run alongside
  the follow-up. More frames still mean more upload time, so leave `sd_dense_start`
  off if a sequential batch delays follow-ups too much.
- `sd_prefer_face` (default off) asks the scorer for faces (`?faces=1`). It needs a scorer
  with `TAPO_SCORER_FACE_MODEL`; see [operations](operations.md). When some of the
  above-threshold frames show a face on the chosen person (the face's centre is inside the
  person box), `sd_frame_pick` picks only among those frames. Otherwise the pick is the
  same as without the option. It helps most on close cameras, where the tallest box is
  often someone's back or a body with the head cut off. Faces of people far away are
  rarely found.
- Before any pick, frames blurred by the camera's own movement (auto-track, a preset
  recall) are set aside, as long as at least one steady frame is left.

```yaml
sd_frame_pick: largest     # sd_dense_start follows: true
```

With `snapshot_source: recording`, bare motion whose live frame scores below the threshold
also gets a recorder look — that is the point of a local recorder, since a live frame can
score 0.05 on a subject the recording shows at 0.83. It is throttled by design: only one
motion follow-up is pending per camera at a time, so the rate is bounded by how long a
window takes to become downloadable rather than by the event rate. The live sampler keeps
working the same burst in parallel, and whichever gets there first closes the alert gate for
that passage, so only one alert can reach the phone. On a slow host, `sd_jobs_per_tick` and
`sd_motion_span_cap` are the brakes.

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
  hold_expiry: observe
  hold_expiry_min_score: 0.35
  keep_sampling_on_defer: false
  loiter_frames: 0
  loiter_miss_tolerance: 0
  loiter_mode: enforce      # observe: audit would_loiter, send nothing
```

The sampler takes additional live frames across a long event. Nearby events are grouped;
`max_frames` bounds work. Use it with a local scorer to avoid sending empty follow-ups.

With a local scorer, `low_score_exit: N` closes a *motion-only* group early once `N`
consecutive follow-up frames all score below `low_score` — bursts from foliage, light or
insects stop consuming grabs. Groups with a camera-confirmed person/PIR detection always
run the full window (the sampler exists to catch a subject appearing mid-event), and a
confirmed detection arriving later reopens an early-exited group. `0` disables the exit.

`hold_expiry` decides what happens to a frame held for corroboration (see
`motion_send_threshold` below) whose second frame never came before the group closed.
Labelled review-log frames show most held frames are real people, and about half of them
never got an alert:

- `off` (the default) drops it as `hold_expired`, exactly as before;
- `observe` sends nothing but audits the send it would have made
  (`action=would_send reason=hold_expiry_observe`) next to the unchanged `hold_expired`
  drop — the mode to trial a camera in;
- `send` delivers the held frame through the normal alert path, audited
  `action=send reason=hold_expiry_send`, and arms the motion cooldown like a sampler send.

Either policy acts only when the held score reaches `hold_expiry_min_score` (unset: the
camera's `scorer.threshold` in force at expiry, `night_threshold` during its night — so
every held frame qualifies), the frame is still in the review log, and the motion cooldown
and the scene group allow it; a held frame that stays behind keeps its `hold_expired` line
with `expiry=below_floor`, `no_archive`, `archive_missing`, `cooldown` or
`scene_duplicate`. A pan-limit recall that broke the corroboration is rescued first
(`hold_rescue_recall`) whatever the policy. The frame sent is the best-scoring one the
group held. The policy needs `scorer.motion_send_threshold` (without it nothing is held —
the config is rejected) and `TAPO_REVIEW_LOG_DIR`, where held frames are archived: without
it the daemon warns once at startup and every expiry stays `hold_expired` with
`expiry=no_archive`. Estimate what `send` would add on a recorded night with
`tapo-monitor replay --compare` before switching a camera on (see
[observability](observability.md#replaying-a-night)). Write `off` bare or quoted; YAML
reads a bare `off` as false, which is accepted as `off`.

`loiter_frames: K` (off at `0`) sends one separate "lingering ~N min" photo when a person
is on `K` consecutive follow-up frames of one group (`interval: 30`, `K: 4` is about two
minutes). It fires at most once per group, however long the person stays, and a sent group
keeps sampling until it has. It needs a scorer and `K <= max_frames`. Only follow-up
frames count, so a person gone from the picture breaks the streak: a PTZ move or
pan-limit recall that turns the lens away interrupts the count, which
`loiter_miss_tolerance: N` bridges for up to `N` frames (default 0, strictly consecutive).
With `scorer.alert_zones` set, only a person whose feet stand in a zone counts toward the
streak: the scorer answer is already filtered by the zones before the sampler reads it, so
a person outside them neither starts nor extends a streak. Without zones nothing changes.

`loiter_mode` (`enforce` by default, or `observe`) is a soak switch for the notice.
`observe` runs the streak exactly as before, including setting the group's one-shot
`loiter_sent` flag, but sends nothing: it writes one audit line per group,
`path=sampler action=would_loiter etype=person minutes=N`, instead. It needs
`loiter_frames > 0`.

`keep_sampling_on_defer` (off by default) changes what a camera-confirmed person whose
live frame scores below the threshold does to the rest of its burst. The frame is still
handed to the SD/recording follow-up (`sd_snapshot`) and the confirmed cooldown is armed,
as before. Off, the defer also closes the burst for the sampler, and the cooldown skips the
next person frames of the same passage although nothing was delivered, so the person
arrives only with the follow-up, minutes later, or not at all when it finds nobody. On,
the sampler keeps sampling the burst with its usual interval, frame budget and threshold
(a person group is held to `scorer.threshold`, never to the motion corroboration), and
while the follow-up is queued a frame of the passage whose person score reaches the
threshold may pass the cooldown: a sampler frame, or a live frame of a later event
(scored first, then sent). Every other frame stays under the cooldown exactly as with the
switch off: a tamper/pet/motion frame of nobody, an empty person frame (audited
`action=cooldown reason=open_burst_no_person`), and any frame the scorer could not judge.
One visit still makes one message: once a photo of the passage that showed a person is
delivered (sampler, live, held frame or another follow-up), the queued follow-up is
dropped, before its read if it has not started, audited `path=sd action=cooldown
reason=passage_already_alerted`. When none is, it sends exactly as before. "The passage"
is the gate's own notion: camera event starts less than `alerts.cooldown` apart (for the
sampler, apart from the span of its group), so one burst queues a single follow-up. The
defer's audit line carries `keep_sampling=true`. It needs a local scorer (without one a
camera defers as before), has no effect while the sampler is off, and leaves the
`reason=snapshot_failed` defer unchanged. Queued follow-ups and their marks survive a
restart; the sampler's group does not, so a burst interrupted by a restart falls back to
the follow-up.
One known gap: when Telegram refuses a photo let through the cooldown this way, nothing is
delivered, so the queued follow-up still goes out, and with the `outbox` on the refused
photo may be delivered late as well, so after an outage one visit can make two messages.

### Local scorer and crop

For changes from older releases, see [Upgrading](upgrading.md). In particular, omitted
`scorer.on_error` now means `hold` and omitted camera-level `crop_min_px` means `400`;
set `on_error: open` or `crop_min_px: 0` explicitly to retain the respective older policies.

```yaml
scorer:
  url: http://127.0.0.1:8766/score
  threshold: 0.40
  timeout: 10
  tiles: 2
  motion_send_threshold: 0.60

crop_to_subject: true
```

#### Scorer token (optional)

A scoring service that requires a shared token (see
[Shared token](operations.md#shared-token-optional)) is reached with

```yaml
scorer:
  url: http://198.51.100.20:8766/score
  token_env: TAPO_SCORER_TOKEN
```

`token_env` is the NAME of an environment variable (letters, digits and underscores, so a
pasted token is refused at load time), not the secret. If `<token_env>_FILE` is also set and
names a readable file, the token is read from it instead. Without `token_env` no header is
sent. The token is never logged or stored in audit lines, state or reports. A 401 answer
is logged once as a configuration error and does not count as a transport failure for
failover. All candidates of a `url` list share the one token.

#### Scorer failover (optional)

`url` may be a list of candidate scoring services, for a scorer that is moved to another
machine or has a standby; a plain string behaves exactly as before.

```yaml
scorer:
  url: ["http://203.0.113.10:8766/score", "http://198.51.100.20:8766/score"]
```

The first entry is the primary. Scorers are interchangeable, so the monitor keeps one
active URL per candidate list (cameras that list the same candidates share it) and every
component reads it from one place (`scorer.active_url` / `scorer.score`): live scoring,
the sampler and SD paths, the crop, the scene probe and the shadow scan.

- Trigger: 3 consecutive request failures at the transport level (timeout, refused or
  dropped connection, or a 502/503/504 from a proxy). An HTTP 4xx on an image, an
  unreadable frame or a bad JSON body is not a failure of the service and never counts. A
  success resets the streak.
- Switch: the other candidates are tried in order with a quick `GET /health` (2 s) on the
  same service; the first that answers becomes active. If none answers, the active URL is
  kept and the candidates are probed again after 3 more failures.
- Return: while a non-primary URL is active, the primary's `/health` is checked at most
  every 5 minutes (by the next scoring request, no background thread) and the monitor
  switches back as soon as it answers.
- Visibility: one `scorer switch` warning per change (with both URLs, journal only) and one
  `audit path=scorer action=scorer_switch scorer_index=N` line (index, never an address).
  The choice is saved in the health state file (`active_scorers`, keyed by the primary URL)
  and restored on restart if it is still in the list. The daily digest reads `/metrics`
  from the active URL.
- Not covered: `tapo-monitor label --scorer-url` takes one URL; `/health` only proves the
  service is up, not that its model is the one you expect; all candidates should run the
  same scorer version (older services without the `vehicle*` fields are tolerated).

With `tiles > 1` the service also scores a tiles×tiles grid, but the grid only refines
*localisation*: the send-decision `person` score always comes from the full frame; the
returned `animal` score is audit telemetry only. The best tile contributes the person
`box` (for `crop_to_subject`) plus a
diagnostic `tile_person` score. Blown-up tile crops of night IR grain routinely
hallucinate 0.3–0.6 "person" scores, so tile scores never gate alerts.

The response also lists `person_scores`: one confidence per separate person in the full
frame (anchors of one person merged by non-maximum suppression, floor 0.10, at most 10).
The daemon counts the entries at the camera's threshold in force (`night_threshold` at
night) and, with two or more, adds `· 2 people` to the caption — the box, the crop and the
score still describe only the most confident one. It never changes whether an alert goes
out. A scorer from before this field sends no list, and the caption then shows no count;
update the scorer host to get it.

`motion_send_threshold` adds multi-frame corroboration for **motion** events.
An empty IR scene hallucinates a 0.3–0.6 "person" score on one frame and not the next,
while a real subject persists across frames. So, for a motion frame:

- a frame `>= motion_send_threshold` sends immediately (a clear single frame);
- a frame in `[threshold, motion_send_threshold)` is *held* until a second frame in that
  band corroborates it within the sampler window (needs `sampler.enabled`), otherwise it
  is dropped when the group closes (unless `sampler.hold_expiry` sends it);
- camera-confirmed people (bit 19) are unaffected — they keep the immediate path, so a
  confirmed person is never delayed or dropped;
- a sampler frame of a burst that carried `events_1` bit 5 (alarm code 6, the camera's
  person class — see [the `events_1` bitmask](events1-bitmask.md)) skips the hold; a live
  frame of such an event skips it only with `person_bit_skips_hold: true`.

Unset (the default) keeps the legacy behaviour: any motion frame `>= threshold` sends.

`person_bit_skips_hold` (default `false`) lets a live motion frame whose event carries
bit 5 send at `>= threshold` instead of waiting for a second frame, as the sampler already
does for such a burst. A replay of three cameras' journals found the hold only delaying or
losing these frames (held bit-5 frames were a person 9 in 10 times by label), and none a
false alarm the hold had stopped. Needs `motion_send_threshold`; without it nothing is held.

`ignore_zones` (default none) lists rects `[x1, y1, x2, y2]` as fractions of the frame
whose people do not count. A static person-shaped object — sacks, a coat on a hook, a
garden statue — scores as a person on every frame, so any event the camera raises
nearby (a bird, a shadow) "confirms" it. A person whose box lies at least 80 % inside a
zone is removed from the answer; the score becomes the best remaining person, so a real
person elsewhere, or one standing in front of the object (a taller box), still counts.
Keep a zone tight around the object. It needs a scoring service that returns
`person_boxes` (a current service does); an older one is left alone, with one warning.

```yaml
scorer:
  ignore_zones:
    - [0.0, 0.48, 0.12, 0.66]   # sacks under the woodshed, left edge
```

`lens_ignore_zones` (default none) is `ignore_zones` per lens, for a camera whose
`event_profile` has a pan/tilt lens (for example `c545d`); any other profile, or a lens
name the profile does not know, is a config error. The keys are `wide` and `pan_tilt`,
each a list of rects as above. A frame whose lens is known is scored with `ignore_zones`
plus the rects of its lens; a frame without a known lens (the SD card, a recording) with
`ignore_zones` only. The lens follows from the stream the frame was grabbed from: `stream1`,
`stream2` and `stream8` are `wide`, `stream6` and `stream7` are `pan_tilt`. Empty = nothing
changes.

```yaml
scorer:
  ignore_zones:
    - [0.0, 0.48, 0.12, 0.66]   # applies to both lenses
  lens_ignore_zones:
    wide:
      - [0.70, 0.55, 0.85, 0.80]
```

Warning: a zone is tied to where the lens points. The fixed lens keeps its framing, but
the `pan_tilt` lens is turned by the firmware after a subject, so after it moves a
`pan_tilt` zone sits over a different part of the scene and can hide a real person.
Prefer `wide` zones, keep every zone tight, and do not put a `pan_tilt` zone on anything
you cannot afford to miss.

`max_person_box` (default off) is a fraction `(0, 1]` of the frame: a person whose box is
larger than it in BOTH width and height is dropped, the same way an ignore zone drops one
(the answer gains `oversized_persons`, `person` becomes the best remaining one). It catches
the model boxing the whole frame as a person (width and height >= ~95 %, score ~0.5) or a
huge static object. A box that is wide but short, or tall but narrow, stays. The order is
`ignore_zones`, `max_person_box`, `alert_zones`. It needs a service that returns
`person_boxes`; an older one is left alone, with one warning. It only removes absurd boxes:
use `0.9` or more. On a dual-lens camera mind the pan/tilt lens, where a person close to the
camera can legitimately fill most of the frame, so a lower value would cost real alerts.

```yaml
scorer:
  max_person_box: 0.9
```

`ignore_boxes` (default none) is for a static object the model boxes as one big person,
for example the roof of a swing seen as a box of 0.44 x 0.43 of the frame, while people
stand in front of it. It lists rects `[x1, y1, x2, y2]` (fractions of the frame, as in
`ignore_zones`); a person is dropped only when its box matches a rect in size AND place
(IoU of at least 0.75, counted in `ignored_boxes`). The existing options do not fit this:
an `ignore_zones` rect over the roof also hides a person standing in front of it (that
box lies inside the zone), and `max_person_box: 0.4` would drop any person that large
anywhere in the frame. A person in front of the object, or a large one elsewhere, still
counts; only a person whose box is practically the rect itself is lost. Take the rect
from a few frames of the false alarm (the boxes the scorer logged, normalized), keep it
tight and cover one object per rect. It runs after `ignore_zones`, before
`max_person_box`. Static clusters in general must not be suppressed automatically: they
are mostly people's routes. `ignore_boxes_mode: observe` soaks a rect first: nothing is
dropped, and a sent frame whose person a rect would have dropped adds the audit line
`action=would_drop_ignore_box boxes=N`. It needs `ignore_boxes`.

```yaml
scorer:
  ignore_boxes_mode: observe        # read the would_drop_ignore_box lines, then delete the key
  ignore_boxes:
    - [0.00, 0.26, 0.44, 0.69]      # swing roof, ~0.44 x 0.43 of the frame
```

`alert_zones` (default none) is the opposite of `ignore_zones`: a list of polygons, each
at least three `[x, y]` points as fractions of the frame, and a person counts only when
the bottom centre of its box (the feet) lies inside at least one of them. Use it to watch
a driveway or a yard and not the street behind it. The ignore zones run first, then the
alert zones; the score becomes the best remaining person, and a frame with nobody in a
zone scores 0. The edge of a polygon counts as inside. It needs a scoring service that
returns `person_boxes`; an older one is left alone, with one warning.

Zones are drawn on one view, so they are refused (config error) on a camera whose
`event_profile` has a pan/tilt lens (for example `c545d`, which also picks between two
lenses), and they do not follow a PTZ camera that moves between presets: use them on
fixed-view cameras, or on a PTZ parked on a single preset.

A camera that can move (`role` other than `static`, so auto-track at night, or
`pan_limit.enabled`) loads with a warning when `alert_zones` is set instead of an error:
the zones keep working while the lens is parked, but they no longer match the picture
once it moves. See [watch zones](watch-zones.md) for when to use them.

```yaml
scorer:
  alert_zones:
    - [[0.40, 1.00], [1.00, 1.00], [1.00, 0.55], [0.62, 0.50]]   # driveway, lower right
```

`alert_zones_mode` (`enforce` by default, or `observe`) lets you soak the zones before they
filter anything. In `observe` the scorer answer is left unfiltered, so alerts go out as if
no zone were set. When a zone would have dropped the person of a frame that was sent, the
audit gets one extra line next to the send,
`action=would_drop_outside_alert_zone persons_inside=N persons_outside=M`, so `label-stats`,
`replay` and a soak report can count them. The line is written for `action=send` audits
only, and vehicles are not looked at in `observe`. It needs `alert_zones`.

`alert_vehicles` (default `false`) and `vehicle_threshold` (default `0.5`, `0 < x <= 1`)
make the camera also alert on a vehicle (car, motorcycle, bus or truck) that the scoring
service finds at or above `vehicle_threshold` in a frame it scored. It needs a service that
reports vehicles; an older one sends none and nothing happens. The alert is a side alert:
a frame that reaches the person threshold is only ever a person alert, so a vehicle never
replaces or duplicates one; the caption uses a car emoji and the audit line carries
`etype=vehicle` (`action=send`, or `action=cooldown` while it is gated, with the vehicle
score in `score`). It has a cooldown of its own (`alerts.cooldown`) that a person alert
neither resets nor is reset by, and it is also held back while a person alert of the same
passage is cooling down. It does not close a sampler group, mark an event delivered or start
SD follow-ups, so a camera-confirmed person is still handled as before. It covers the live
poll, the sampler frames and SD-card/recording follow-ups (there only when no frame of the
window shows a person, once per event and deduplicated against a vehicle alert the live pass
or the sampler already sent for it); the shadow scan does not raise vehicle alerts. With `alert_zones` set, a vehicle counts only when the bottom centre of its box lies
in a zone (the same rule as the feet of a person); a service without `vehicle_boxes`
cannot place it, so nothing is sent.

```yaml
scorer:
  alert_vehicles: true
  vehicle_threshold: 0.6
```

`vehicle_mode` (`enforce` by default, or `observe`) is the soak switch for vehicle alerts.
In `observe` a vehicle that would alert is not sent and arms no cooldown; the audit line is
`action=would_send etype=vehicle` (with `path=live`, `sampler` or `sd`) instead of
`action=send`. An SD window audits it once. It needs `alert_vehicles: true`.

`night_threshold` replaces `threshold` while the camera's night is on — the shared astral
night with the camera's `schedule` applied, so an `always_night` camera uses it around the
clock and an `always_day` one never does. IR night scenes score differently from daylight
ones, so the best-separating value from `tapo-monitor label-stats` can differ by day and
night. Everything that reads `threshold` uses it at night: the live path, the sampler, the
SD follow-up, hub clips and the corroboration band, which becomes
`[night_threshold, motion_send_threshold)` (so `motion_send_threshold` must stay above
it). The night is decided when the daemon processes a frame, not when the event started: a
hub clip or SD follow-up handled a few minutes after dusk is judged by the night value.
Unset (the default) uses `threshold` around the clock. Check a change against recorded
events with `tapo-monitor replay --compare` first (see [observability](observability.md)).

- `threshold` is the minimum **person** confidence from 0 to 1; animal confidence never
  triggers an alert.
- `tiles: 1` scores only the whole frame; larger values also score a grid.
- `crop_to_subject` uses the best returned person box and safely falls back to the full
  frame.
- `crop_min_frac` sets the smallest crop the zoom may produce (see below).
- `crop_min_px` (default 400, 0 = off): with `crop_from_native`, a crop narrower than this
  many source pixels is not sent as the alert; the whole frame is, and the crop follows as a
  second photo. A crop cut from the delivery-width frame is not measured and is sent alone,
  as before.
- A frame that reached the alert without a score (scorer unavailable, passed through) is
  never cropped: the whole frame is sent.
- When no score is available, live frames fail open as raw passthrough.
  The client retries timeouts and HTTP 400/500 once, with a 0.5 s backoff and a budget of
  about twice `scorer.timeout`; it retries only with at least 3 s left. Connection refusals,
  HTTP 401 and gateway errors are not retried this way. Budget for this latency when
  adjusting the sampling interval; a healthy request has no retry delay.
  The unscored photo is captioned `(unverified)` and audited with `reason=scorer_unavailable
  degraded=true`.
- `on_error` (`hold`, default, or `open`): the fail-open rule is kept for live frames and
  for the first frames of a sampler group. A sampler follow-up that cannot be scored after
  earlier frames of the same group already scored below threshold is, with `hold`, skipped
  (audit `action=scorer_unavailable reason=skipped_prior_low_scores`) and retried at the
  next interval; the last frame of the window still passes through. `open` always passes it.
- The [artifact filter](operations.md#partially-decoded-frames-artifact-filter) is enabled
  by default on the scorer service. A recognised partially decoded grey frame returns
  zero scores with `artifact: true`; this is a successful filtered result, not a scorer
  outage handled by `on_error`. `TAPO_SCORER_ARTIFACT_FILTER=0` on the service turns it off.

The scorer *service* binds `0.0.0.0`; unlike the status endpoint, there is no localhost
default. Authentication is optional: without a [shared token](operations.md#shared-token-optional),
anyone who can reach the port can submit frames and read `/metrics`. With a token,
`/score` and `/metrics` require it, but `/health` remains open. Use a trusted network or
firewall the port, and TLS when required; pointing `scorer.url` at `127.0.0.1` changes
nothing about what the service listens on.

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
to Telegram (1280 px) and the whole-scene copy kept in the sent log (at most 2560 px,
lighter JPEG quality, about 0.5 MB for a noisy 4K frame) are downscaled after the crop.

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

`enrich.light_status` (default `false`) queries the camera's white lamp (the full-color
night-vision light some models can switch on for a confirmed person) at alert time and
notes it — a 🔦 — in the Telegram caption. It costs one extra API call per alert and only
means anything on hardware that has the feature; a camera without it, or a query that
fails, leaves the caption unchanged (the state is "unknown", not "off").

### Soft pan limit

```yaml
pan_limit:
  enabled: true
  margin: 0.01
  poll_interval: 6
  onvif_port: 2020
  onvif_user_env: CAM_ONVIF_USER
  onvif_password_env: CAM_ONVIF_PASSWORD
  tilt: true          # also bound tilt (off by default)
  tilt_min: -1.0      # only presets inside this window may become a tilt bound
  tilt_max: -0.6
  hold_grace: 20      # while track_hold holds a subject, wait this long out of bounds
```

The guard reads current/preset pan positions over ONVIF and recalls the nearest bounding
preset when auto-track moves outside their span. It does not create a hard motor limit.
ONVIF errors are isolated from the event loop. While privacy mode is known to be on (read
by the last control pass, else by the twin), the guard does not poll at all: a parked lens
can only answer `MOTOR_BUSY`. A `GotoPreset` the motor refuses between control passes is
counted as a privacy refusal and keeps the ONVIF client; only transport errors rebuild it.

The guard needs the `onvif-zeep` package: `pip install -e ".[onvif]"` (or
`pip install onvif-zeep`) into the daemon's venv. The import is deliberately lazy — it
happens on the guard's first poll, not at startup — so `selfcheck` passes without the
package while the guard sits inert, logging a warning every poll. Install it on every
host where `pan_limit` is enabled.

#### Predictive stop instead of recall

An opt-in `mode: stop` observes pan on a separate thread and sends ONVIF Stop before a
selected preset boundary. It then disables native tracking using the monitor's existing
camera session. Slow scoring or clip reads do not set the observation cadence.

```yaml
pan_limit:
  enabled: true
  mode: stop
  right_preset: "4"       # measured usable rightmost view
  left_preset: null       # no software limit on the left
  stop_margin: 0.15       # stop this far inside the boundary, in ONVIF pan units
  stop_poll_interval: 0.15
  stop_cooldown: 30
  onvif_user_env: CAM_ONVIF_USER
  onvif_password_env: CAM_ONVIF_PASSWORD
```

The selected presets must exist. Every configured day/night return preset must lie
strictly inside the trigger range, including the stopping margin. Unrelated presets do
not widen this range. `right_preset` is required; `left_preset` optionally adds a second
side. This mode currently limits pan only.

A trip latches tracking off, including later configuration writes. The normal control
pass may return to its configured safe home preset. Tracking can resume only after the
cooldown, fresh stable interior observations and a verified tracking disable. An unread
position inhibits tracking; known privacy suspends pose reads and motor commands. There
is no second native camera login for the observer.

Calibrate the margin from actual motion, stopping distance and network delay on each
camera. `stop_poll_interval` is a requested cadence, not a guaranteed response time.
This remains a software guard: lost connectivity or camera firmware behavior can defeat
it. It does not install a firmware motor limit, restrict manual app access, or guarantee
startup calibration stays within the range. Verify the view with infrared enabled before
selecting the boundary. Begin with a single camera pilot.

In the default `mode: recall`, each intervention archives the out-of-bounds frame — the view the recall is about to
erase — into a `panlimit-log` directory beside the review log or sent log, kept for
2 days (see [Inspecting alert frames](operations.md#inspecting-alert-frames)). And
because a recall physically removes the subject a held marginal motion frame was waiting
to corroborate (`motion_send_threshold`), a hold broken by a recall is sent when it
expires instead of being dropped — audited as `hold_rescue_recall`; it needs the review
log configured, which is where the held frame lives.

Correcting the aim does not unrecord what the camera already put on its SD card. The
follow-up arrives about two minutes later and re-scores the *recording* of the event, so
frames taken while the lens was off its span still show whatever it was pointed at — a
wall, netting lit by the camera's own IR — and they score like a subject: on 2026-09-02 at
04:36:59 such a frame reached a phone at p0.63, eleven seconds after the recall had
already fixed the camera. Frames whose capture time falls inside a recall's window are
therefore skipped before the scorer sees them, audited as `panlimit_window`. The window
opens a full `poll_interval` before the recall — the previous poll is the last moment the
aim is known to have been good — and closes five seconds after it, while the lens travels
back. A frame whose name carries no capture time is scored as usual: unknown must never
cost a real detection.

Enable it per camera, not by default. The firmware already returns a camera to where its
track started, after `back_time` seconds (30 on the C560WS), which covers the ordinary
case on its own — and faster than a 60 s control pass. What it does not do is return the
camera to an *absolute* position, so a home that has itself drifted stays drifted, and it
never corrects tilt. That is the gap `pan_limit` and `night_preset` fill, and it is worth
filling only where auto-track can actually reach somewhere useless.

Check the span before enabling it. The bounds are the minimum and maximum pan over *all*
the camera's presets, so on a camera with two presets flanking its useful view, the guard
clamps to the gap between them and yanks the camera back mid-track. Measure the outermost
still-usable positions by eye first, and add a preset for a bound that no existing preset
marks — the bound is only as good as the presets it is derived from.

`tilt` extends the same guard to the vertical axis, and is off by default because that
warning bites harder there. Auto-track moves tilt as well as pan, and nothing else brings
it back: a recalled preset is the only thing that corrects it. But presets are also where
people park a camera to look at something unusual, and a single one aimed high stretches
the tilt bound over nearly the whole travel — on one camera here, six presets spanned 1.79
of the 2.0 its motor can reach, purely because one pointed at the sky. `tilt_min` and
`tilt_max` fix that by excluding presets outside the window from becoming a bound. They
never invent a position: every bound stays a preset the camera can actually be recalled
to, so a window that leaves fewer than two candidates disables the tilt guard rather than
clamping to something made up.

Pan is checked first and tilt only if pan is already inside its bounds, because recalling
a preset restores both axes at once.

### Observation-only coordinator

```yaml
coordinator:
  group: overlap-group
  scene_window: 15
  camera_order: []
  handoff_preset:
```

`group` enables the observation-only duplicate gate for cameras sharing the same group.
`scene_window` is the inclusive event-time window in seconds. A detection is committed to
the group only after its Telegram delivery succeeds; failed or deferred deliveries do not
block another camera. The live, sampler and SD paths share the same gate. Cameras without
a group keep the existing per-camera behavior.

`camera_order` is the operator-measured sequence (first view to next view) used only to
label a scene's direction; empty leaves direction unknown. It requires a `group` and may
only name cameras of that same group — any other name could never meet this camera in a
scene, so the load fails with the camera, the group and the stray names.

`handoff_preset` remains reserved for the bounded lease state machine. The current
runtime still does not move a camera or select a better frame; live activation waits for
a measured overlapping-camera pair. Track progress in the [roadmap](roadmap.md).

## Observability

```yaml
observability:
  digital_twin: false
  probe_interval: 900
  drift_alerts: false
  ledger: false
  ledger_retention_days: 30
  shadow_match_window: 20
  status_port: 0
  status_bind: 127.0.0.1
```

- All observability features are opt-in.
- `probe_interval` has a 60-second minimum; probes reuse the daemon client.
- Enable `digital_twin` before `drift_alerts` and inspect several cycles.
- `ledger` stores normalized metadata and decisions, never media.
- `shadow_match_window` is the intended correlation window for integrations; the current
  CLI report accepts an explicit `--window` and defaults to the same 20 seconds.
- `status_port` (1–65535) serves `GET /status` — a JSON summary of daemon and fleet
  state — when non-zero; 0 or absent keeps it off. The server binds `status_bind`,
  which defaults to `127.0.0.1`: the endpoint is unauthenticated, so exposing it
  beyond the host is the operator's explicit choice, never a default.

State path overrides:

```bash
export TAPO_HEALTH_STATE_FILE=/private/path/health.json
export TAPO_TWIN_STATE_FILE=/private/path/twin.json
export TAPO_LEDGER_FILE=/private/path/events.sqlite3
```

See [Observability](observability.md) for schema, privacy and rollout details.

## MQTT (Home Assistant)

```yaml
mqtt:
  host: broker.example.org
  port: 1883                 # default 1883, or 8883 with tls
  user_env: MQTT_USER
  password_env: MQTT_PASSWORD
  tls: false
  discovery_prefix: homeassistant
  base_topic: tapo_monitor
  publish_images: false
  motion_off_after: 60
```

- The block's presence is the switch; `host` is then required. Absent, nothing is
  imported and the daemon behaves as before. Needs `pip install 'tapo-monitor[mqtt]'`.
- `user_env`/`password_env` name environment variables, like every credential here.
- `base_topic` and `discovery_prefix` may contain `/` but no wildcards, spaces or empty
  levels. Give each daemon its own `base_topic` when several share one broker.
- `publish_images` sends the last alert photo to the broker; off by default because the
  photos show people.
- `motion_off_after` (≥ 1 s) is how long Person and Motion stay on after the last event.
- Camera names become topic levels with anything outside `A-Z a-z 0-9 _ -` replaced by
  `_`; two cameras that collide that way are refused.

See [MQTT and Home Assistant](mqtt.md) for entities, topics and delivery guarantees.

## Outbox (late delivery after an outage)

```yaml
outbox:
  enabled: true
  # dir: /private/path/outbox   # default: outbox/ beside health.json
  max_age: 86400                # seconds; older entries are dropped unsent
  max_entries: 300              # oldest dropped beyond this
  max_photos: 10                # late photos per camera per drain
  summary: true                 # one outage text before the late photos
  lang: en                      # en or cs: language of the outage text and late captions
```

- Off unless `enabled: true`. Without it a failed Telegram send is retried in memory
  (SD follow-up, hub retry) for about ten minutes and then lost, as before.
- Every alert path ends in the same send; when a real send fails, the frame and its
  metadata (camera, incident, event type, caption, score) are written to the outbox
  directory as a JPEG plus a JSON sidecar. The directory survives a restart. The
  default is `outbox/` next to the health state file, i.e.
  `$XDG_STATE_HOME/tapo-monitor/outbox` (`~/.local/state/tapo-monitor/outbox`), or next
  to `TAPO_HEALTH_STATE_FILE` when that is set.
- An incident delivered later by any path (SD follow-up, hub retry, sampler) is removed
  from the outbox, so nothing reaches the phone twice; so is one an SD follow-up or the
  sampler deliberately decided not to send (same passage already alerted, scene
  duplicate, within the cooldown, no subject on the card, known face). Entries whose SD
  follow-up or hub retry is still pending, and entries younger than two minutes, are
  left to those retries.
- Once Telegram answers again (a delivered alert, or a silent `getMe` probe at most once
  a minute while it is down), each camera's batch is delivered: one summary text naming
  the outage window and the counts, then the photos that showed a person, captioned
  `⏳ delayed by X h Y min · <original caption>`, at most `max_photos` of them. The rest
  go to the review log (`TAPO_REVIEW_LOG_DIR`, verdict `outbox`) instead of the phone;
  without that variable they are only counted (the summary says "not sent") and the
  daemon warns at startup. A later summary counts only entries no earlier one did.
- The drain runs on the main loop, so each tick does a bounded amount: at most 5
  rescorings, 3 photos and about 10 s; a backlog trickles out over the following ticks.
- "Person" means a person score at the camera's threshold for the event's time (its
  `night_threshold` at night). A frame the scorer could not score at the time is scored
  at drain time. While the scorer is still unreachable, an unscored `person` event goes
  out as a person, and any other unscored event waits for the scorer for up to six hours
  after its failure; then its event type decides (the review log).
- An entry is removed only after its send succeeded. A failed photo stops that camera
  until the next attempt (other cameras continue); after three failed attempts the entry
  goes to the review log. A failed summary means the link is still down and stops the
  whole drain. When the outbox is full, the oldest non-person entries are dropped first. Late photos are recorded in the sent log with `path: outbox` and never
  arm a cooldown, so they cannot silence a fresh live alert.
- Collect-only cameras (`telegram_alerts: false`, silenced app notifications) are never
  captured, and an event in a mute window never reaches the send path, so nothing muted
  live is delivered late either.

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
  the Digital Twin state, and is what makes the repair policy below take effect.
- `auto_fix` is limited by `allowed_repairs`; unknown or unsupported capabilities never
  trigger a write. Both keys apply only while `enabled` is true: with reliability off the
  daemon keeps its built-in guard repairs (person detection on, vehicle detection off,
  SmartTrack people filter) exactly as it did before this feature existed, so trimming the
  allow-list of a disabled block cannot silently switch them off.
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

Unknown keys are rejected: the load fails naming the full key path plus the closest known
key (`cameras[0].scorer.treshold: unknown key (did you mean 'threshold'?)`), derived from
the config dataclasses so the check cannot rot. A mistyped key would otherwise silently
take its default — a dropped `rotate` costs roughly a third of the person score.

Renamed keys are the exception. When a release renames a key, the old name keeps loading
for that release: it is mapped to the new name and logs
`<path>: renamed to '<new>'; accepted for now, update the config`. Setting both the old
and the new key is an error. After that release the old name is an ordinary unknown key.
The table lives in `RENAMED_KEYS` in `tapo_monitor/config.py` and is currently empty.

Change one capability at a time. Observe logs and status before enabling the next, because
model/firmware support and camera resource limits differ. The full annotated schema remains
in [`cameras.example.yaml`](../cameras.example.yaml).
