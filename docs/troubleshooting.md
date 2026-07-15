# Troubleshooting

Start by identifying the failed layer. “The camera is online” only proves network
reachability; API authentication, event polling and RTSP can fail independently.

## First five minutes

```bash
tapo-monitor check cameras.yaml
tapo-monitor status
tapo-monitor twin-status
journalctl -u tapo-monitor -n 150 --no-pager
```

Then classify the symptom:

| Symptom | First layer to inspect |
| --- | --- |
| Ping/network outage alert | cable, power, route, VLAN or remote tunnel |
| Network online but no client | camera API credentials, third-party access, lockout/backoff |
| Client online but no events | `getEvents`, camera detection toggles, watermark/log output |
| Events present but no photo | RTSP account, stream limit, keyframe timeout |
| Live photo empty | event timing; enable event-aligned follow-up/sampler |
| SD follow-up empty | freshness guard, cloud password, download window |
| Scorer accepts/rejects incorrectly | model input, tiles, threshold and audit report |
| Alert logged but not received | Telegram delivery result and retry path |

If Digital Twin is disabled, `twin-status` will have no state. Enable it only after the
core event path is stable.

## Camera unreachable

The daemon pings before creating an authenticated client. If network reachability is down:

- verify camera power and physical link;
- verify the monitoring host's route/VLAN/tunnel independently;
- avoid debugging passwords until the device is reachable;
- use `tapo-monitor status` to inspect observed uptime, outage duration and reconnects.

An unplugged cable should not generate repeated login attempts. The unauthenticated ping
layer exists specifically to keep physical outages separate from auth failures.

## Invalid authentication data

Affected firmware can reject the first login after reconnect and can lock out the source
address after repeated failures.

Checklist:

1. enable third-party compatibility in the Tapo app if required;
2. verify the dedicated camera account, not only the TP-Link cloud account;
3. verify which password the operation needs: local camera, RTSP or cloud-account;
4. stop other integrations that may be creating concurrent sessions;
5. wait for the lockout/backoff window instead of restarting repeatedly.

The daemon retries conservatively and then applies exponential backoff. Do not reduce the
control interval to work around auth errors; that usually makes the condition worse.

## Camera API works, but events are missing

The production daemon uses `getEvents`. Check logs for the event audit line and decoded
`events_1` fields. On tested firmware, `event_type` may be empty even when the bitmask is
valid.

Known signals are documented in [`events1-bitmask.md`](events1-bitmask.md). Unknown bits
remain unknown until repeatable ground truth exists.

Also verify:

- person and motion detection are enabled in the camera;
- the camera clock/timezone is sufficiently aligned;
- `detection.sources` includes `getevents`;
- a `night_only` camera is currently inside its alert window;
- an event is not being correctly suppressed by cooldown.

ONVIF and `motion` are not currently independent daemon event sources. Selecting them
without `getevents` produces no production event polling.

## SmartTrack appears configured but does not move

On validated firmware, `setSmartTrackConfig` silently clears the auto-track master switch.
The safe sequence is:

1. motion/person/vehicle policy;
2. preset and SmartTrack categories;
3. `setAutoTrackTarget` last;
4. read back and verify.

This sequence is centralized in `tracking.py` and `daemon.apply_plan`. Avoid adding camera
setter calls between SmartTrack and the final auto-track assertion.

## Sensitivity does not match the configured number

Do not pass numeric strings to pytapo sensitivity setters. For example,
`sensitivity="60"` can be treated as a label and remapped to a different digital value.
The daemon converts configured sensitivity to an integer before applying it.

When diagnosing drift, compare the Digital Twin's normalized actual value with the desired
plan rather than trusting a setter return alone.

## RTSP capture fails

Checklist:

- confirm the camera exposes RTSP and third-party access is enabled;
- verify `rtsp_user_env` and `rtsp_password_env` values independently;
- check the selected stream and port;
- raise `rtsp_timeout` for slow hosts or long keyframe intervals;
- check whether another application has consumed the camera's stream limit;
- avoid simultaneously running multiple camera integrations during diagnosis.

API health can remain `ok` while RTSP is `down`. That is expected layered-health behavior,
not a contradiction.

## The live snapshot is empty

This is often timing rather than detection failure. The camera opens an event at motion
start, while the subject may enter the useful view seconds later.

Options:

- enable `sd_snapshot` for a camera-confirmed event-time follow-up;
- use `snapshot_source: recording` with a continuous local recorder;
- enable the event-window sampler;
- use a local scorer so empty follow-ups do not become notifications.

The alert pipeline deliberately treats “event exists” and “subject is visible in this
frame” as separate facts.

## SD-card download returns no frames

Several pytapo/firmware behaviors look like random failures:

### Fresh recording guard

The downloader can produce an empty file when the requested window ends less than roughly
60 seconds before the current time. No useful exception is guaranteed. The daemon delays
the job until the complete event window is old enough; changing that calculation to use
only event start time reintroduces the failure.

### Event loop conflict

A media download from the long-lived event client can fail with an event-loop/auth warning.
The daemon starts SD download in a fresh subprocess and pre-warms the required user ID.
Keep media download isolated from the main poller's client.

### Transfer window size

The tested camera can stall with pytapo's larger default transfer window. The SD helper
uses a bounded window size and owns a temporary job directory so killed downloads do not
fill `/tmp`.

Also verify the camera has an SD card, the requested recording exists and
`cloud_password_env` resolves to the credential required by that model.

## Local recorder follow-up finds no segment

Verify:

```text
<RECORDING_ROOT>/<camera-host>/<YYYY-MM-DD>/<HH>/*.mkv|*.mp4|*.ts
```

Check system time, segment timestamps, directory permissions and `RECORDING_ROOT`. The
event-aligned reader searches the matching event window; the newest-frame fallback also
honors `RECORDING_MAX_AGE`.

`snapshot_source: recording` requires `sd_snapshot: true` because it reuses the deferred
follow-up queue.

## Scorer unavailable or inaccurate

Health check:

```bash
curl -s http://127.0.0.1:8766/health
```

If the scorer is unreachable, the monitor fails open. This can increase false positives
but should not hide camera-confirmed people.

For distant subjects:

- raise `scorer.tiles` to score a grid as well as the complete frame;
- use a larger model on a stronger shared scorer host;
- enable `crop_to_subject` only after boxes are reliable;
- inspect structured audit data before changing `threshold`.

Summarize audit logs:

```bash
journalctl -u tapo-monitor --since "24 hours ago" --no-pager \
  | tapo-monitor audit-log -
```

Tune using real accepted and rejected examples. A low send rate alone does not prove the
threshold is wrong; the camera may be generating false events.

## Telegram delivery failures

The daemon records the delivery result. A failed live send does not activate cooldown and
can be handed to an enabled SD/sampler retry path. Outage and recovery notifications remain
pending until delivery succeeds.

Check:

- token/chat environment variables are available to the service process;
- outbound HTTPS/DNS works from the monitor host;
- systemd uses the intended environment file;
- logs distinguish a rejected frame from a failed Telegram request.

Do not infer delivery from “alert decision accepted”; look for the explicit send result.

## Pan guard repeatedly recalls the camera

The soft pan limit derives allowed bounds from ONVIF preset positions. Verify presets
represent the intended left/right range and increase `margin` slightly if tracking near a
boundary oscillates. An ONVIF error invalidates the cached client and retries later; it
does not stop the main loop.

## Digital Twin shows unknown values

Unknown is not automatically a failure. It can mean:

- the getter does not exist on this pytapo/model combination;
- firmware returned an empty response;
- a safe probe failed in isolation;
- the health layer has not yet been exercised (for example, no RTSP capture event yet).

Digital Twin intentionally avoids raw module enumeration because it has been unsafe on
tested firmware. Do not replace unknown values with assumptions or automatic setters.

## Shadow report interpretation

- `matched` means both sources observed the same event type within the window;
- `camera_only` may be a firmware false positive or a shadow watcher miss;
- `shadow_only` may be a camera miss, clock skew or incomplete watcher coverage.

Treat the report as calibration evidence, not labelled ground truth. The independent
always-watching shadow worker is still a roadmap item.

## Collecting a safe bug report

Include:

- camera model and firmware version;
- relevant feature flags and sanitized configuration shape;
- exact error type and nearby log decisions;
- whether network/API/events/RTSP/storage layers were healthy;
- whether the problem reproduces with other integrations stopped.

Remove camera addresses, usernames, tokens, coordinates, device IDs, face IDs and local
filesystem paths before posting. Use the repository's private security channel for a
credential or security exposure, as described in [SECURITY.md](../SECURITY.md).
