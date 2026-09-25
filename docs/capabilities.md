# Tapo detection — capability catalog

What a TP-Link Tapo PTZ camera (validated on the C560WS) can do for monitoring, and
what this project adds on top. Each capability is opt-in per camera via `cameras.yaml`.

This project controls camera monitoring policy and PTZ tracking, but its response remains
**observe and notify**. The active-response actuator layer (siren, floodlight, speaker) is
documented below as *available* but intentionally **not implemented** — see "Actuators".

## 1. Detection inputs

| Source | How | Notes |
|---|---|---|
| ONVIF pull-point events | Researched, not daemon-wired | Transport exists, but tested firmware reliability varies; do not configure as the only production source. |
| Camera AI (`getEvents`) | Poll recent events | Person bit (events_1 bit 19), `face_id`, vehicle/pet classification. Logged to SD regardless of detection toggles. |
| Hub clip index (`hubpoll`) | Poll the hub a battery camera records to | A sleeping battery camera keeps no event index of its own; clips are read off the hub and the alert frame comes from a go2rtc sidecar. See [battery cameras on a hub](battery-cameras-on-a-hub.md). |
| Motion detection | Camera setting / classifier support | `digital_sensitivity` 0–100 (or low/normal/high). Tunable per weather; not a standalone daemon event source today. |
| Person detection | AI, separate sensitivity | Drives people-only auto-tracking. |
| PIR sensor | `alarm_type` | Hardware PIR confirmation where present. |
| Other (available, unused) | line-crossing, package, glass-break, bark / baby-cry / meow | Exposed by the firmware; not wired into this stack. |

## 2. Reaction & tracking

- **Auto-tracking** master switch + **SmartTrack** category selection (people / vehicle
  / pet / baby) — track only what you care about.
- **PTZ presets** — park the camera at a fixed view (static role) or return after tracking.
- **Soft pan-limit** (optional, per camera) — `pan_limit` keeps auto-track within the span
  of the camera's presets. The local Tapo API has no angular limit or motor-position
  readout, so the daemon reads the pan via ONVIF and recalls the camera to its bounding
  preset when it drifts past the leftmost/rightmost preset (e.g. auto-track swinging into a
  wall). The presets define the allowed range; ONVIF errors never stall the loop.
  `pan_limit.tilt` extends the same guard to the tilt axis, with `tilt_min`/`tilt_max`
  keeping an outlier preset (one aimed at the sky) from stretching the bound. While a
  `track_hold` keeps the lens on a subject the guard waits `pan_limit.hold_grace`
  seconds (default 20) before recalling, and it never moves a lens parked by privacy mode.
- **Day/night scheduling** — astral sunset/sunrise (coordinates from config) with a
  fixed HH:MM fallback. One source of truth shared by all components.
- **Night vision mode** (optional, per camera) — `night_vision: ir` forces IR/B&W night
  vision on that schedule (day/colour by day), re-asserted each control tick; `auto`
  re-asserts the camera's own day/night switch. A colour night mode under a streetlight
  runs a slow shutter that smears moving subjects, so IR's faster shutter keeps the event
  frame sharper.
- **Lens Distortion Correction (LDC)** (optional, per camera) — `ldc: true` toggles hardware
  barrel-distortion correction directly on wide-angle 4K sensors (e.g. C560WS, C260).
  Straightening vertical and horizontal lines across the field of view prevents subjects
  at the frame perimeter from being distorted, boosting YOLO scorer bounding-box precision.
  State is continuously tracked for drift by the Digital Twin (`video.ldc.enabled`).
- **Tamper detection self-healing** (optional, per camera) — `tamper_detection: true` with
  `tamper_sensitivity: low|normal|high` re-asserts tamper monitoring every control tick.
  Blinding, lens covering, or physical redirection can no longer remain silently disabled.
  In the alert funnel, tamper events bypass visual subject confidence thresholds to ensure
  covered or blacked-out lenses trigger alerts immediately. Drift is reported as `critical`.
- **Scaled whitelamp pulse duration** (optional, per camera) — `whitelamp_force_time: 30`
  (5–300 s) overrides the excessive 300 s (5 minute) firmware default on white floodlight
  activation. Paired with `whitelamp_intensity: 1–100`, detection-triggered lighting stays
  polite, short, and focused without illuminating the street unnecessarily.
- **Safe OSD formatting** — `set_osd_safe` wraps on-screen display updates via standard
  `executeFunction` JSON-RPC rather than raw `performRequest`, avoiding connection drops and
  firmware IP lockouts on outdoor models.

## 3. Enrichment & notification

- **Snapshot, live + SD hybrid** — a live RTSP grab first; because a bare grab often misses
  the subject (the event fires on motion start, the person walks in seconds later), a
  confirmed person can trigger an **SD-card follow-up** that downloads the recorded segment
  around the event, extracts several candidate frames across it, and picks the one the
  subject is actually in — so a missed live grab becomes an in-frame photo instead of a
  blank ping. With `snapshot_source: recording` the follow-up frames come from a local 24/7
  recorder (`RECORDING_ROOT`) instead of the camera SD — full stream1 resolution even when
  detection runs on stream2, and it sends the sharpest above-threshold frame (ffmpeg
  `blurdetect`) rather than the first. It requires `sd_snapshot: true` (it reuses the SD
  follow-up queue) and falls back to the SD/live path when no segment is available.
- **Local YOLO scorer (optional)** — a stateless HTTP scorer gates person alerts by
  person confidence; animal confidence never triggers an alert, but a confident animal
  score does add a paw to the caption of an alert that was already going out.
  Groq then captions only frames that already passed the scorer. Optional tiled inference
  scores the whole image plus a grid to rescue distant subjects in wide views; `crop_to_subject` uses the
  winning person box for a padded alert-photo zoom and safely falls back to the full frame.
  With `crop_from_native` that zoom is cut from a native-resolution grab rather than the
  already-downscaled frame — a figure spanning 5% of the width is ~64px across at 1280 and
  ~190px at 4K. The frame is reduced where it is captured and the native original travels
  with it, so the scorer, the captioner and Telegram keep receiving delivery-width images
  and only the crop spends the detail. Off by default; measured within one stream the extra
  grab is free on a Pi 4 and about +0.5 s on a Pi Zero 2 W, and a stream already at delivery
  width skips the native original altogether. A too-tall zoom is widened just enough to
  stop being a vertical sliver — a standing figure is naturally taller than the scene, so
  widening all the way to the scene's own ratio would spend the zoom on empty margin.
- **Event-window sampler (optional)** — for long camera events, follow-up RTSP grabs
  across the event window catch people who enter frame after the first live grab.
- **AI description** — Groq vision model returns a short scene description for approved
  frames.
- **Face-ID naming** — map stable `face_id`s to names locally.
- **Telegram** — photo + caption plus operational alerts. Delivery is confirmed before
  cooldown/outage state advances; SD, sampler and recovery paths retry failed sends.

## 4. Weather gating

Rain makes auto-tracking cameras chase raindrops and IR reflections. Using open-meteo
(coordinates from config) with a result cache and hysteresis, two strategies are offered:

- `lower_sensitivity` — drop motion sensitivity while it rains, keep tracking.
- `disable_tracking` — turn auto-tracking off for the duration of the rain.
- `storm_park` — an independent flag (composes with either strategy) that **parks the PTZ**
  while it rains, so a `lower_sensitivity` camera can both lower sensitivity *and* stop
  swinging after raindrops/branches. (Auto-tracking runs at night here, so this bites at
  night in the rain; by day the tracking cameras are already static.)

## 5. Multi-camera and schedules

- Run any number of cameras from one config.
- `night_only` cameras drain daytime events silently and alert only during the astral
  night window, while camera control still runs all day.
- **Coordinator duplicate gate** (shipped, observation-only) — cameras sharing a
  `coordinator.group` suppress duplicate alerts for the same passage: once one camera's
  detection is *delivered*, peers stay quiet for events inside `scene_window` seconds.
  Live, sampler and SD paths share the gate; it never moves a camera.
- **Perimeter handoff** (planned) — when one camera in a group detects a person,
  peers turn to a hand-off preset so overlapping/adjacent views cover the same target.
  `handoff_preset` is reserved for this and not executed yet. Constraint: `getEvents`
  reports *that* a person was seen, not *where* — so direction is handled by each
  camera's own auto-tracking, not by the coordinator.

## 6. Operations

- Camera-down watchdog with de-duplicated Telegram alerts.
- Event-API watchdog: records `getEvents` errors, alerts on sustained failures, reports
  recovery, and can request one API reboot per failure episode.
- Daemon dead-man's switch (`alerts.stall_threshold`): the camera watchdog runs inside the
  tick, so a fault in the tick suppresses the alerting meant to report it. A second
  watchdog in the outer loop sends 🔴 once every tick has raised for the threshold and 🟢
  when one completes again. It shares the process it guards, so it covers a *raising*
  tick — not a hung one, and not a crash loop, which reset the timer on restart.
- Reconnect handling and lockout-aware sessions (see below).
- Structured audit logs plus `tapo-monitor audit-log` for threshold calibration.
- Daily digest heartbeat: the review digest carries a fleet block — camera reachability,
  the daemon's tick, the shared scorer, recorder freshness, alert counts, refused
  self-heals, the frame logs' size and free disk, and the running package fingerprint.
  A one-shot warning fires when the log filesystem falls below `TAPO_LOG_DISK_MIN_FREE_MB`. It claims OK only for what it actually
  checked; any failed check removes the headline.
- JSON status endpoint (`observability.status_port`): daemon + fleet summary as one GET,
  localhost-first because it has no authentication.
- Mutual host watch (`tools/host_watch.sh`): peers ping each other (optionally a `/health`
  URL too), so a dead host is noticed by a machine other than itself.
- Release deploys (`tools/deploy_release.sh`): fingerprinted release directories behind a
  `current` symlink, selfcheck before the switch, rollback by re-pointing the link.
  `tapo-monitor version` and `tapo-monitor selfcheck` state what a host runs and whether
  it can run.
- systemd templates for the monitor daemon and shared scorer service.
- Deployment, health and calibration runbook ([`operations.md`](operations.md)), including
  setups that share one scorer across several caller services.

## 7. Dual-lens cameras: Tapo C545D

Read-only findings from one C545D (HW 1.0, firmware 1.1.2 auto-upgraded to 1.1.7
during the probe; rows are from 1.1.7), taken with pytapo 3.4.18, ffprobe and onvif-zeep.
Configure it with `event_profile: c545d` — see [configuration](configuration.md#dual-lens-cameras-c545d).

The C545D has two sensors in one body: a **fixed wide lens** (channel 1) and a
**pan/tilt lens** (channel 2). One login, one IP, one device: the lenses are *channels*,
not child devices (`getChildDeviceList` → -40210).

### Lens addressing

| Layer | How a lens is addressed | Notes |
|---|---|---|
| Local API (pytapo) | `chn_id` list on chn-aware getters (`[1]`, `[2]`, `[1, 2]`) | `getAllChnInfo` → channel 1 "Fixed Lens", channel 2 "PT Lens". Without `chn_id` a getter answers for channel 1 only. |
| RTSP | a path per lens | `stream1` 2304×1296 / `stream2` 1280×720 = wide lens; `stream6` 2304×1296 / `stream7` 1280×720 = pan/tilt lens; `stream8` MJPEG 640×360 (~1 fps) = wide lens. All H.264 High, 15 fps, PCM A-law audio. `stream3`/`stream5` → 404, `stream4` → 406; a query string (`stream1?channel=2`) is ignored. |
| ONVIF Media (port 2020) | wide lens only | one video source; `profile_1` → `stream1`, `profile_2` → `stream2`, `profile_3` → `stream8`. `GetSnapshotUri` fails. |
| ONVIF PTZ | on every profile, moves the pan/tilt lens | pan/tilt only (no zoom space), 8 presets max, `HomeSupported: false`; `GetStatus` gives the position `pan_limit` reads. |

The wide lens is strongly barrel-distorted; the pan/tilt lens has roughly half its field
of view, so a person it follows appears about twice as large.

### Local API getters

Per lens (`chn_id`): `getMotionDetection`, `getPersonDetection`, `getVehicleDetection`,
`getPetDetection`, `getTamperDetection`, `getLinecrossingDetection`, `getDayNightMode`,
`getLensDistortionCorrection` (channel 2 → `null`, no LDC on the pan/tilt lens),
`getNightVisionModeConfig` / `getWhitelampConfig` / `getRotationStatus` (channel 2 returns
only `night_vision_mode` and `wtl_intensity_level`).

Device-wide: `getBasicInfo` (`device_model: C545D`), `getAllChnInfo`,
`getDualCamCapability` (`zooms: ["1.0x", "5.0x"]`, likely the app's hybrid zoom),
`getDualCamLinkage` (`linkage_state {enabled, linkage_type}`), `getLinkageTargetCapability`
and `getLinkageTargetSetting` (people / pet / vehicle), `getPrivacyMode` (with `chn_id` →
-40101), `getSDCard`, `getRecordPlan`, `getCircularRecordingConfig`, `getAlertEventType`,
`getPresets`, `getAutoTrackTarget` (`track_mode: pantilt`), `getSmartTrackConfig`,
`getPatrolSchedule`, `getVideoQualities` (main stream only; with `chn_id` → -40101),
`getVideoCapability`, `getAudioConfig`, `getNightVisionCapability` (infrared + white lamp),
`getWhitelampStatus`, `getOsd`, `getCoverConfig`, `getFirmwareAutoUpgradeConfig`.

Not on this model (-40101 / -40210): siren/alarm config, floodlight, PIR, package,
baby-cry, bark, meow, glass-break and face detection.

Without an SD card `getEvents` (`searchDetectionList`) and the recordings list return
-71114 STORAGE_NOT_EXIST: the event index lives on the card, so this camera needs one for
`sources: [getevents]`. `getLastAlarmInfo` with `{"system": {"name":
["last_alarm_info"]}}` (not wrapped by pytapo) answers without a card, but only with the
last alarm's time and a coarse type (a person was reported as `motion`).

**Do not call:** `checkDetectEventState {}` — the HTTPS API refused connections for about
10 s right after it. Keep calls at least ~1.5 s apart; closer ones return -40109
`ONE_SECOND_REPEAT_REQUEST`. The raw `performRequest` wrappers stay off-limits as on
every model.

### Events

`getEvents` entries carry no top-level `events_1`. Each lens that fired reports under
`chn_events`:

```json
{"start_time": 1790330876, "end_time": 1790330984, "alarm_type": 6,
 "chn_events": {"1": {"events_1": 34, "event_start_time": 1790330878},
                "2": {"events_1": 34, "event_start_time": 1790330876}}}
```

Observed, n=10 (8 person walks, 2 plain motion), checked against what the app reported:

| What happened | `alarm_type` | channels | `events_1` |
|---|---:|---|---:|
| a person walking by | 6 | 1 and 2 | 34 (bits 1 + 5) |
| plain motion | 2 | 1 only | 2 (bit 1) |

The AI-person bit 19 (524288) was not set for the person, and bit 5 / `alarm_type` 6 —
the PIR on a C560WS — cannot be a PIR here, the model has none. `event_profile: c545d`
reads that pair as a person; see [the bitmask notes](events1-bitmask.md#model-specific-meaning).
A long event is split into consecutive events about every 180 s, and `end_time` grows
while it is active. A later query returned one event's `start_time` 1 s earlier than the
first poll did; the watermark treats that as already seen.

### Firmware lens linkage

`dualCamLinkage` (found on, target people) turns the pan/tilt lens after a
person the wide lens saw. With `event_profile: c545d` an event on channel 2 keeps the
scheduled preset recall and the pan-limit guard off that lens for 180 s after the
event (motion arbiter reason `linkage`), whatever the auto-track switch says. The twin
reports the linkage switching off (`dual_cam.linkage.enabled`, warning).

### What tapo-monitor does with it

- Events are normalized ([`detection.normalize_event`](../tapo_monitor/detection.py)):
  `events_1` becomes the OR of the lenses, `channels` lists the lenses that fired, and
  audit lines carry `channels=1,2`.
- Streams: `rtsp_stream: stream2` for a fast wide-lens live grab, `sampler.stream:
  stream6` for follow-up grabs from the lens that turns toward the subject, and optionally
  `lens_pick_stream: stream7` to grab both lenses on a pan/tilt event and keep the frame
  with the larger subject.
- Digital twin: with the profile it also reads the lens layout, the linkage state and
  the detection switches of both lenses (`detection.person.chn2.enabled` is a critical
  drift path — our self-heal setters without `chn_id` reach channel 1 only).
- An SD card whose `detect_status` is `dilatant_suspect` (fake capacity; such cards also
  came up read-only and would not format) marks storage degraded on any model.

### Open questions

- Which lens a setter without `chn_id` changes, and whether turning auto-track off
  (`role: static`) also turns the linkage off. Until that is known, run it with
  `role: static` and no presets, or check the twin after the first control passes.
- More event samples, in the dark and for pets/vehicles; the table above is n=10.
- ONVIF PullPoint: the first `PullMessages` succeeded, later ones were closed by the
  camera (`RemoteDisconnected`). Not usable as a source yet.
- Components not yet probed: `panoramicView`, `markerBox`, `blockZone`,
  `detectionRegion`, `audioCapability`, `nvmp`, `snapshot`.

## Actuators (hardware capabilities & safety policy)

The firmware exposes an active-response layer reachable through the local API:

- **Acoustic siren + alarm** — `startManualAlarm` / `stopManualAlarm` / `playAlarm`. Intentionally
  **not implemented** and prohibited to prevent public neighborhood nuisance on street cameras.
- **Speaker / two-way audio** — `setSpeakerVolume`, `testUsrDefAudio`. Intentionally **not implemented**.
- **Floodlight / white lamp pulse** — `reverseWhitelampStatus` and `setWhitelampConfig`. **Implemented**
  as opt-in `light_trigger` with duration scaling (`whitelamp_force_time: 30–60 s`) to illuminate
  subjects without triggering loud acoustic alarms or blinding neighbors.

## Beyond pytapo — what this project adds

`pytapo` is a thin API client. On top of it this project adds the operational glue it
lacks:

- **Camera Digital Twin** — low-frequency, redacted snapshots from safe getters on the
  daemon's existing session; layered network/API/events/RTSP/storage health; desired-state
  drift with stable keys; and opt-in new/recovered drift alerts. Unsupported or empty
  firmware responses remain unknown and do not become false alarms.
- **Shadow Detection Auditor** — a private local, media-free event ledger correlates
  camera `getEvents` detections with independent recorder/scorer observations. It exposes
  deterministic matched, camera-only and shadow-only counts without pretending the latter
  are automatically proven misses.

- **Astral day/night scheduling** — pytapo has no concept of sunset/sunrise windows.
- **Weather gating** — rain-aware sensitivity / tracking, with API caching + hysteresis.
- **Lockout-aware sessions** — the C560WS locks out a source IP for ~30 min after failed
  logins, and the first login after a reconnect often fails with "Invalid authentication
  data" before a retry succeeds. The camera wrapper centralizes retry/backoff so callers
  don't rediscover this the hard way.
- **Normalized detection model** — shared classification shapes for `getEvents`, ONVIF
  and motion research. The production daemon currently polls `getEvents`; alternative
  event-source wiring remains roadmap work.
- **Scorer/gating separation** — camera firmware produces events, the optional YOLO
  scorer validates subject-bearing frames, and Groq is demoted to captioning.
- **SmartTrack ordering safety** — `setSmartTrackConfig` silently clears the auto-track
  master switch on this firmware; the tracking layer always (re)asserts auto-track *last*.
- **Sensitivity type gotcha** — `setMotionDetection(sensitivity="60")` (a numeric string)
  is remapped by pytapo to the `"high"` label (digital 80) — the opposite of intent. Pass
  an `int` to set the digital value exactly. Encapsulated so callers can't trip on it.
- **Decoupled fast poll** — `getEvents` is polled every few seconds on the *already
  connected* client while camera control runs on a slower tick, so detection latency stays
  low without a per-tick re-login (which risks the lockout above). pytapo leaves this to you.
- **Reliable SD media download** — pytapo's media stream silently fails inside a
  long-running poller ("Cannot run the event loop while another loop is running"). This
  project runs the download in a fresh subprocess with its own client, pre-warms
  `getUserID()` before the download loop, and caps `window_size` at 50 (the C560WS stalls
  at pytapo's default 200). See
  [SD-card download returns no frames](troubleshooting.md#sd-card-download-returns-no-frames).
