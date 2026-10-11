# Upgrading to 0.9.0

This guide covers the changes since 0.8.1. For the full feature list, see the
[changelog](../CHANGELOG.md) and [capabilities](capabilities.md).

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
