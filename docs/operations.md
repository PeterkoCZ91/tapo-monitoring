# Operations: deployment, health and calibration

Prepare and validate the private YAML using the [configuration reference](configuration.md)
before following this runbook. Firmware/API/media symptoms are indexed in
[troubleshooting](troubleshooting.md).

One `tapo_monitor` checkout can run more than one process in production. Keep the roles
separate when deploying or debugging.

## Process roles

| Role | Typical unit/process | Purpose | Health check |
| --- | --- | --- | --- |
| Tapo monitor daemon | `tapo-monitor run /path/cameras.yaml` | Poll Tapo events, control PTZ/day-night/rain behaviour, grab frames, decide whether to alert, send Telegram. | `journalctl -u tapo-monitor -n 100 --no-pager` |
| YOLO scorer service | `python -m tapo_monitor.scorer_service --model ... --port ...` | Stateless HTTP JPEG scorer. Does not poll cameras or send Telegram. | `curl -s http://127.0.0.1:8766/health` |
| Local recorder (optional) | external process | Continuously records RTSP; tapo-monitor can use it as a last-resort snapshot fallback and, with `snapshot_source: recording`, as the event-aligned follow-up frame source. | newest file age under `RECORDING_ROOT` |

The important boundary: **the scorer is shared, the alert pipelines are not.** The scorer
owns only model inference behind `/score`; it has no camera sessions and never notifies.
Any number of monitor daemons — or other HTTP clients you build — can POST frames to one
scorer, but each caller keeps its own thresholds, cooldowns and notification policy. The
only shared contract is HTTP:

```text
POST /score  JPEG bytes -> {"person": float, "animal": float, "classes": {...}}
GET /health              -> {"ok": true}
GET /metrics             -> aggregate request, inference and latency counters
```

The monitor gates person alerts on `person` only. `animal` is returned for audit and
calibration, but an animal score can never pass a person-alert threshold. `/metrics`
contains aggregate counts and durations only: it does not retain JPEGs, URLs, camera names
or client addresses.

## Monitor instances

One daemon instance is configured by one `cameras.yaml` and can manage multiple cameras.
Use another instance only when cameras live on a different host/network or you want
isolated state. Runtime state owned by an instance: event watermarks, per-camera alert
cooldowns, camera-down watchdog, pending SD follow-up queue, sampler groups, weather and
day/night control decisions.

Network health transitions survive daemon restarts in
`$XDG_STATE_HOME/tapo-monitor/health.json` (default
`~/.local/state/tapo-monitor/health.json`). Override the location with
`TAPO_HEALTH_STATE_FILE`. The file contains no credentials and is atomically replaced
with mode `0600` only when durable health state changes; high-frequency detection state
remains in memory.

Telegram delivery is part of the alert transition, not a fire-and-forget side effect.
Failed outage and recovery messages remain pending until Telegram confirms delivery; a
failed SD follow-up stays queued, and a failed sampler send leaves its group open. A failed
live send does not arm the cooldown: it is handed to the SD follow-up when enabled, or
reported as unsent so an enabled sampler can retry the event window.

Inspect the persisted observations without contacting or authenticating to a camera:

```bash
tapo-monitor status
```

The table shows the current observed online/offline interval, cumulative observed
availability, the last completed outage and the number of offline-to-online transitions.
Pass an explicit health JSON path when the daemon uses a custom
`TAPO_HEALTH_STATE_FILE` location.

For deeper diagnosis, the opt-in Camera Digital Twin separates network reachability from
API, event polling, RTSP media and storage health, and compares live camera state with the
daemon's intended control plan. The Shadow Detection Auditor keeps a media-free SQLite
ledger for comparing firmware events with independent local observations. Configuration,
state paths, CLI commands, privacy boundaries and rollout guidance are in
[`observability.md`](observability.md).

## Deploy the scorer

1. Install the package with scorer dependencies (`pip install tapo-monitor[scorer]`) in
   the scorer venv and put the ONNX model on local disk.
2. Adjust `WorkingDirectory`/`ExecStart` in `systemd/tapo-scorer@.service` to that
   checkout and venv, then create `/etc/tapo-monitor/scorer.env`:

```env
TAPO_SCORER_MODEL=/opt/tapo-monitor/models/yolox_m.onnx
TAPO_SCORER_PORT=8766
TAPO_SCORER_INPUT_SIZE=640
```

3. Install and start the unit:

```bash
sudo cp systemd/tapo-scorer@.service /etc/systemd/system/tapo-scorer@.service
sudo systemctl daemon-reload
sudo systemctl enable --now tapo-scorer@tapo
curl -s http://127.0.0.1:8766/health   # -> {"ok": true}
curl -s http://127.0.0.1:8766/metrics  # request/inference/latency counters
```

Point each camera's `scorer.url` at the service:

```yaml
scorer:
  url: http://SCORER_HOST:8766/score
  threshold: 0.40
  timeout: 10
```

## Deploy the monitor

```bash
tapo-monitor check /etc/tapo-monitor/cameras.yaml   # validate before restart
sudo systemctl restart tapo-monitor
journalctl -u tapo-monitor -f
```

## Snapshot sources

The monitor chooses frames in this order:

1. Live RTSP snapshot via `ffmpeg`.
2. Event-time follow-up when `sd_snapshot` / `sd_motion` is enabled and the event deserves
   a second chance. By default this downloads the segment from the camera **SD card**; with
   `snapshot_source: recording` it instead reads the local recorder tree (`RECORDING_ROOT`)
   for the event window — full stream1 resolution even when detection runs on stream2, and
   it picks the *sharpest* above-threshold frame (ffmpeg `blurdetect`) rather than the first.
   `recording` reuses the SD follow-up queue, so it requires `sd_snapshot: true`, and it
   falls back to the SD/live path when no matching segment exists or `RECORDING_ROOT` is unset.
3. Optional local-recorder fallback, only in the late fallback path after SD produced no
   usable frames.

The local recorder tree feeds both the conservative late fallback (step 3) and, when
selected, the event-aligned `snapshot_source: recording` (step 2). Enable it only on hosts
that continuously record RTSP:

```env
RECORDING_ROOT=/srv/recordings
RECORDING_MAX_AGE=300
```

It expects `<RECORDING_ROOT>/<camera-host>/<YYYY-MM-DD>/<HH>/*.mkv|*.mp4|*.ts`. The late
fallback extracts one frame near the end of the newest segment and ignores segments older
than `RECORDING_MAX_AGE` seconds; the event-aligned `recording` source instead extracts
candidates across the event window from the matching segment. Keep retention under each
camera host bounded.

## Audit and threshold calibration

The daemon emits structured `audit ...` lines for every alertable event, scorer decision,
SD/sampler follow-up and Telegram send. Summarize a day of them with:

```bash
journalctl -u tapo-monitor --since "24 hours ago" --no-pager | tapo-monitor audit-log -
```

The report is per camera:

- `detections`: firmware events the monitor considered alertable
- `telegram_ok` / `telegram_failed`: what actually reached Telegram
- `dropped_below_threshold`: frames the scorer rejected
- sent/drop score ranges: whether the threshold is cutting near real people

Run the loop for 24–48 hours after changing scorer model, thresholds or camera placement,
then compare the audit output with the actual Telegram photos:

- many real people below threshold → lower that camera's `scorer.threshold`;
- parked cars/noise above threshold → raise that camera's threshold;
- any `scorer_unavailable` count → fix scorer availability before tuning (threshold data
  is noisy while frames pass through unfiltered);
- `telegram_failed` → a delivery problem, not a detection problem;
- many `snapshot_failed`/deferred → RTSP or host load is weak; SD follow-up should be
  absorbing the important ones.

For rotating night cameras, parked cars should show up as motion/PIR events followed by
low scorer scores and no Telegram send. If they produce sends, inspect the frame before
touching thresholds.

## Debugging checklist

1. Decide which process owns the symptom: monitor, scorer or recorder.
2. Check scorer health before tuning thresholds.
3. Check the audit summary before changing `scorer.threshold`.
4. If an alert is missing, check whether it was dropped below threshold, deferred to SD,
   blocked by cooldown or failed at Telegram.

## Rollback

- Monitor: restore the previous package/files and `sudo systemctl restart tapo-monitor`.
- Scorer: restore the previous venv/model, or set `scorer.url` empty to fall back to
  raw/Groq gating.
