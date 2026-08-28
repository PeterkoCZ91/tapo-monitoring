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
GET /metrics             -> aggregate request, candidate, failure and latency counters
```

The monitor gates person alerts on `person` only. `animal` is returned for audit and
calibration, but an animal score can never pass a person-alert threshold. `/metrics`
contains aggregate counts and durations only: it does not retain JPEGs, URLs, camera names
or client addresses.

The live `/metrics` counters include `score_successes`, `person_candidates`,
`animal_candidates`, `malformed_responses`, sanitised `failure_reasons` and recent-window
`request_seconds_p50`/`p95` plus `score_seconds_p50`/`p95` in addition to the request,
inference and latency fields. `sources` contains the same aggregate counters grouped by a
16-hex pseudonymous source ID; it never contains camera names or URLs. When the durable
metrics journal is configured, cumulative counters survive a service restart. Percentiles
are intentionally recent-window measurements, bounded to keep memory constant. They
describe only the shared scorer: caller retry/circuit-breaker/fallback, stream-freeze and
notification counters remain caller-side. Run
`tools/check_scorer_rollout.sh http://SCORER_HOST:8766` after deployment; it fails when the
endpoint is healthy but still running the older metrics schema.

## Monitor instances

One daemon instance is configured by one `cameras.yaml` and can manage multiple cameras.
Use another instance only when cameras live on a different host/network or you want
isolated state. Runtime state owned by an instance: event watermarks, per-camera alert
cooldowns, camera-down watchdog, pending SD follow-up queue, sampler groups, weather and
day/night control decisions.
Event-API health is tracked separately from network reachability. When `getEvents` fails,
the daemon leaves the watermark unchanged, records the exception in the audit stream and
uses `alerts.event_failure_threshold` / `alerts.event_restart_threshold` for warning and
optional one-time recovery. A successful poll clears the episode and emits a recovery
notice when a warning was delivered.


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
TAPO_SCORER_METRICS_FILE=/opt/tapo-monitor/data/scorer.jsonl
TAPO_SCORER_METRICS_PERSIST_SECONDS=60
TAPO_SCORER_METRICS_RETENTION_DAYS=7
TAPO_SCORER_METRICS_RETENTION_FILES=8
```

The four `TAPO_SCORER_METRICS_*` values are read from this file by the process itself and
never appear in `ExecStart`: an undefined `${VAR}` there expands to nothing, and the
service would exit before the model loads — under `Restart=always` that is an invisible
crash loop caused by an observability setting. Unset, blank and unparseable values all
fall back to the built-in defaults.

3. Install and start the unit. **On an existing host, write `scorer.env` before copying
   the unit**, and note that `install` overwrites it — copy the example only when
   provisioning, and hand-edit an env file that already carries local paths:

```bash
sudo install -d -o tapo -g tapo /opt/tapo-monitor/data
sudo install -m 0640 systemd/tapo-scorer.env.example /etc/tapo-monitor/scorer.env
# Edit scorer.env if the service user, model path or retention differs.
sudo cp systemd/tapo-scorer@.service /etc/systemd/system/tapo-scorer@.service
sudo systemctl daemon-reload
sudo systemctl enable --now tapo-scorer@tapo
curl -s http://127.0.0.1:8766/health   # -> {"ok": true}
curl -s http://127.0.0.1:8766/metrics  # current aggregate counters; no image data
tools/check_scorer_rollout.sh http://127.0.0.1:8766
```

When `TAPO_SCORER_METRICS_FILE` is set, the scorer also writes one aggregate JSON line
after the configured persistence interval. The small `.state` sidecar restores cumulative
counters after a restart. The current JSONL file rotates once its **oldest record** is
older than the retention window and keeps eight rotated files; it contains timestamps,
counters, latency totals and sanitised failure reasons only — never JPEGs, URLs, camera
names or client addresses. The age deliberately comes from the first record rather than
the file mtime, which an every-minute append keeps permanently fresh.

Point each camera's `scorer.url` at the service:

```yaml
scorer:
  url: http://SCORER_HOST:8766/score
  threshold: 0.40
  timeout: 10
```

## Deploy the monitor

Deploy the **whole package**, never a subset of changed files: modules are versioned
together and a partial copy fails at import, which `Restart=always` then retries forever.

```bash
tapo-monitor version                                # fingerprint of the tree to be copied
# ... copy the full package to the host ...
tapo-monitor selfcheck /etc/tapo-monitor/cameras.yaml   # imports, config, credentials, ffmpeg
sudo systemctl restart tapo-monitor
tools/check_monitor_rollout.sh <FINGERPRINT>        # on the host, after the restart
journalctl -u tapo-monitor -f
```

`version` prints the release plus a short digest over the deployed module set, so a host
can answer "which code am I running" without being a git checkout — and a half-copied
package reports a different fingerprint than the tree it came from. `selfcheck` imports
every module, loads the config, asserts that the credential env vars named by that config
are actually set, and looks for `ffmpeg`; it prints env var *names*, never values.
`tools/check_monitor_rollout.sh` re-checks the unit state (an `auto-restart` sub-state is
the crash loop `is-active` hides), the fingerprint, `selfcheck`, and the journal since the
restart. It reports rather than asserts liveness: the daemon logs per decision, not per
poll, so a quiet camera is not a failure.

Both units carry `OnFailure=pi-failure-notify@%n.service` with `StartLimitBurst=5` per 300
seconds, so a crash loop stops retrying and sends one Telegram message instead of failing
silently forever. `pi_notify.sh` must be at `/usr/local/bin/pi_notify.sh`; it reads the
Telegram secrets from `/etc/tapo-monitor/secrets.env`, and a host that keeps them
elsewhere sets `TAPO_ENV_FILE` in `/etc/tapo-monitor/notify.env`.

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

## Inspecting alert frames

Threshold tuning is easier when you can see the exact frame behind a score. Two opt-in aids
capture frames for review without changing alert behaviour.

**Archive what was sent.** Set `TAPO_SENT_LOG_DIR` and every photo delivered to Telegram is
also copied there as a timestamped JPEG beside an `index.jsonl` line: timestamp, filename,
caption and delivery flag, plus the camera name and the scorer's `person`/`animal`
confidences when they are known. The camera name is what lets a host running two cameras
tell from the archive which one fired. Files older than `TAPO_SENT_LOG_RETENTION_DAYS`
(default 2) are pruned on each write, and the index is rotated on the same window so it
cannot outlive the frames it points at. It is inert when the variable is unset and never
raises into the send path — a full disk degrades to "no archive", never a lost alert.

A `crop_to_subject` camera sends the zoom to Telegram but archives the **uncropped** scene:
a cropped empty yard tells you nothing about a false positive.

```bash
export TAPO_SENT_LOG_DIR=~/tapo-monitor/sent-log
export TAPO_SENT_LOG_RETENTION_DAYS=2   # optional, default 2
```

**Archive what was suppressed.** With motion corroboration on (`scorer.motion_send_threshold`),
borderline non-PIR motion is *held* rather than sent. The sent log can't show those, so set
`TAPO_REVIEW_LOG_DIR` to also archive every held frame (filename carries camera, verdict and
person score; `index.jsonl` records the rest). This is the ground truth for confirming a hold
suppressed an animal/empty scene rather than a person. Pruned by `TAPO_REVIEW_LOG_RETENTION_DAYS`
(default 7); inert when unset; never raises into the alert path.

```bash
export TAPO_REVIEW_LOG_DIR=~/tapo-monitor/review-log
export TAPO_REVIEW_LOG_RETENTION_DAYS=7   # optional, default 7
```

**Get the suppressed frames delivered daily.** An archive nobody opens is not review — set
`TAPO_REVIEW_DIGEST_TIME` (local `HH:MM`) and once a day at that time the daemon sends a
Telegram summary of the review log's last 24 hours (per-camera counts with the top person
score) followed by the few highest-scoring suppressed frames, capped by
`TAPO_REVIEW_DIGEST_MAX_PHOTOS` (default 4). A quiet day still gets its one-line digest, so
silence always means "nothing suppressed", never "the digest broke". Digest photos are
deliberately kept **out** of the sent log — that archive stays the record of delivered
alerts. A failed send retries on the next tick; nothing here can raise into the alert path.

```bash
export TAPO_REVIEW_DIGEST_TIME=20:45      # off when unset
export TAPO_REVIEW_DIGEST_MAX_PHOTOS=4    # optional, default 4
```

**It is also the only message that says the fleet is alive.** Every other Telegram message
the daemon sends is a transition — camera lost, camera back, tick stalled, drift found — so
with nothing else, "everything works" is expressed as silence, and silence is
indistinguishable from a host that lost power or a Telegram token that stopped working. The
digest therefore carries a fleet block: which cameras are reachable, the daemon's own tick,
the shared scoring service (asked once a day, because when it dies alerts stop at *every*
site at once), the local recorder's newest file, the day's alert counts from the sent log,
and any self-heal a camera is refusing.

Two rules keep it honest. It only claims `Fleet OK` for what it actually checked — a host
with no recorder gets no recorder line rather than a reassuring one, and a camera whose
state is not yet known is named as unchecked rather than counted either way. And any failed
check takes the headline away entirely (`🟠 Fleet degraded — …`), because a heartbeat that
says OK while a camera is down is worse than no heartbeat: it turns a silence you might
have questioned into a confirmation you will trust.

```text
📋 Review digest: 3 suppressed frame(s) in the last 24h
yard: 2 (max p0.61)
shadow scan 2026-05-04: 192 of 192 segments, 1389 frames, 4 matched, 1 candidate(s)

💚 Fleet OK — front, yard reachable
   scorer 0 failed / 4180 req, p95 0.73s
   recorder newest file 47s old
   alerts 24h: 62 sent (yard 41, front 21), 1 undelivered
```

Note what this still cannot do: a heartbeat the host sends itself can never report that the
host is dead. Noticing the *absence* of a daily message is a job for a human or an external
dead-man's switch, not for this daemon.

**Audit what the cameras never reported.** Cameras only prove the alerts they raised; a
person the camera never flagged leaves no trace. When a host runs a 24/7 recorder
(`RECORDING_ROOT`), the nightly shadow scan re-reads yesterday's segments with no camera
involvement, scores motion candidates through the same scoring service, records shadow
observations in the event ledger (enable `observability.ledger`), and files
miss-candidate frames into the review log under the `shadow` verdict — so they arrive
with the daily digest. Budget, pacing and the event-match window are flags; the run
summary lands in `.shadow-scan.json` beside the review log and the digest quotes it, so
a missing nightly run is visible, not silent.

```bash
tapo-monitor shadow-scan cameras.yaml            # yesterday, default budget/pacing
tapo-monitor shadow-scan cameras.yaml --date 2026-08-12 --budget 800 --rate 2.0
tapo-monitor shadow-scan cameras.yaml --extract-budget 3600   # cap the decode phase
```

`--extract-budget` bounds the ffmpeg decode phase (default 18000 s) and `--budget` the
frames scored. Both are split as an **even share per camera**: cameras are processed one
after another, so a single run-wide counter left whoever is last in `cameras.yaml` with
only the leftovers. Unspent share rolls forward, so a share is a floor and not a cap. A run
that hits the ceiling counts the skipped segments per camera and sets `extract_exhausted`
in the summary — a trimmed run never looks like a complete one.

The scene pass decodes keyframes only (`-skip_frame nokey`). Scene changes live between
keyframes and a 15-minute 4K HEVC segment holds ~18000 frames against ~300 keyframes, so
full decode cost 4x more without finding more. Measured through the extraction function
itself on a 2-core 2.8 GHz x86 host with the daemon running, one segment costs 48 s when
the view is quiet and 64-75 s in daylight — extraction also pays for the mid-segment seek
and competes with the live pipeline, so bare ffmpeg time understates it. That is where the
18000 s default comes from: two cameras x 96 segments x the worst case, plus room for a
slower day. Sizing a host of your own? Time one segment first:

```bash
time ffmpeg -hide_banner -skip_frame nokey -i <segment>.mkv \
  -vf "scale=1280:-2,select='gt(scene,0.04)',showinfo" -vsync vfr -an -frames:v 8 -f null -
```

Schedule it with a systemd timer in the small hours, niced, with the same environment
file as the daemon:

```ini
[Service]
Type=oneshot
Nice=15
IOSchedulingClass=idle
EnvironmentFile=%h/tapo/tapo-camera.env
ExecStart=%h/tapo-env/bin/tapo-monitor shadow-scan %h/tapo-monitor/cameras.yaml

[Timer]
OnCalendar=*-*-* 03:00
Persistent=true
```

**Score a frame on demand.** The daemon only scores frames behind camera events, and
`night_only` cameras only at night, so there is otherwise no way to see how the scorer reads
a scene right now. `scene_probe` grabs a current RTSP frame from named cameras and scores it
through the same service — without sending anything to Telegram:

```bash
python -m tapo_monitor.scene_probe cameras.yaml front yard
# front   person=0.26  animal=0.06  tile2=0.28  top=[horse:0.06]  box=[...]
```

It prints the full-frame `person`/`animal` scores plus the best-tile score (`--tiles`,
default 2, mirrors what tiled inference would see) and saves each frame with its scores in
the filename under `--archive-dir` (default `~/tapo-monitor/probe-log`; `--no-archive`
prints only). It reuses the daemon's snapshot and scorer helpers and does not touch a
running daemon, so it is safe to run against production cameras.

## Debugging checklist

1. Decide which process owns the symptom: monitor, scorer or recorder.
2. Check how far back the journal actually reaches before concluding anything from it
   (see below). Silence in the journal is not evidence that the daemon was idle.
3. Check scorer health before tuning thresholds.
4. Check the audit summary before changing `scorer.threshold`.
5. If an alert is missing, check whether it was dropped below threshold, deferred to SD,
   blocked by cooldown or failed at Telegram.

### Make sure the journal survives

`journalctl` is the only record of what the monitor did, so confirm it is written to disk
rather than to RAM:

```bash
journalctl --header | grep -m1 'File path'   # /run/... means volatile
journalctl --disk-usage
journalctl -o short-iso | head -1            # oldest entry actually retained
```

A `File path` under `/run/log/journal` means journald is writing to RAM. It will not
switch on its own: journald decides at start-up, so creating `/var/log/journal` on a
running system leaves an empty directory and changes nothing. The runtime journal is
capped at a fraction of `/run`, which on a small single-board host is a few megabytes —
hours of history, all of it lost on reboot. That is the wrong trade-off here, because the
failures worth investigating (a camera that stopped delivering, a daemon that died
overnight) are usually noticed a day or more later.

Check for a vendor drop-in before writing your own — Raspberry Pi OS ships
`/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf` with `Storage=volatile`,
which is the real reason the journal is in RAM on those hosts:

```bash
ls /usr/lib/systemd/journald.conf.d/ /etc/systemd/journald.conf.d/ 2>/dev/null
```

systemd merges drop-ins by filename **across all directories**, so `/etc` does not
automatically win: a file named `10-persistent.conf` sorts before the vendor's `40-` file
and is silently overridden. Vendors take the low numbers, so give the administrator's
drop-in a high one:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\nSystemMaxFileSize=20M\nSystemKeepFree=1G\n' \
  | sudo tee /etc/systemd/journald.conf.d/99-persistent.conf
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo systemctl restart systemd-journald
sudo journalctl --flush        # migrates the runtime journal into /var
```

Verify with `journalctl --header` rather than assuming — that is the check that catches a
drop-in losing the merge.

Cap `SystemMaxUse` deliberately on flash storage. A monitor host writes on the order of
10-15 MB of journal per day, so 200M is roughly two weeks of history at modest wear.

The flush preserves history but not always all of it: restarting journald rotates the
active file, and if the runtime journal is already near its cap, the archived and the new
file cannot both fit, so the oldest segment is vacuumed. Compare
`journalctl -o short-iso | head -1` before and after to see what the switch actually cost.

### A recorder that "fails" on a schedule

A recorder rotated by a timer is stopped with `SIGTERM`, and a shell wrapper that traps it
exits `143`. systemd reads that as `Failed with result 'exit-code'`, so the unit enters a
failed state on every rotation while recording is in fact healthy. Declare the exit
status instead of ignoring the noise: otherwise `systemctl --failed` is permanently dirty,
any fleet-wide "is anything failed" check answers yes around the clock, and an `OnFailure=`
hook attached to that unit would page on every rotation:

```ini
# /etc/systemd/system/<recorder>.service.d/10-clean-stop.conf
[Service]
SuccessExitStatus=143
```

## Rollback

- Monitor: restore the previous package/files and `sudo systemctl restart tapo-monitor`.
- Scorer: restore the previous venv/model, or set `scorer.url` empty to fall back to
  raw/Groq gating.
