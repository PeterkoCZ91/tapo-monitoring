# Upgrading to 0.12.0

Install the new package on the monitor hosts as usual. Most changes need no action, but
the first one can change what an existing camera does, so check it before deploying.

- **`tracking.day_preset` no longer defaults to `"2"`.** A camera that never named a
  preset no longer recalls preset 2: unset, nothing is recalled. If a camera relied on
  the implicit value (to return to a view by day, to park in `storm_park` rain, or, with
  `role: static`, to stay parked around the clock), set it explicitly:

  ```yaml
  tracking:
    day_preset: "2"
  ```

  `weather.storm_park` or `weather.strategy: disable_tracking` without a `day_preset`
  is not an error but logs a warning at load, because rain then only stops tracking.
- **`scorer.unscored_at_night`** (`send` or `hold`, default `send`) is new. The default
  keeps today's behaviour: a frame the scorer could not score still goes out unverified.
  With `hold`, nothing unverified is sent during the camera's night: a card camera hands
  the event to its follow-up, other frames are kept in the review log without a score.
  Any other value is a `ConfigError`.
- **`outbox.max_bytes`** (default 64 MiB, integer >= 1) now caps the stored alert bytes
  next to `max_entries`. Both evict the oldest non-person entries first. At start-up the
  daemon applies the current limits to entries left by an older run, so lowering a limit
  trims an existing outbox on the next restart. A non-integer or smaller value is a
  `ConfigError`.
- **Alert video clips are opt-in.** `clip_video` and its options change nothing unless
  enabled; see [Alert video clips](alert-clips.md). Enabling `clip_video` without
  `clip_url` on a camera that lacks the `getevents` detection source is a `ConfigError`,
  and `clip_url` must be an `http://` or `https://` URL.
- **Stricter validation** (from the unreleased changelog): non-boolean values for
  `night_only`, `telegram_alerts`, `pan_limit.enabled`/`tilt`, `light_trigger.enabled`,
  the `reliability` and `observability` switches and `alerts.event_restart_enabled` are
  now a `ConfigError` instead of being read as true. See the [changelog](../CHANGELOG.md)
  for the integer and mapping checks on `loop`, `alerts` and `observability`.

# Upgrading to 0.11.0

Install the new package on the monitor hosts. **pytapo must be 3.4.28 or newer**: a plain
`pip install -U tapo-monitor` pulls it in, but a release deploy into an existing venv does
not, so upgrade pytapo in that venv too (`tools/deploy_release.sh --sync-deps` does it
from `uv.lock`).

- **Cameras on the TPAP login connect again.** A camera that switched to `encryption: 4`
  failed every login with `Invalid authentication data` on older pytapo, even with the
  right password. See [troubleshooting](troubleshooting.md#invalid-authentication-data).
- **No login loop after an auth failure.** A failure during the control pass now waits out
  the connect backoff instead of logging in again every minute.
- **Opt-in predictive pan stop** (`pan_limit.mode: stop`): stops native tracking before a
  preset boundary. Experimental — observed positions still crossed the limit in tests;
  nothing changes unless configured. See
  [configuration](configuration.md#predictive-stop-instead-of-recall).
- `events_1` bit 8 is reported in `unknown_bits` instead of `linecrossing`.

## Upgrading to 0.10.0

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
