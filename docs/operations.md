# Operations: deployment, health and calibration

One `tapo_monitor` checkout can run more than one process in production. Keep the roles
separate when deploying or debugging.

## Process roles

| Role | Typical unit/process | Purpose | Health check |
| --- | --- | --- | --- |
| Tapo monitor daemon | `tapo-monitor run /path/cameras.yaml` | Poll Tapo events, control PTZ/day-night/rain behaviour, grab frames, decide whether to alert, send Telegram. | `journalctl -u tapo-monitor -n 100 --no-pager` |
| YOLO scorer service | `python -m tapo_monitor.scorer_service --model ... --port ...` | Stateless HTTP JPEG scorer. Does not poll cameras or send Telegram. | `curl -s http://127.0.0.1:8766/health` |
| Local recorder (optional) | external process | Continuously records RTSP; tapo-monitor can use the newest segment as a last-resort snapshot fallback. | newest file age under `RECORDING_ROOT` |

The important boundary: **the scorer is shared, the alert pipelines are not.** The scorer
owns only model inference behind `/score`; it has no camera sessions and never notifies.
Any number of monitor daemons — or other HTTP clients you build — can POST frames to one
scorer, but each caller keeps its own thresholds, cooldowns and notification policy. The
only shared contract is HTTP:

```text
POST /score  JPEG bytes -> {"person": float, "animal": float, "classes": {...}}
```

## Monitor instances

One daemon instance is configured by one `cameras.yaml` and can manage multiple cameras.
Use another instance only when cameras live on a different host/network or you want
isolated state. Runtime state owned by an instance: event watermarks, per-camera alert
cooldowns, camera-down watchdog, pending SD follow-up queue, sampler groups, weather and
day/night control decisions.

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
2. SD-card follow-up around the camera event time, when `sd_snapshot` / `sd_motion` is
   enabled and the event deserves a second chance.
3. Optional local-recorder fallback, only in the late fallback path after SD produced no
   usable frames.

The recorder fallback is deliberately conservative. Enable it only on hosts that
continuously record RTSP:

```env
RECORDING_ROOT=/recordings
RECORDING_MAX_AGE=300
```

It expects `<RECORDING_ROOT>/<camera-host>/<YYYY-MM-DD>/<HH>/*.mkv|*.mp4|*.ts`, extracts
one frame near the end of the newest segment, and ignores segments older than
`RECORDING_MAX_AGE` seconds. It is not event-aligned — SD follow-up remains the primary
event-time path. Keep retention under each camera host bounded.

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
