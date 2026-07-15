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

### I want to understand or extend it

1. [Architecture](architecture.md) — component boundaries, loop timing, persistence and
   failure containment.
2. [Capabilities](capabilities.md) — implemented and deliberately unimplemented features.
3. [`events_1` bitmask](events1-bitmask.md) — known firmware event signals.
4. [Observability](observability.md) — Camera Digital Twin and Shadow Detection Auditor.
5. [Roadmap](roadmap.md) — remaining product phases and research tracks.

## Feature maturity

| Area | Maturity | Notes |
| --- | --- | --- |
| Config-driven fleet daemon | Operational | One daemon manages multiple cameras. |
| `getEvents` detection and Telegram pipeline | Operational | Primary production event path. |
| Live + SD/local-recorder frame selection | Operational, opt-in media follow-up | Requires the matching credentials/storage source. |
| Local HTTP scorer and subject crop | Operational, optional | Fails open if unavailable. |
| Weather, day/night and PTZ control | Operational | Model/firmware behavior can differ. |
| Network uptime and outage alerts | Operational | State persists across restarts. |
| Camera Digital Twin | Foundation, opt-in | Read-only probes, layered health and drift. |
| Shadow Detection Auditor | Foundation, opt-in | Ledger/reporting complete; independent watcher planned. |
| ONVIF event source | Researched, not daemon-wired | Do not select it as the only event source. |
| Multi-camera handoff coordinator | Planned | Config fields are reserved but no runtime handoff exists. |
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
