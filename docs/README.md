# Documentation

This directory documents the public behavior of tapo-monitoring. Deployment-specific
camera names, addresses, credentials, coordinates and observations do not belong here.

## Choose a path

### I want to run the project

1. [Getting started](getting-started.md) — **start here**: one camera, person detection,
   Telegram, in about ten minutes.
2. [Credentials](credentials.md) — the two camera logins, which value goes where, and the
   lockout rule. Read this before your first run.
3. [Configuration](configuration.md) — the full reference: every option in `cameras.yaml`,
   grown one feature at a time from the minimal start.
4. [Operations](operations.md) — install the systemd units, inspect health and calibrate
   alert thresholds.
5. [Troubleshooting](troubleshooting.md) — diagnose login lockouts, missing frames and
   firmware-specific behavior.
6. [Docker](docker.md) — run the monitor and the optional scorer from the container
   images, with Compose, instead of a venv and systemd.
7. [Network measurements](network-watch.md) — compare packet loss, RTT and jitter at
   the router and cameras with a bounded history and a fleet summary.
8. [Log retention](log-retention.md) — archive and ledger cleanup, database sizes,
   and explicit journal/syslog storage policies.

### I want to understand or extend it

1. [Architecture](architecture.md) — component boundaries, loop timing, persistence and
   failure containment.
2. [Capabilities](capabilities.md) — implemented and deliberately unimplemented features.
3. [`events_1` bitmask](events1-bitmask.md) — known firmware event signals.
4. [Local API reference](tapo-local-api.md) — the camera's local API beyond pytapo:
   parameter conventions, error codes, SD card states, event shapes and calls that take
   the API down.
5. [Battery cameras on a hub](battery-cameras-on-a-hub.md) — how a sleeping camera's events
   are read off the hub it records to, and the hub rules that shape the client.
6. [Observability](observability.md) — Camera Digital Twin and Shadow Detection Auditor.
7. [Camera reports](camera-reports.md) — produce an anonymized report of your own camera
   model so it can be supported without the maintainers owning one.
   [C216 model notes](c216.md) distinguish verified paths from open event-mapping,
   SD-rescue and tracking-safety tests.
8. [MQTT and Home Assistant](mqtt.md) — the opt-in, publish-only bridge: entities, topics,
   delivery guarantees and an example automation.
9. [Labeling](labeling.md) — label collected sent/review-log frames and measure false
   alarms, misses and the best threshold; optional box verdicts and hand-drawn persons
   support per-model recall checks.
10. [Watch zones, loitering and vehicle alerts](watch-zones.md) — restrict person alerts
    to polygons, get one notice for someone who lingers, and alert on vehicles.
11. [Measurements](measurements.md) — anonymized accuracy, miss, latency and replay
    figures from a real deployment, and how to reproduce them.
12. [Detector comparison](measurements.md#detector-comparison) — accuracy, latency and
    hand-checked disagreements of several person detectors, and the
    [benchmark tool](../tools/bench_detectors/README.md) to repeat it on your own frames.
    [Shadow comparison and selection](operations.md#build-a-labeling-set-from-two-to-four-shadow-targets)
    builds a review sample from two to four shadow logs, including missed-person candidates.
    Comparisons do not switch the production model automatically.
13. [Roadmap](roadmap.md) — remaining product phases and research tracks.
14. [Releasing](releasing.md) — how a GitHub release reaches PyPI, and the one-time
    Trusted Publishing setup.
15. [Upgrading to 0.9.0](upgrading.md) — changed defaults, optional features and
    the scorer-first deployment sequence.

## Feature maturity

| Area | Maturity | Notes |
| --- | --- | --- |
| Config-driven fleet daemon | Operational | One daemon manages multiple cameras. |
| `getEvents` detection and Telegram pipeline | Operational | Primary production event path. |
| Live + SD/local-recorder frame selection | Operational, opt-in media follow-up | Requires the matching credentials/storage source. |
| HTTP scorer and subject crop | Operational, optional | Configured failure policy; low-scored sampler groups hold unscored frames by default. |
| Scorer/camera address failover | Operational, opt-in | Scorers must be interchangeable; camera addresses must reach the same device. |
| Detector shadow comparisons | Opt-in evaluation | Up to four independent targets; live decisions unchanged without ensemble mode. |
| Ensemble decision policy | Experimental, opt-in | Uses the first shadow; thresholds require validation on your own frames. |
| Human frame/box review | Operational, opt-in box mode | Missed-person sampling and per-model recall support manual detector evaluation. |
| Persistent Telegram outbox | Operational, opt-in | Bounded late delivery after outages; off by default. |
| Watch zones, loitering and vehicle notices | Operational, opt-in | Observation modes let you review candidate decisions before enforcement. |
| Weather, day/night and PTZ control | Operational | Model/firmware behavior can differ. |
| Network uptime and outage alerts | Operational | State persists across restarts. |
| ICMP network measurements | Operational, opt-in standalone tool | Router/camera loss, RTT and jitter; bounded daily history. Sleeping battery cameras are not ping targets. |
| Camera Digital Twin | Operational, opt-in | Read-only probes, layered health, drift and allow-listed self-healing. |
| Shadow Detection Auditor | Operational, opt-in | Ledger/reporting complete; the nightly `shadow-scan` batch is the independent watcher (v1). |
| Battery/hub cameras (`hubpoll`) | Operational, opt-in | Detections polled from the hub, frames from a go2rtc sidecar. |
| MQTT bridge / Home Assistant discovery | New, opt-in | Publish-only; off unless the `mqtt:` block is set. |
| Deployment and fleet integrity | Operational | Fingerprinted release directories, symlink rollback, selfcheck, digest heartbeat. |
| ONVIF event source | Researched, not daemon-wired | Do not select it as the only event source. |
| Multi-camera coordinator | Duplicate gate operational; handoff planned | `group`/`scene_window` suppress cross-camera duplicates; `handoff_preset` is reserved and no runtime handoff exists. |
| Siren/light/speaker actions | Deliberately excluded | Observe-and-notify safety boundary. |

## Glossary

Terms that recur across these docs:

- **Camera Account** — the username/password you create on the camera (Tapo app → Advanced
  Settings), used for the local API and RTSP. Not the TP-Link cloud login. See
  [credentials](credentials.md).
- **cloud password** — the TP-Link account password; needed only for the SD-card download.
- **`getEvents`** — the camera's own detection list, read off its SD card; the default
  event source.
- **`hubpoll`** — for battery cameras: detections are read from the H200 hub they record
  to, not the camera directly. See [battery cameras on a hub](battery-cameras-on-a-hub.md).
- **`event_profile`** — per-model decoding of the camera's event signals (e.g. `c545d` for
  the dual-lens model); `default` fits most cameras.
- **sampler** — after an event, it keeps scoring frames across the event window to catch
  the moment the subject is clearest, rather than judging one live frame.
- **follow-up (SD / recording)** — when the live frame misses the subject, pull the right
  frame from the camera's SD recording or a local recorder and send that instead.
- **scorer** — an optional HTTP service that rates whether a frame contains a person.
  Scoring failures follow the [failure policy](operations.md#when-the-scorer-itself-fails);
  camera-confirmed alerts can still pass, while a low-scored sampler group holds later
  unscored frames by default until its last scheduled frame.
- **detector shadow** — an optional candidate scorer fed copies of live frames, with its
  own queue and comparison log. Up to four can run; they do not change alerts unless
  the first is explicitly used by the experimental ensemble policy.
- **missed-person candidate** — a low primary score with a higher shadow score, selected
  for human review. The disagreement is not itself a confirmed missed person.
- **outbox** — an opt-in persistent queue for failed Telegram deliveries, with bounded
  retention and late delivery; separate from pending media-work state.
- **Camera Digital Twin** — opt-in read-only health model of a camera: layered health,
  config drift and allow-listed self-healing. See [observability](observability.md).
- **Shadow Detection Auditor** — opt-in independent watcher that logs what a second
  detector would have seen, to measure misses without changing live alerts.
- **ledger** — a local SQLite store of normalized event/decision metadata (no frames or
  secrets) that the twin and auditor read.
- **selfcheck / fingerprint** — the deploy integrity check: each release directory is
  stamped with a content fingerprint so a host can prove which package it runs.

## Documentation rules

- Examples use placeholders or documentation-only addresses.
- Secret fields show environment-variable names, never values.
- Unsupported camera behavior is described as unknown until it is reproduced.
- Planned behavior is labelled explicitly and must not be presented as operational.
- Historical local experiments and deployment notes stay outside the public documentation.

The package entry point is documented in the root [README](../README.md). Security reports
follow [SECURITY.md](../SECURITY.md); code contributions follow
[CONTRIBUTING.md](../CONTRIBUTING.md).
