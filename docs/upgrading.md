# Upgrading to 0.10.0

This guide covers the changes since 0.9.0; the 0.9.0 guide follows below. Nothing needs
configuring: install the new package on the monitor hosts and the scorer as usual.

## Why upgrade

- **Better follow-up photos.** Frames smeared by the camera's own pan/tilt swing are set
  aside before the photo is picked, so `largest`/`tallest` stop choosing a blurred frame.
  An opt-in face preference (`sd_prefer_face` with a scorer face model) picks a frame that
  shows the person's face when there is one.
- **Fewer lost card reads.** A card read whose first request fails with a transient
  session error gets one more try on a fresh login, on every camera.
- **Slow uplinks stop timing out.** Follow-up frames are scored one at a time by default.
- **Scorer on a CPU.** `TAPO_SCORER_THREADS` bounds the detector's and face detector's
  threads to the CPUs the service gets; see
  [Running the scorer on a CPU](operations.md#running-the-scorer-on-a-cpu) and the
  [detector comparison](measurements.md#a-week-of-shadow-scoring-on-live-cameras).

## Changes to existing behavior

| Change | Effect | Setting |
| --- | --- | --- |
| Smeared follow-up frames | A frame below 0.4x the clip's median detail is skipped while a steady candidate remains. | Always on. |
| Follow-up scoring concurrency | SD and recording follow-up frames are uploaded to the scorer one at a time (before: up to four at once). A batch can take a little longer on a fast link. | `sd_score_parallel: 2` or `4` per camera restores parallel uploads. |
| Card-read retry | Every camera retries a transient `-40214` / `Invalid authentication data` once, 5 s later, on a new login (before: only with `sd_early_look`). | Always on. |
| Boot notification | Retries transient network errors up to three times while the network starts and no longer waits for a specific VPN service. | `systemd/pi-boot-notify.service` |

# Upgrading to 0.9.0

This guide covers the changes since 0.8.1. For the full feature list, see the
[changelog](../CHANGELOG.md) and [capabilities](capabilities.md).

## Why upgrade

- **Alerts survive an internet outage.** Before, the in-memory retries gave up after
  about ten minutes, so a night-long outage lost every alert of the night. With the
  optional outbox they are kept on disk and delivered once Telegram answers again,
  with one outage summary per camera (English, or Czech with `outbox.lang: cs`).
- **Fewer misleading or redundant alerts.** An alert sent without a score is marked
  `(unverified)` instead of passing as a confirmed person, small crops no longer
  arrive as unreadable thumbnails, damaged grey frames are not scored, and a motion
  photo that already showed a person suppresses the duplicate SD follow-up.
- **More stable long runs.** Event-ledger transactions close their SQLite connections
  deterministically, which fixes connection build-up on Python 3.13.
- **More control without risk.** Watch and ignore zones, loitering and vehicle alerts,
  scorer and camera failover lists and scorer authentication are all opt-in, and
  `observe` modes let you trial a rule before it affects alerts.
- **Evaluate detectors on your own data.** Up to four shadow detectors (optionally
  rate-limited per target) run beside the live one without changing alerts, with
  labeling, box review and report tools to compare them.
- **One more tested camera:** the TC46 behaves like the C560WS.

## Changes to existing behavior

| Change | Effect | Setting |
| --- | --- | --- |
| Sampler scoring failures | When earlier frames in the group scored below threshold, an unscored follow-up waits for the next interval; the final frame still passes through. Live fail-open behavior remains. | `scorer.on_error: hold` is the default; `open` restores the earlier sampler behavior. |
| Small native crops | The whole frame is the alert, followed by a zoom photo when the crop is narrower than the floor. Unscored frames are never cropped. | `crop_min_px: 400`; `0` disables the small-crop rule. Applies with `crop_from_native`. |
| Damaged video frames | Partially decoded grey frames return zero scores, empty boxes and `artifact: true` without inference. | Scorer filter enabled by default; `TAPO_SCORER_ARTIFACT_FILTER=0` disables it. |
| Duplicate follow-ups | A delivered motion photo whose person score reached the threshold now counts toward the person cooldown, preventing a redundant SD/recording follow-up. | Normal cooldown behavior; no new flag. |
| Scoring retries | A timed-out, HTTP 400 or HTTP 500 scoring request is retried once. The worst-case wait can be about twice the configured timeout. | Review `scorer.timeout` against your latency budget. |

Unscored deliveries are captioned `(unverified)` and recorded in the audit. Delayed
sampler captions identify the frame time, and late-frame log entries make scheduling
delays visible. These changes are not all disabled by default.

## Features that need configuration

The outage outbox, address/URL candidate lists, watch and ignore rules, vehicle and
loitering alerts, early-read tuning, background hub clips, shadow targets, scorer
authentication and ensemble mode need configuration. Upgrading does not select a
new detector or enable an ensemble.

Use the supported `observe` modes to inspect detection-rule decisions before
enforcing them. A scorer URL list provides an independent fallback only if its
endpoints really use independent backends; two addresses forwarding to one process
do not provide that redundancy.

Box labeling and missed-frame collection improve review coverage. Automatically
generated teacher labels are not independent ground truth, and a disagreement-only
sample does not measure the false-alert rate of all production frames. Review random
controls and report model availability as well as scores. See
[operations](operations.md) and [measurements](measurements.md).

## Upgrade sequence

1. Back up configuration and persistent state; record the currently deployed package
   fingerprint and rollback target. Keep secrets outside the repository.
2. Validate the proposed configuration with `tapo-monitor check`. Use
   `tapo-monitor selfcheck` for host prerequisites and configured control credentials;
   neither command proves that Telegram or RTSP authentication works.
3. Update the scorer service first, then the monitoring daemons. Deploy the complete
   package so its decoder, configuration and client code remain consistent.
4. Check service health and a real alert path, including score, frame readability,
   caption and confirmed delivery. Compare recorder health, errors and alert volume
   against the previous deployment.
5. Enable additional options one at a time and keep model experiments in shadow mode
   until your manually reviewed data support a decision.

Scorer authentication is optional: with it configured, `/score` and `/metrics`
require a bearer token while `/health` stays open. Configure every client before
enabling it. Choose CPU, OpenVINO or CUDA settings for the actual runtime and hardware;
the package does not provision a GPU or guarantee its availability.

See [configuration](configuration.md), [operations](operations.md) and
[releasing](releasing.md) for detailed settings, deployment and publication steps.
