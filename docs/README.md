# Documentation

This directory documents the public behavior of tapo-monitoring. Deployment-specific
camera names, addresses, credentials, coordinates and observations do not belong here.

## Choose a path

### I want to run the project

1. [Configuration](configuration.md) — prepare camera accounts, environment variables and
   `cameras.yaml`.
2. [Operations](operations.md) — install the systemd units, inspect health and calibrate
   alert thresholds.
3. [Troubleshooting](troubleshooting.md) — diagnose login lockouts, missing frames and
   firmware-specific behavior.
4. [Docker](docker.md) — run the monitor and the optional scorer from the container
   images, with Compose, instead of a venv and systemd.
5. [Network measurements](network-watch.md) — compare packet loss, RTT and jitter at
   the router and cameras with a bounded history and a fleet summary.

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
8. [MQTT and Home Assistant](mqtt.md) — the opt-in, publish-only bridge: entities, topics,
   delivery guarantees and an example automation.
9. [Labeling](labeling.md) — label collected sent/review-log frames and measure false
   alarms, misses and the best threshold.
10. [Roadmap](roadmap.md) — remaining product phases and research tracks.
11. [Releasing](releasing.md) — how a GitHub release reaches PyPI, and the one-time
    Trusted Publishing setup.

## Feature maturity

| Area | Maturity | Notes |
| --- | --- | --- |
| Config-driven fleet daemon | Operational | One daemon manages multiple cameras. |
| `getEvents` detection and Telegram pipeline | Operational | Primary production event path. |
| Live + SD/local-recorder frame selection | Operational, opt-in media follow-up | Requires the matching credentials/storage source. |
| Local HTTP scorer and subject crop | Operational, optional | Fails open if unavailable. |
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

## Documentation rules

- Examples use placeholders or documentation-only addresses.
- Secret fields show environment-variable names, never values.
- Unsupported camera behavior is described as unknown until it is reproduced.
- Planned behavior is labelled explicitly and must not be presented as operational.
- Historical local experiments and deployment notes stay outside the public documentation.

The package entry point is documented in the root [README](../README.md). Security reports
follow [SECURITY.md](../SECURITY.md); code contributions follow
[CONTRIBUTING.md](../CONTRIBUTING.md).
