# Deployment and health runbook

Use this as the operational checklist when more than one runtime is involved. The process
map is in [`runtime-topology.md`](runtime-topology.md).

## Services

| Service | Owns | Does not own | Health check |
| --- | --- | --- | --- |
| `tapo-monitor` | Tapo camera sessions, watermarks, PTZ control, SD/sampler queues, Telegram sends | YOLO model lifecycle, A12 events | `journalctl -u tapo-monitor -n 100 --no-pager` |
| `tapo-scorer` | HTTP `/score` and `/health`, model process | Camera polling, cooldowns, Telegram | `curl -s http://127.0.0.1:8766/health` |
| `a12-system-v2` | A12 pipeline and thresholds | Tapo camera state | A12 container logs + scorer health from inside container |
| local recorder | continuous RTSP archive | alert decisions | newest file age under `RECORDING_ROOT` |

## Deploy scorer

1. Install the package with scorer dependencies in the scorer venv.
2. Put the ONNX model on local disk.
3. Adjust `WorkingDirectory` and `ExecStart` in `systemd/tapo-scorer@.service` so they
   point at that scorer checkout and venv.
4. Create `/etc/tapo-monitor/scorer.env`:

```env
TAPO_SCORER_MODEL=/opt/tapo-monitor/models/yolox_m.onnx
TAPO_SCORER_PORT=8766
TAPO_SCORER_INPUT_SIZE=640
```

5. Install and start the unit:

```bash
sudo cp systemd/tapo-scorer@.service /etc/systemd/system/tapo-scorer@.service
sudo systemctl daemon-reload
sudo systemctl enable --now tapo-scorer@tapo
curl -s http://127.0.0.1:8766/health
```

Expected health response:

```json
{"ok": true}
```

## Deploy Tapo monitor

1. Update the repo or copied package on the Pi.
2. Validate config before restart:

```bash
tapo-monitor check /etc/tapo-monitor/cameras.yaml
```

3. Restart and watch logs:

```bash
sudo systemctl restart tapo-monitor
journalctl -u tapo-monitor -f
```

4. After the audit build is deployed, summarize the last day:

```bash
journalctl -u tapo-monitor --since "24 hours ago" --no-pager | tapo-monitor audit-log -
```

## Deploy A12 scorer integration

A12 uses the same scorer only over HTTP. In the A12 environment/config:

```env
YOLO_BACKEND=http
YOLO_SCORER_URL=http://127.0.0.1:8766/score
YOLO_CLASSES=person,dog
```

Use `127.0.0.1` only when the container runs with host networking. Otherwise use an
address reachable from inside the container. Keep A12 local YOLO files present as fallback.

## Recorder fallback

The recorder fallback is optional. Enable it only on hosts that continuously record RTSP:

```env
RECORDING_ROOT=/recordings
RECORDING_MAX_AGE=300
```

Expected layout:

```text
/recordings/<camera-host>/<YYYY-MM-DD>/<HH>/*.mkv
```

Keep retention under each camera host bounded. The fallback searches that camera subtree
for the newest segment when it is needed, so very large archives should be rotated or
mounted outside the fallback root.

The normal live alert path does not use recorder fallback, so it cannot pre-empt the
SD-card event-time follow-up. If SD produced no usable frames and the fallback path would
otherwise retry a live grab, tapo-monitor may extract one frame from the newest fresh
segment. Files older than `RECORDING_MAX_AGE` seconds are ignored. This fallback is not
event-aligned; SD follow-up remains the primary event-time path.

## Calibration loop

Run for 24-48 hours after changing scorer model, thresholds or camera placement. Then
compare audit output with real Telegram photos:

- many real people below threshold -> lower that camera's `scorer.threshold`;
- parked cars/noise above threshold -> raise that camera's threshold;
- scorer outages -> fix `tapo-scorer` before tuning;
- A12 misses or spams -> tune A12 thresholds separately from Tapo.

## 48-hour post-deploy review

Run this after the audit build has been live for roughly 48 hours. Use exact times when
possible so day/night behaviour is comparable across reviews.

### Collect logs

```bash
journalctl -u tapo-monitor --since "48 hours ago" --no-pager > /tmp/tapo-monitor-48h.log
tapo-monitor audit-log /tmp/tapo-monitor-48h.log
```

Also check scorer health from each caller host/container:

```bash
curl -s http://127.0.0.1:8766/health
```

For A12 in host-network Docker, run the same health check from inside the container. If
A12 is not host-networked, use the scorer address configured in `YOLO_SCORER_URL`.

### Tapo monitor questions

- Per camera, compare `detections` against `telegram_ok`. A large gap is fine only when
  `dropped_below_threshold` explains it and the dropped photos are real noise.
- Inspect scores near the threshold. Real people below threshold mean lower that camera
  only; parked cars/noise above threshold mean raise that camera only.
- Check `scorer_unavailable`. Any non-zero count means threshold data is noisy; fix scorer
  availability before tuning.
- Check `telegram_failed`. Any non-zero count is a delivery problem, not a detection
  threshold problem.
- Check `snapshot_failed` and `deferred`. Too many live failures mean RTSP or host load is
  still weak; SD follow-up should be absorbing the important ones.
- For rotating night cameras, search for parked-car cases. They should show as Tapo
  motion/PIR events followed by low score drops or no send.

### Recorder fallback questions

Only review this if `RECORDING_ROOT` is enabled.

```bash
grep -n "using recorder fallback" /tmp/tapo-monitor-48h.log
```

Each fallback send must be checked manually because the recorder frame is not event-aligned.
If fallbacks are frequent, fix live RTSP/SD first. Keep recorder retention bounded under
`RECORDING_ROOT`; very large archives make fallback search slower.

### A12 questions

- Compare A12 notification volume before/after the shared scorer change.
- Confirm A12 still filters blank frames below its own thresholds.
- Tune A12 thresholds separately from Tapo; shared scorer output does not mean shared alert
  rules.
- Check A12 logs for scorer request failures or fallback to local YOLO.

### Decision record

Record the final decision with the date, exact window and thresholds used:

```text
48h review: YYYY-MM-DD HH:MM to YYYY-MM-DD HH:MM
Camera <name>: detections=<n>, telegram_ok=<n>, drops=<n>, threshold=<x.xx>, decision=<keep/lower/raise>
A12: threshold decision=<keep/lower/raise>, notes=<...>
Scorer availability: <ok/issues>
Recorder fallback: <disabled/ok/issues>
```

## Rollback

- Tapo monitor: restore previous package/files and `sudo systemctl restart tapo-monitor`.
- Scorer: restore previous venv/model or set Tapo `scorer.url` empty to return to raw/Groq gating.
- A12: set `YOLO_BACKEND=local` and restart the container.
