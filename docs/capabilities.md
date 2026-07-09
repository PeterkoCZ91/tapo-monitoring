# Tapo detection — capability catalog

What a TP-Link Tapo PTZ camera (validated on the C560WS) can do for monitoring, and
what this project adds on top. Each capability is opt-in per camera via `cameras.yaml`.

This project is **passive surveillance only**: it detects, describes and notifies. The
active actuator layer (siren, floodlight, speaker) is documented below as *available*
but is intentionally **not implemented** — see "Actuators".

## 1. Detection inputs

| Source | How | Notes |
|---|---|---|
| ONVIF pull-point events | Real-time event subscription | `isPeople` / `isPet` / `isCar` / `isMotion` / tamper flags. Lowest latency. |
| Camera AI (`getEvents`) | Poll recent events | Person bit (events_1 bit 19), `face_id`, vehicle/pet classification. Logged to SD regardless of detection toggles. |
| Motion detection | Configurable | `digital_sensitivity` 0–100 (or low/normal/high). Tunable per weather. |
| Person detection | AI, separate sensitivity | Drives people-only auto-tracking. |
| PIR sensor | `alarm_type` | Hardware PIR confirmation where present. |
| Other (available, unused) | line-crossing, package, glass-break, bark / baby-cry / meow | Exposed by the firmware; not wired into this stack. |

## 2. Reaction & tracking

- **Auto-tracking** master switch + **SmartTrack** category selection (people / vehicle
  / pet / baby) — track only what you care about.
- **PTZ presets** — park the camera at a fixed view (static role) or return after tracking.
- **Day/night scheduling** — astral sunset/sunrise (coordinates from config) with a
  fixed HH:MM fallback. One source of truth shared by all components.

## 3. Enrichment & notification

- **Snapshot, live + SD hybrid** — a live RTSP grab first; because a bare grab often misses
  the subject (the event fires on motion start, the person walks in seconds later), a
  confirmed person can trigger an **SD-card follow-up** that downloads the recorded segment
  around the event, extracts several candidate frames across it, and picks the one the
  subject is actually in — so a missed live grab becomes an in-frame photo instead of a
  blank ping.
- **Local YOLO scorer (optional)** — a stateless HTTP scorer can decide whether a
  frame actually contains a person/animal before Telegram is sent. Groq then captions
  only frames that already passed the scorer.
- **Event-window sampler (optional)** — for long camera events, follow-up RTSP grabs
  across the event window catch people who enter frame after the first live grab.
- **AI description** — Groq vision model returns a short scene description for approved
  frames.
- **Face-ID naming** — map stable `face_id`s to names locally.
- **Telegram** — wide photo + caption plus operational alerts.

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
- **Perimeter coordination** (planned) — when one camera in a group detects a person,
  peers turn to a hand-off preset so overlapping/adjacent views cover the same target.
  Constraint: `getEvents` reports *that* a person was seen, not *where* — so direction
  is handled by each camera's own auto-tracking, not by the coordinator.

## 6. Operations

- Camera-down watchdog with de-duplicated Telegram alerts.
- Reconnect handling and lockout-aware sessions (see below).
- Structured audit logs plus `tapo-monitor audit-log` for threshold calibration.
- systemd templates for the monitor daemon and shared scorer service.
- Runtime topology and deployment/health runbooks for setups that share one scorer with
  A12 or other callers.

## Actuators (available, documented, NOT implemented)

The firmware exposes an active-response layer reachable through the local API:

- **Siren + light alarm** — `startManualAlarm` / `stopManualAlarm` / `playAlarm`.
- **Floodlight / spotlight** — `setForceWhitelampState`, `manualFloodlightOp`.
- **Speaker / two-way audio** — `setSpeakerVolume`, `testUsrDefAudio` (play a warning).

These are listed for completeness. This project deliberately ships no code that triggers
them; it stays a passive observe-and-notify stack.

## Beyond pytapo — what this project adds

`pytapo` is a thin API client. On top of it this project adds the operational glue it
lacks:

- **Astral day/night scheduling** — pytapo has no concept of sunset/sunrise windows.
- **Weather gating** — rain-aware sensitivity / tracking, with API caching + hysteresis.
- **Lockout-aware sessions** — the C560WS locks out a source IP for ~30 min after failed
  logins, and the first login after a reconnect often fails with "Invalid authentication
  data" before a retry succeeds. The camera wrapper centralizes retry/backoff so callers
  don't rediscover this the hard way.
- **Unified detection** — one abstraction over ONVIF events, `getEvents` and motion,
  instead of three call sites with different shapes.
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
  at pytapo's default 200). See the *Hard-won gotchas* section of the README.
