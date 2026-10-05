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
One outage is one notice: while the camera fails its ping (a network outage is open,
before or after the 🔴 alert), the event-API watchdog stands down — no warning, no
restart, no "restored" line — because `getEvents` cannot succeed without the camera.
The event clock restarts when the camera is reachable again, so an event endpoint that
is still broken after the return warns a full `event_failure_threshold` later. A warning
already delivered before the network dropped stays open and gets its recovery notice.


Network health transitions survive daemon restarts in
`$XDG_STATE_HOME/tapo-monitor/health.json` (default
`~/.local/state/tapo-monitor/health.json`). Override the location with
`TAPO_HEALTH_STATE_FILE`. The file contains no credentials and is atomically replaced
with mode `0600` only when durable health state changes; high-frequency detection state
remains in memory.

Alert work in flight survives a restart too: hub delivery retries, pending SD follow-ups
and the alert cooldowns are written to `runtime.json` beside the health file whenever they
change, and restored on start (logged as `runtime state restored: ...`). A restart in the
middle of a passage therefore neither drops a queued alert nor sends the same person
twice. A queued hub retry whose frame file has meanwhile disappeared is dropped with a
warning, and a file older than an hour is discarded whole — its entries' own ten-minute
TTLs have long expired.

A stop signal (`SIGTERM` from `systemctl stop/restart`, or Ctrl-C) no longer interrupts a
camera call halfway: inside a tick it only asks the loop to exit once the tick is done;
between ticks it exits at once, and a second signal always exits at once. On the way out
the daemon closes held hub sessions, writes `runtime.json` one final time and gives the
audit-ledger queue up to 5 s to reach SQLite — lines still queued after that are counted
in a warning rather than silently lost. An SD or recorder follow-up still being read in the
background is not waited for (`stopping with N SD follow-up(s) being read`): its entry is in
`runtime.json` and is read again after the start, and the start removes the `sdjob_<pid>_*`
temp dirs a stopped daemon's reads left behind.

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

## Provision the scorer

Every camera's alerts depend on this one host, and it is the only one that runs no
daemon — so it has no digest, no fingerprint check and, until it is given one, no way to
speak. `tools/provision_scorer.sh` builds it from this checkout. Run it on the scorer
host; it is idempotent and safe to re-run on the live service:

```bash
sudo tools/provision_scorer.sh --user tapo --dry-run   # diffs only, touches nothing
sudo tools/provision_scorer.sh --user tapo
curl -s http://127.0.0.1:8766/health   # -> {"ok": true}
curl -s http://127.0.0.1:8766/metrics  # current aggregate counters; no image data
```

It renders `systemd/tapo-scorer.service.in` into `/etc/systemd/system/tapo-scorer.service`
with the host's own user, working directory and interpreter, writes
`/etc/tapo-monitor/scorer.env` on first run, installs `pi_notify.sh` together with the
`pi-failure-notify@` handler the unit's `OnFailure=` names, restarts only when a file
that affects the service actually changed, and ends by running
`tools/check_scorer_rollout.sh` against the port it just configured.

The unit is rendered rather than copied because systemd expands `${VAR}` in an `ExecStart`
*argument* but never in the executable itself: the interpreter and working directory can
only be literal text, so a copied unit needs the same hand-edit on every host — which is
how the running unit drifted away from the repository's copy in the first place.

Defaults, all overridable: `--home ~USER/tapo-scorer`, `--python <home>/env/bin/python`,
`--model <home>/yolox_m.onnx`, `--port 8766`, `--input-size 640`,
`--metrics-file <home>/metrics/scorer.jsonl`, `--unit tapo-scorer.service`. On a fresh
host, `--bootstrap` creates the venv and installs `".[scorer]"` from the checkout; the
ONNX weights are the one thing the script will not fetch, so put them at `--model` first.

`scorer.env` is created once and never overwritten — it holds the host's own paths, and
re-provisioning must not silently undo a local retention change. Edit it by hand and
re-run the script, which restarts the service because the env file changed.

```env
TAPO_SCORER_MODEL=/srv/tapo-scorer/yolox_m.onnx
TAPO_SCORER_PORT=8766
TAPO_SCORER_INPUT_SIZE=640
TAPO_SCORER_METRICS_FILE=/srv/tapo-scorer/metrics/scorer.jsonl
TAPO_SCORER_METRICS_PERSIST_SECONDS=60
TAPO_SCORER_METRICS_RETENTION_DAYS=7
TAPO_SCORER_METRICS_RETENTION_FILES=8
TAPO_SCORER_METRICS_MAX_JOURNAL_BYTES=33554432
```

The `TAPO_SCORER_METRICS_*` values are read from this file by the process itself and
never appear in `ExecStart`: an undefined `${VAR}` there expands to nothing, and the
service would exit before the model loads — under `Restart=always` that is an invisible
crash loop caused by an observability setting. Unset, blank and unparseable values all
fall back to the built-in defaults.

### Shared token (optional)

The scoring service has no authentication by default, which is fine while it listens only
for monitors on a trusted LAN. Before it moves to a machine other people or devices can
reach, turn on a shared token. It is opt-in: with no token configured nothing changes.

With a token set on the service, `POST /score` and `GET /metrics` need
`Authorization: Bearer <token>` and otherwise answer `401 {"error": "unauthorized"}` with no
detail (the token is compared in constant time, and a refused request is not counted in the
metrics). `GET /health` stays open so failover checks and systemd probes keep working; it
only says `ok` and the detector family.

1. Generate a token on the scorer host and keep it in a file only the service user can read:

   ```bash
   sudo install -m 0600 -o tapo -g tapo /dev/null /etc/tapo-monitor/scorer.token
   openssl rand -hex 32 | sudo tee /etc/tapo-monitor/scorer.token >/dev/null
   sudo stat -c '%a %U' /etc/tapo-monitor/scorer.token   # check: 600 and the service user
   ```

2. Point the service at it in `/etc/tapo-monitor/scorer.env` and re-run
   `tools/provision_scorer.sh` (the env file changed, so it restarts the service):

   ```env
   TAPO_SCORER_TOKEN_FILE=/etc/tapo-monitor/scorer.token
   ```

   `TAPO_SCORER_TOKEN=<value>` also works but a file is preferred and wins when both are
   set. A file that is missing, empty or readable by group or others stops the service at
   start (exit 2) instead of silently serving without protection; the journal line names
   the setting, never the token. With the token on, the start-up line reads `auth=token`.

3. On every monitor host put the same token in the daemon's env file (for example a
   `chmod 600` `EnvironmentFile`) under a name of your choice and name that variable in
   the config (the config holds the variable's name, never the token):

   ```yaml
   scorer:
     url: http://198.51.100.20:8766/score
     token_env: TAPO_SCORER_TOKEN
   ```

   `TAPO_SCORER_TOKEN_FILE`-style indirection works here too: if `<token_env>_FILE`
   (`TAPO_SCORER_TOKEN_FILE` above) names a readable file, its contents are used instead of
   the variable's value. Every URL in a failover list uses the same token.

   The same variable (`TAPO_SCORER_TOKEN` or `..._FILE`) is read by
   `tools/fleet_status.sh` and `tools/check_scorer_rollout.sh` (sent through a curl config
   on a pipe, never on the command line), and `tapo-monitor label --scorer-url` and
   `tools/replay_zones.py score` read the variable named by `--scorer-token-env` /
   `--token-env` (default `TAPO_SCORER_TOKEN`). The daily digest's `/metrics` read uses
   `scorer.token_env` too.

The token is never logged and never written to audit lines, `health.json`, runtime state,
the digest or `tapo-monitor report`. A client that is refused with 401 logs one clear
`rejected the token` error per endpoint and keeps running as it does for any scorer outage
(frames pass unscored); a 401 is a configuration error, so it does **not** count towards
failover and never switches to another candidate. If the digest shows the scorer as down
right after enabling a token, check for that line first.

To rotate: write the new token to the service's file, restart `tapo-scorer`, then update
the monitor hosts' env and restart them. Until a monitor has the new token it logs the 401
error and sends frames unscored, so rotate a host at a time in a quiet period, or accept a
short gap. To roll back, remove the `TAPO_SCORER_TOKEN*` lines from `scorer.env` and
restart the service; clients that still send a header are unaffected.

The token only keeps casual or accidental callers out: it travels in clear text, so over
anything but a private network (Tailscale, a LAN) put the scorer behind TLS as well.

### Swapping the detector model

The service runs one detector family, chosen by `TAPO_SCORER_ARCH` in `scorer.env`
(`tapo_monitor/detectors.py`). Whatever the family, `/score` answers with the same fields,
with boxes in pixels of the original frame, so callers need no change.

| Variable | Values | Meaning |
|---|---|---|
| `TAPO_SCORER_ARCH` | `yolox` (default), `detr` | Decoder family. Any other value stops the service at start with `scorer config error`. |
| `TAPO_SCORER_INPUT_SIZE` | positive integer | Model input side. The unit passes it as `--input-size`; unset, `yolox` uses 416 and `detr` 640. |
| `TAPO_SCORER_DETR_PERSON_ID` | `0` (COCO80, default), `1` (COCO91) | Label space of the logits; also fixes the animal and vehicle classes. |
| `TAPO_SCORER_DETR_ACTIVATION` | `softmax` (default), `sigmoid` | `softmax` for heads with a trailing "no object" column, `sigmoid` for focal-loss heads. |
| `TAPO_SCORER_DETR_NORMALIZE` | `imagenet` (default), `none` | Input scaling: ImageNet mean/std, or plain 0-1. |

`detr` covers NMS-free exports with two outputs, `logits` `[1,Q,C]` and `boxes` `[1,Q,4]`
(normalised centre-x/centre-y/width/height), such as RF-DETR, D-FINE and RT-DETR. The
settings come from how the model was trained and exported, so read them off the export
rather than guessing: a wrong activation, label space or normalisation does not fail, it
just scores badly. An export with a different output layout, or extra inputs such as a
target-size tensor, is not supported. A DETR export whose outputs have the wrong shape makes
every `/score` answer 500 `scoring failed` (callers then fall back as for any scorer
failure); empty or NaN outputs score as "nothing found". `/health` and `/metrics` report
the family as `arch` (never the model path).

Verified settings for RF-DETR (official `model.export()`, ONNX opset 17, outputs `dets`
`[1,300,4]` and `labels` `[1,300,91]`; the decoder picks the outputs by shape, so the names
do not matter). Person scores through `/score` matched the reference implementation's
`predict()` on 100 labelled frames, max difference 0.03 on the Intel iGPU and under 0.001 on CPU:

| Model | `TAPO_SCORER_INPUT_SIZE` | Common settings | p50 / p95 per frame on an Intel iGPU |
|---|---|---|---|
| RF-DETR Medium | `576` | `TAPO_SCORER_ARCH=detr`, `TAPO_SCORER_DETR_PERSON_ID=1`, `TAPO_SCORER_DETR_ACTIVATION=sigmoid`, `TAPO_SCORER_DETR_NORMALIZE=imagenet` | 0.29 / 0.30 s |
| RF-DETR Large | `704` | same | 0.45 / 0.46 s |

The input size is the model's training resolution (read it from the export's `input`
shape); the 640 default is wrong for both. The `openvino-gpu` provider needs the
`onnxruntime-openvino` wheel in the scorer's environment: the plain `onnxruntime` wheel
has no OpenVINO provider and the service falls back to CPU with a logged warning.

Check a new model **before** it replaces the running one: start a second instance on
another port with its own env, leave the production unit alone, and send both the same
saved frames.

```bash
TAPO_SCORER_ARCH=detr TAPO_SCORER_DETR_ACTIVATION=sigmoid TAPO_SCORER_DETR_PERSON_ID=1 \
  TAPO_SCORER_INPUT_SIZE=640 \
  python -m tapo_monitor.scorer_service --model /srv/tapo-scorer/candidate.onnx --port 8767
curl -s http://127.0.0.1:8767/health        # {"ok": true, "arch": "detr"}
for f in frames/*.jpg; do
  curl -s --data-binary @"$f" http://127.0.0.1:8766/score > "old_$(basename "$f").json"
  curl -s --data-binary @"$f" http://127.0.0.1:8767/score > "new_$(basename "$f").json"
done
```

Use a set that includes the cases that matter: labelled people (day and night), the known
false-alarm frames (static objects, rain, IR grain) and empty scenes. Compare per frame
`person`, `person_boxes`, `vehicle` and `animal`, and the latency in the candidate's
`/metrics` (`score_seconds_p50`/`p95`) on the same hardware and provider
(`TAPO_SCORER_PROVIDER`). Scores of a different model are not calibrated like YOLOX's, so
the camera thresholds need a re-check against the labelled frames. Switch only when
recall on the labelled people holds and the false-alarm frames do not get worse. To switch,
change `TAPO_SCORER_MODEL` and the variables above in `scorer.env`, re-run
`tools/provision_scorer.sh` (it restarts the service because the env file changed), check
`/health` for the new `arch`, and keep the old weights and env lines for rollback.

### Shadow run of a second detector (optional)

To compare a candidate on live traffic without touching the cameras, let the production
scorer forward a copy of every frame to a second scorer service (the candidate, started as
above on its own port) and log both scores. Monitor hosts keep calling only the primary;
their replies are byte-for-byte what they were. Off unless both `URL` and `LOG` are set in
`scorer.env` (with `TAPO_SCORER_ENSEMBLE` set, `URL` alone is enough and nothing is logged):

```bash
TAPO_SCORER_SHADOW_URL=http://CANDIDATE_HOST:8767/score
TAPO_SCORER_SHADOW_LOG=/srv/tapo-scorer/shadow/shadow.jsonl
TAPO_SCORER_SHADOW_TOKEN_FILE=/etc/tapo-monitor/shadow.token   # 0600, only if the candidate has a token
TAPO_SCORER_SHADOW_DISAGREE_DIR=/srv/tapo-scorer/shadow/disagree   # optional: keep disagreeing frames
TAPO_SCORER_SHADOW_DISAGREE_MAX=300                                # newest N files kept (default)
TAPO_SCORER_SHADOW_QUEUE=8                                         # frames waiting for the single shadow worker; a full queue drops (default 8)
TAPO_SCORER_SHADOW_TIMEOUT=8                                       # seconds per shadow call (default)
```

`TAPO_SCORER_SHADOW_TOKEN` works like `TAPO_SCORER_TOKEN` (the file wins; a file that is not
0600 stops the service at start). Behaviour:

- The frame goes into a queue (`TAPO_SCORER_SHADOW_QUEUE`, default 8) after the reply has
  been written. A full queue drops the
  frame and counts it; a slow, dead or unauthorized shadow only delays the background
  thread. It never changes or delays an answer to a monitor host.
- One JSON line per frame in the log: `ts`, `source_id`, `primary_person`,
  `primary_vehicle`, `shadow_person`, `primary_seconds`, `shadow_seconds` and `error`
  (`null`, `timeout`, `connection`, `http_<code>`, `bad_response`), plus `primary_boxes` and
  `shadow_boxes` when the reply carried person boxes (normalized `[x1, y1, x2, y2, score]`,
  best first, at most 10, 3 decimals) and `frame` (first 16 hex characters of the SHA-256 of
  the frame body, to match a row to a saved frame; only written when the log is on, absent in
  older logs, which `tools/shadow_report.py` still reads). The URL and token are
  never written; repeated failures log one warning a minute. Images are not in the log.
- With `DISAGREE_DIR`, a frame is also saved when one model scores a person at 0.45 or more
  and the other below 0.25, as `<UTC time>_p<primary>_s<shadow>.jpg` (directory 0700, files
  0600, oldest deleted beyond the limit). These are the frames to judge by hand.
- `/metrics` gains `shadow` with `sent`, `failed`, `dropped` and `disagree` counters.

A second, independent candidate can be tested at the same time (opt-in, off unless both
`URL` and `LOG` are set; the first shadow and its log format are untouched):

```bash
TAPO_SCORER_SHADOW2_URL=http://CANDIDATE2_HOST:8768/score
TAPO_SCORER_SHADOW2_LOG=/srv/tapo-scorer/shadow/shadow2.jsonl
TAPO_SCORER_SHADOW2_NAME=yolox960          # written to every row as shadow_name (default shadow2)
# optional, same meaning as the first shadow: _TOKEN_FILE / _TOKEN, _QUEUE, _TIMEOUT,
# _DISAGREE_DIR, _DISAGREE_MAX
```

It has its own queue and worker thread, so a slow or dead second target drops its own frames
and never delays a reply or the first shadow. Its log rows have the same fields as above plus
`shadow_name`; `/metrics` gains `shadow2` (same four counters) only while it is on. It never
takes part in `TAPO_SCORER_ENSEMBLE`. Both shadows add their own load to the scorer host
and each candidate.

The shadow doubles the traffic and inference load of the candidate, so keep it on a host
that can take it, and turn it off (remove the lines, restart) when the comparison is done.

To evaluate after a few days: count rows with `shadow_person` set and compare the two
columns at the operating thresholds in use (for example how often primary is at or above the
camera threshold and the shadow below it, and the reverse); compare `primary_seconds` to
`shadow_seconds`; a high `failed` or `dropped` means the sample is biased towards quiet
moments. Then label the saved disagreement frames:

```bash
tapo-monitor label /srv/tapo-scorer/shadow/disagree
```

The share of those frames with a real person says which model is right where they differ.
The frames are camera images: keep the directory private and delete it afterwards.

`tools/shadow_report.py` does the arithmetic on the log (stdlib only, read-only):

```bash
python3 tools/shadow_report.py shadow.jsonl --since 2026-10-01 --shadow-threshold 0.45
python3 tools/shadow_report.py shadow.jsonl --labels ~/dataset/labels.jsonl \
    --disagree-dir /srv/tapo-scorer/shadow/disagree --lang cs --json
```

With two shadow logs, pass both; each is reported separately and the two shadows are
compared on the frames both scored (matched by the `frame` fingerprint):

```bash
python3 tools/shadow_report.py shadow.jsonl shadow2.jsonl --shadow-threshold 0.45 \
    --shadow2-threshold 0.35
```

`--json` then returns `first`, `second` and `compare`; `--labels` applies to the first log.

How to read it:

- Records and errors: rows with an `error` are excluded from every comparison below. Many
  errors or drops mean the sample misses busy moments.
- Median and p95 of `primary_seconds` and `shadow_seconds` (a shadow call includes the
  network round trip).
- The 2x2 matrix at primary >= 0.45 and shadow >= `--shadow-threshold` (both / only primary
  / only shadow / neither) and the share of disagreements. Thresholds differ between models,
  so look at the matrix at the shadow threshold you would really use.
- Disagreements by source (a short hash, no camera names), by UTC hour and by score band
  show whether they concentrate on one camera, on night frames or near the threshold
  (where a disagreement is cheap) rather than far from it.
- Since the log rows carry `primary_boxes` / `shadow_boxes` (person boxes as
  `[x1, y1, x2, y2, score]`, normalized to 0-1, at most 10 per frame; absent in older rows),
  the report also lists static box clusters per model (same centre cell of a 0.05 grid in
  at least 5 frames of one source, a hint for a fixed object that looks like a person) and
  says for disagreeing frames whether the two models' boxes overlap (IoU >= 0.3, "same
  object") or each sees something else.
- Pearson and Spearman correlation of the two scores: high Spearman with a low Pearson
  means the models rank frames alike but are calibrated differently, so a different
  threshold may fix it.
- With `--labels` (the `labels.jsonl` of `tapo-monitor label`, last line per sha256 wins,
  `unsure` skipped) the labeled disagreement frames are scored against these rules: primary
  alone, shadow alone at 0.45 / 0.30 / 0.194, and "shadow >= 0.45 OR (primary >= 0.45 AND
  shadow >= T)" for T from 0.08 to 0.20, each with its false alarms and misses. Frame scores
  come from the `p<primary>_s<shadow>` file name, matched by hash in `--disagree-dir`
  (without it, from the path stored in the label). The sample is small and biased, because
  only frames where the models differ strongly were saved, so use the counts to rank rules,
  not as error rates; confirm on more frames before changing a threshold.

### Recording audit estimate (optional)

The shadow-run frames are biased towards disagreements, so they cannot say how many persons
a detector misses overall. To estimate that, score a day of recorded frames with both models
and label a stratified sample. Build `scores.jsonl` with one object per frame:
`{"frame", "window", "ts", "stratum", "yolox", "rfdetr"}` where `stratum` is `cand` (either
model >= 0.25), `lowrand` (a random sample of the frames where both are below 0.25) or
`lowrest` (the other low frames, unlabeled). Label `cand` and `lowrand` frames with
`tapo-monitor label`, then:

```bash
python3 tools/recording_audit_estimate.py scores.jsonl --labels ~/dataset/labels.jsonl \
    --night-windows night1,night2 --seed 1 --lang cs --json
```

How it works and how to read it:

- Labels (`labels.jsonl`, last line per sha256 wins, `unsure` ignored) are matched to frames
  by the label's `path` or `file` field: equal to `frame`, or else equal after stripping
  directories when that file name is unique in the table, or else by a `frame` field of the
  label. Labels of a dataset built by `recording_audit.py dataset` are named
  `audit/<stratum>_y.._r.._<window>_<index>.jpg` while the table has
  `frame = <window>/<index>.jpg`; those match by `(window, index)` (regex
  `_([dn]\d\d)_(\d+)\.jpg$`), so tables and datasets from before the change still pair up.
  `dataset` now also writes `frame` into `index.jsonl`. Unmatched labels are counted and
  warned about.
- Each labeled frame stands for N/n frames of its stratum (the person rate per stratum is
  the labeled share; `lowrand` stands for `lowrand` plus `lowrest`, so it is also the rate
  used for `lowrest`). Labeled `lowrest` frames are not a random sample and are not used.
- Rules: `yolox>=0.45`, `rfdetr>=0.45`, `rfdetr>=0.30`, `rfdetr>=0.45 OR (yolox>=0.45 AND
  rfdetr>=T)` for T from 0.08 to 0.25, and a low threshold 0.12 applied to either model
  (`either`) or to both (`both`).
- Per rule, per 1000 frames: persons, caught, missed, false alarms (an alert on a
  non-person), and the miss rate, each with a 95 % interval from 1000 bootstrap resamples
  of the labeled frames in `cand` and `lowrand` separately (`--seed`, `--iterations`).
- The share of persons that no model scores >= 0.25 is the independent completeness check:
  it comes from the `lowrand` person rate alone, not from any rule.
- Visit level (`--visit-gap`, default 30 s): labeled persons are grouped into visits (same
  window, consecutive labeled person frames at most the gap apart). For each rule the report
  gives the visits with at least one alerting frame (caught), the rest (missed) and the
  caught share with a Wilson 95 % interval. This is not weighted by stratum: it counts the
  labeled persons only, so it says how many of the *found* visits a rule would have alerted
  on, not how many exist. Why it differs from the frame estimate: one person stands on many
  frames, and an alert needs only one of them, so a rule that misses most frames of a visit
  can still catch the visit; the per-frame miss rate therefore overstates missed visits. If
  the table is sparse in time, a larger gap merges visits; frames without numeric `ts` are
  skipped.
- Day and night are separate groups. A window counts as night when its name contains
  `night` or `noc`, or when listed in `--night-windows` (then every other window is day).
- The tool warns when a stratum has fewer than 30 labels, when `lowrand` has no person
  (the upper 95 % bound is about 3/n, which the bootstrap cannot show), and that the
  interval covers sampling noise only: it assumes `lowrand` was drawn at random, that
  `lowrest` resembles it and that labels are right. Label a random draw, not the frames the
  labeling queue finds most interesting, or the estimate is biased.

### Build a labeling set from two shadow targets

`tools/label_selection.py` joins `shadow.jsonl` and `shadow2.jsonl` on `frame`, keeps frames
whose image is still in a `disagree/` directory, skips frames already in `labels.jsonl`
(matched by image SHA-256) and writes a dataset for `tapo-monitor label`:

```bash
python3 tools/label_selection.py --shadow-log shadow.jsonl --shadow2-log shadow2.jsonl \
    --disagree-dir shadow/disagree --disagree-dir shadow2/disagree \
    --labels ~/tapo-dataset/labels.jsonl --out ./selection --limit 600 --dry-run
```

Drop `--dry-run` to write `OUT/disagree1/*.jpg` (`--batch N` names it `disagreeN`),
`OUT/index.jsonl` (`file`, `ts` in UTC epoch seconds from the file name, `person` = production
score, optional `boxes` as `[x1, y1, x2, y2, score, model]` with model `yolox640`, `rfdetr` or
`yolox960`; change with `--models`), `OUT/teacher.jsonl` (the score farthest from production,
for ordering by disagreement) and `OUT/selection-report.md`. Categories, in priority order:
`strong` (one model >= 0.45, another < 0.25), `threshold` (split at 0.45), `small` (best box
shorter than 0.10 of the frame), and random controls `control_person` / `control_empty`
(`--control` each). `--limit` caps the total and `--source-share` the share of one source.
`--image-dir DIR` (repeatable, e.g. the `sent-log` and `review-log` of a dataset) searches
DIR recursively for JPEGs, hashes them the same way and supplies images for frames missing
from `disagree/`, which is what gives `small` and the controls any images; `ts` comes from
the neighbouring `index.jsonl`. The same image in several places counts once, `disagree/`
wins. Log rows without `frame` (written only since the field was added) cannot be paired and
are counted as dropped in the report, which also lists selected frames by image source type.
Sources appear only as short hashes. Output holds camera images: keep it private.

### Provision a shadow scorer

The shadow target above is an ordinary `tapo_monitor.scorer_service` on another port or
machine, typically an LXC container. `tools/provision_shadow_scorer.sh` builds it from a
checkout in one command and is safe to re-run. It needs a Python environment with
`".[scorer]"` installed (numpy, onnxruntime) and the ONNX weights on disk; it fetches neither.
The unit `tapo-scorer-shadow-NAME.service` is rendered from
`systemd/tapo-scorer-shadow.service.in` (the same renderer approach as the primary unit), and
the detector settings (`TAPO_SCORER_ARCH`, `TAPO_SCORER_INPUT_SIZE`, `TAPO_SCORER_DETR_*`)
are written as `Environment=` lines in it, so several shadow scorers can share one machine.

```bash
# RF-DETR (NMS-free DETR head, sigmoid scores)
sudo tools/provision_shadow_scorer.sh --name rfdetr --model /srv/models/rfdetr.onnx \
    --port 8767 --arch detr --detr-activation sigmoid --user tapo \
    --python /srv/tapo-monitor/.venv/bin/python --token-file /etc/tapo-monitor/shadow.token

# YOLOX-m at a larger input, to see whether resolution recovers small or distant people
sudo tools/provision_shadow_scorer.sh --name yolox960 --model /srv/models/yolox_m.onnx \
    --port 8768 --arch yolox --input-size 960 --user tapo \
    --python /srv/tapo-monitor/.venv/bin/python

# any run: add --dry-run first. It prints the diff, or "changed=0" when nothing differs.
```

What it does: validates the arguments (name `[a-z0-9-]`, port 1-65535, model exists, token
file mode 0600 and owned by the service user; the token itself is never read, printed or put
on a command line, only its path goes into the unit), installs the unit only when it differs,
runs `daemon-reload`, `enable` and `restart` after a change, then polls
`http://127.0.0.1:PORT/health` for up to 90 s and succeeds only on HTTP status 200. On
failure it exits non-zero and prints the last 30 journal lines of the unit.
`--print-unit` renders the unit to stdout without root. Point the primary scorer at the new
service with `TAPO_SCORER_SHADOW_URL` as described above.

Limits to know before you start:

- The shadow runs on the CPU of the container. RF-DETR and YOLOX at 960 are several times
  slower than the production YOLOX-m at 640; check `/metrics` (`score_seconds_p95`) on the
  candidate. The primary queues frames (`TAPO_SCORER_SHADOW_QUEUE`) and drops what the
  shadow cannot keep up with, so a slow candidate shows up as a rising `dropped` counter, not
  as a slower primary.
- Give the container its own cores and memory; `onnxruntime` takes every core it sees by
  default and would otherwise compete with whatever else runs there.
- A shadow never changes the answer to a monitor host. Hosts talk only to the primary scorer;
  the candidate's reply is read by the primary for the log (and, only with
  `TAPO_SCORER_ENSEMBLE` set explicitly, for the combined score).
- Thresholds are not transferable between models: score the log against labels before trusting
  a candidate (see the evaluation steps below).

### Ensemble mode: one combined score (optional)

When the shadow log and the labeled disagreements show that a rule combining the two models
beats either alone, the scorer can apply that rule itself, so monitor hosts, their
thresholds and zones stay as they are and simply see a better `person`. On the data this was
built from, "shadow >= 0.45 OR (primary >= 0.45 AND shadow >= 0.12)" cut false alarms by about
46 % and missed persons by about 57 % against the primary alone (a small, biased sample: check
it on yours with `tools/shadow_report.py --labels` first). Needs the shadow settings above; the
`..._SHADOW_LOG` line may then be left out (nothing is logged):

```bash
TAPO_SCORER_ENSEMBLE=gate            # default: unset = off
TAPO_SCORER_ENSEMBLE_LOW=0.12        # shadow score the primary needs to be confirmed
TAPO_SCORER_ENSEMBLE_HOST=0.45       # person threshold of the monitor hosts (alias: _HIGH)
TAPO_SCORER_ENSEMBLE_TRUST=0.45      # shadow score trusted on its own; default = HOST
TAPO_SCORER_ENSEMBLE_BAND=0.10       # width of the band a trusted marginal score lands in
TAPO_SCORER_ENSEMBLE_TIMEOUT=2.0     # seconds the reply may wait for the shadow
```

For every `/score` the primary inference and a call to the shadow service (same image, same
`?tiles`) run in parallel, then `person` becomes:

- shadow >= HOST: the shadow score, with `box`, `person_boxes` and `person_scores` from the
  shadow reply;
- else TRUST <= shadow < HOST: `person = HOST + (shadow - TRUST) / (HOST - TRUST) * BAND`
  (linear into `[HOST, HOST + BAND)`), boxes from the shadow, its `person_scores` remapped
  the same way, `used: shadow_marginal`. The hosts compare `person` with their own threshold
  (0.45, hold band 0.45-0.55), so a second-model score of 0.30-0.45 has to be moved into
  that band to be accepted. With the default TRUST = HOST this step never applies and the
  behaviour is the one before it existed;
- else primary >= HOST and shadow >= LOW: the primary reply as it is;
- else `min(max(primary, shadow), HOST - 0.01)`, so an unconfirmed primary cannot cross the
  host threshold (entries of its `person_scores` list are capped the same way, so a client
  that rebuilds `person` from the list cannot revive a person the second model denied).

Choosing TRUST: in a recording audit the second detector at >= 0.30 caught all 19 of 19
visits at about 1.4 false positives per 1000 frames, while a 0.45 cut missed 3 of them
(a small sample: re-check on yours with `tools/recording_audit_estimate.py`). Then set
`TAPO_SCORER_ENSEMBLE_TRUST=0.30`. Raise it on one camera first and watch
`shadow_marginal` in `/metrics`.

`animal`, `vehicle`, `classes`, `w` and `h` always come from the primary. The reply gains
`ensemble: {mode, primary, shadow, used}` with `used` one of `shadow`, `shadow_marginal`,
`primary`, `neither` or `primary_only`. If the shadow fails, answers 401, sends garbage or does not answer within
the timeout, the primary reply goes out unchanged with `used: primary_only` (`shadow: null`);
the reply never waits longer than the timeout for it, and one warning a minute names the
error class only (never the URL or token). The shadow log and disagreement frames keep
working and reuse the same shadow call. `/metrics` gains `ensemble` with `requests`,
`shadow`, `shadow_marginal`, `primary`, `neither`, `primary_only`, `failed` and `timeouts`; a growing
`primary_only` means the combined score is silently not being applied.

Cost: every request now also depends on the second service. Expect about +0.3 s per request
when the shadow is the slower model (the reply takes the slower of the two, not their sum);
size the camera-side `scorer.timeout` for that plus the ensemble timeout. A dead shadow costs
up to `TAPO_SCORER_ENSEMBLE_TIMEOUT` per request until it is back. Enabling the feature
needs a restart of the scorer. To roll back, remove `TAPO_SCORER_ENSEMBLE` and restart; the
replies are then byte-for-byte the primary's again. A bad value (unknown mode, anything but
`0 < LOW < TRUST <= HOST <= 1` and `BAND >= 0`, no shadow URL) stops the service at start instead of running half-configured.
### Partially decoded frames (artifact filter)

A damaged HEVC decode sometimes leaves a flat mid-grey picture (luma about 128) with only a
few intra-coded blocks of real content, and a detector will "find" a person in those blocks.
The scorer therefore checks every frame first, on a 160x90 luma copy: at least 60 % of the
pixels within 4 of 128 and a mean luma of 120-136. Such a frame is answered without running
any inference: `person`, `animal` and `vehicle` 0, empty boxes and score lists, and
`"artifact": true` (clients ignore the unknown field). `/metrics` gains the counter
`artifact`. On 3922 kept frames this matched 8 known artifacts and nothing else; normal, night
IR, dark and foggy frames stay at a grey share of 0.12 or less. On by default; set
`TAPO_SCORER_ARTIFACT_FILTER=0` and restart the scorer to turn it off. Undecodable input is not
treated as an artifact.

### Estimating completeness from the continuous recordings

The kept frames cannot show what the pipeline never flagged. If a host records RTSP
continuously (`RECORDING_ROOT`), `tools/recording_audit.py` samples that stream without
bias and scores it with one or two scorer services:

```bash
ssh HOST "cd /srv/recordings/CAMERA && find . -type f -name '*.mkv' -printf '%P\t%s\n'" > listing.txt
python3 tools/recording_audit.py plan --listing listing.txt --out plan.json --files-from files.txt
rsync -a --bwlimit=20000 --files-from=files.txt HOST:/srv/recordings/CAMERA/ raw/
nice -n 19 python3 tools/recording_audit.py score --plan plan.json --raw raw --frames-dir frames \
    --out scores.jsonl --scorer http://127.0.0.1:8766/score --scorer http://CANDIDATE:8767/score \
    --token-file ~/.config/candidate-token
python3 tools/recording_audit.py dataset --scores scores.jsonl --frames-dir frames --out dataset
tapo-monitor label --lang cs dataset
```

`plan` draws a seeded sample, by default 6 day and 6 night windows of 10 minutes, each inside
one segment and on a different day, from the most recent complete days (the newest day is
still being written, the two oldest are about to be pruned). It prints the bytes to copy
before anything is transferred; nothing on the recorder host is changed. `score` cuts a frame
every `--every` seconds (default 5) and sends at most `--max-rate` frames per second
(default 2) to each scorer; the token is sent to non-loopback scorers only. Rows of
`scores.jsonl` carry a stratum: `cand` (any model at 0.25 or more), `lowrand` (a seeded
random 150 of the frames both models rate below 0.25) and `lowrest` (the other low frames).
`dataset` keeps the 400 highest candidates and the lowrand sample; the verdict shown in the
labeler is the stratum.

How to read the labels: the share of real persons among labeled candidates is each model's
precision on unfiltered video. The persons in the labeled lowrand sample, multiplied by
`lowrest / 150`, estimate what both models missed; the ratio to the persons found is the
completeness. The segments and frames are camera images: keep them private and delete them
afterwards.

### Moving the scorer to another machine

Because every camera shares one scorer, move it without a gap by listing both machines in
`scorer.url` (see [Scorer failover](configuration.md#scorer-failover-optional)):

1. Provision the new host with `tools/provision_scorer.sh` and check it with
   `tools/check_scorer_rollout.sh http://NEW_HOST:8766` while the old one still serves.
2. On each monitor host change `scorer.url` to a list, old scorer first, new one second:
   `["http://203.0.113.10:8766/score", "http://198.51.100.20:8766/score"]`, and deploy the
   config. Restart is needed to read it; nothing switches yet.
3. Stop the old scorer. After 3 failed requests each monitor logs `scorer switch` and
   continues on the new host; check `journalctl -u tapo-monitor | grep "scorer switch"` and
   the `scorer_index=1` audit line. `health.json` remembers the choice across restarts.
4. To make the move permanent, put the new URL first (or alone) and restart. Leave the old
   one listed as the standby instead if it will come back: the monitors return to the first
   entry within 5 minutes of its `/health` answering.

The sites do not have to be changed together: a monitor that still has a plain string URL
keeps using it. The daily digest reads `/metrics` from the active URL; `tools/fleet_status.sh` takes its own
`TAPO_FLEET_SCORER_URL`, so point that at the new host after the move.

### When the scorer itself fails

The unit gives up after five starts in five minutes instead of crash-looping in silence,
and `OnFailure=pi-failure-notify@%n.service` reports why. That needs credentials on a host
that has none of its own: create `/etc/tapo-monitor/notify.env`, mode 600, with
`TELEGRAM_TOKEN=` and `TELEGRAM_CHAT_ID=` (or `TAPO_ENV_FILE=` pointing at a file that
carries them, which is what the camera hosts do). The script never accepts a token as an
argument — argv is world-readable in `/proc` — and warns instead when the file is absent.

Test the chain without disturbing the scorer: install a throwaway unit whose `ExecStart`
is `/bin/false` and that carries the same `OnFailure=` line, start it, and read the
journal.

```bash
journalctl -u 'pi-failure-notify@*' -n 20 --no-pager
# "Triggering OnFailure= dependencies" then "Finished pi-failure-notify@<unit>.service.service"
# -> the handler ran and exited 0, which pi_notify.sh does only after Telegram answers "ok":true
```

Read the handler's name from the journal rather than constructing it: `%n` is the *full*
unit name, so the instance is `<unit>.service` and systemd appends its own `.service` on
top. Asking `systemctl show` about the single-suffix spelling is not a check — it reports
defaults for a unit that does not exist, and `Result=success` for something that never
ran looks exactly like success.

This covers the scorer dying loudly. A host that dies quietly — power, kernel, network —
is caught from outside by the mutual host watch, which polls this service's `/health` from
another machine; see "Watching the hosts themselves" below.

When `TAPO_SCORER_METRICS_FILE` is set, the scorer also writes one aggregate JSON line
after the configured persistence interval. The small `.state` sidecar restores cumulative
counters after a restart. The current JSONL file rotates once its **oldest record** is
older than the retention window — or, whichever comes first, once the file itself
outgrows `TAPO_SCORER_METRICS_MAX_JOURNAL_BYTES` (32 MiB by default), so a burst of fat
records cannot outgrow a small disk between two age checks — and keeps eight rotated
files; it contains timestamps, counters, latency totals and sanitised failure reasons
only — never JPEGs, URLs, camera names or client addresses. The age deliberately comes
from the first record rather than the file mtime, which an every-minute append keeps
permanently fresh. Rotation of either kind never touches the `.state` sidecar, so the
cumulative counters survive it.

Point each camera's `scorer.url` at the service:

```yaml
scorer:
  url: http://SCORER_HOST:8766/score
  threshold: 0.40
  timeout: 10
```

## Release deploys and rollback

Deploy the **whole package**, never a subset of changed files: modules are versioned
together and a partial copy fails at import, which `Restart=always` then retries forever.
`tools/deploy_release.sh` enforces that by construction — it ships `git archive` output,
so what lands on the host is always one complete, committed tree.

### Layout on the host

Each deploy is its own directory; the unit runs whatever `current` points at:

```text
~/tapo-monitor/
  cameras.yaml                          # host-owned config, shared by all releases
  current -> releases/20260901T101500Z-3f9c2d81a0b4/
  releases/
    20260901T101500Z-3f9c2d81a0b4/      # <UTC timestamp>-<package fingerprint>
      tapo_monitor/  tools/  systemd/ ...
      config-snapshot/                  # cameras.yaml + env file as this release saw them (0600)
```

The directory name carries the package fingerprint (the digest `tapo-monitor version`
prints), so "which code is that release" is answered by `ls`. `config-snapshot/` holds
the config and the unit's `EnvironmentFile` exactly as they were when the release passed
its selfcheck: `cameras.yaml` is shared across releases, so without the copy a rolled-back
release could not say what configuration it was validated against.

The unit must start from the release layout: `WorkingDirectory=%h/tapo-monitor/current`
and an **absolute venv interpreter** running `python -m tapo_monitor` (see
`systemd/tapo-monitor.service`). systemd activates no venv and searches no PATH, and the
`tapo-monitor` console script only exists where pip generated it — an extracted archive
has none. With `-m`, Python finds the package in the working directory, i.e. in whichever
release `current` points at, so neither deploy nor rollback ever edits the unit.

### Deploy

From the workstation, inside the repo checkout:

```bash
tools/deploy_release.sh <ssh-host>                       # ship HEAD
tools/deploy_release.sh <ssh-host> v0.4.0                # or any committed ref
tools/deploy_release.sh <ssh-host> --user                # host runs a systemd user unit
```

The script stages the ref with `git archive`, fingerprints the staged tree with its own
CLI, extracts it into `releases/<UTC-ts>-<fingerprint>/`, snapshots `cameras.yaml` plus
the unit's env file, and runs `selfcheck` **from inside the new release** under that env —
imports, config, credentials, ffmpeg, all checked before the release can take over. Only
then does it switch `current` (a temp symlink renamed over with `mv -T`: one atomic
rename, never an unlink/relink window), restart the unit (`--restart-cmd` for hosts where
the operator has no sudo), verify that `current` now reports the staged fingerprint, and
prune to the last 5 releases — never the one `current` points to. A failed selfcheck
aborts before the switch and leaves the release directory in place for inspection.

Follow up on the host with `~/tapo-monitor/current/tools/check_monitor_rollout.sh
<FINGERPRINT>`: it re-checks the unit state (an `auto-restart` sub-state is the crash
loop `is-active` hides), the fingerprint, `selfcheck`, and the journal since the restart.
It reports rather than asserts liveness: the daemon logs per decision, not per poll, so a
quiet camera is not a failure.

To rehearse the deploy path without a production host, point the script at any
ssh-reachable account whose `~/tapo-monitor` is expendable, seed a `cameras.yaml` and an
env file carrying the credential vars it names, and pass `--restart-cmd true
--env-file <that file>` — everything short of the systemd restart runs for real.

### Hosts with a user unit or a different venv

`deploy_release.sh` assumes a system unit and `~/tapo-env`. For a host that runs a systemd
*user* unit (`systemctl --user`, with lingering enabled) from another venv, pass `--user`
and the interpreter:

```bash
tools/deploy_release.sh <ssh-host> --user --python ~/other-venv/bin/python
tools/rollback_release.sh <ssh-host> <release-name> --user --python ~/other-venv/bin/python
# then, on the host:
~/tapo-monitor/current/tools/check_monitor_rollout.sh --user <FINGERPRINT>
```

`--user` sends **every** systemctl/journalctl call on the host to the user manager: the
`EnvironmentFile` lookup (so the snapshot and the `TAPO_EXPECTED_FINGERPRINT` update find
the user unit's env file), the `NRestarts` and `is-active` reads of the post-restart health
check, and in `check_monitor_rollout.sh` the unit state and the journal. It also makes the
default restart `systemctl --user restart <unit>`; an explicit `--restart-cmd` still wins.

Without `--user` the scripts query the system manager, which reports a unit it does not
have as `inactive`: the deploy's health check then rolls back a release that started fine,
and `check_monitor_rollout.sh` reports a false `unit: FAILED`. The ssh session must reach
the user manager (lingering on, `XDG_RUNTIME_DIR` set by the login session) — check once
with `ssh <ssh-host> systemctl --user is-active tapo-monitor`.

Things that still differ there:

- `selfcheck` runs over a non-interactive ssh whose `PATH` may not contain `ffmpeg`; the
  check fails and `current` is not switched. Install ffmpeg system-wide (a binary under
  `~/.local/bin` is not visible to that shell) or make the env file the script sources set
  a `PATH` that has it. Without ffmpeg a hub camera would drop every clip as `no_frame`.

### Rollback

```bash
tools/rollback_release.sh <ssh-host>                     # list releases, current marked
tools/rollback_release.sh <ssh-host> 20260901T101500Z-3f9c2d81a0b4
```

Rollback is re-pointing `current` at a release that already passed its deploy-time
selfcheck, restarting, and verifying the fingerprint — nothing is copied or rebuilt.
Note what it deliberately does *not* roll back: `cameras.yaml` and the env file are
host-owned and shared. If the bad deploy also changed them, diff against the target
release's `config-snapshot/` and restore by hand.

### Migrating a host from the rsync layout

An rsync-layout host keeps the package files (`tapo_monitor/`, `tools/`, ...) directly in
`~/tapo-monitor/` and a unit with `WorkingDirectory` pointing there. The release layout
lives in the same directory, so migration is additive:

1. Run `tools/deploy_release.sh <host>` once — it creates `releases/` and the `current`
   symlink beside the old files without touching them (`cameras.yaml` already being at
   `~/tapo-monitor/cameras.yaml` is exactly what the layout expects).
2. Update the unit to the template's `WorkingDirectory=.../tapo-monitor/current` and
   absolute-interpreter `ExecStart`, then `daemon-reload` and restart.
3. Once `check_monitor_rollout.sh` passes from `current`, delete the loose package
   directories from `~/tapo-monitor/` — they are now dead code that only invites
   confusion about what is running.

Until step 2 the running unit still uses the old files, so a failed first deploy changes
nothing; and `deploy_release.sh` refuses to run where `~/tapo-monitor/current` exists as
a real directory rather than a symlink.

Both units carry `OnFailure=pi-failure-notify@%n.service` with `StartLimitBurst=5` per 300
seconds, so a crash loop stops retrying and sends one Telegram message instead of failing
silently forever. `pi_notify.sh` must be at `/usr/local/bin/pi_notify.sh`; it reads the
Telegram secrets from `/etc/tapo-monitor/secrets.env`, and a host that keeps them
elsewhere sets `TAPO_ENV_FILE` in `/etc/tapo-monitor/notify.env`.

## The whole fleet in one table

Every check above reports on one host at a time. The daily pass over all of them was a
few dozen ad-hoc ssh one-liners, phrased a little differently each morning, so the answer
could never be laid beside yesterday's. `tools/fleet_status.sh` asks every host the same
questions in the same order, in parallel, and prints one row each:

```bash
export TAPO_FLEET_HOSTS="alpha beta gamma"   # ssh targets, or label=ssh-target
tools/fleet_status.sh
```

```
tapo fleet status — 2026-09-02 07:12:03 CEST, night window 18:00-08:00

HOST   UNIT           RST  UPTIME  FINGERPRINT  EXPECT DIGEST  HOSTWATCH  DISK  LOAD   TEMP  PANLIM  SENT
alpha  running          0   7h20m  3f9c2d81a0b4 ok     today   ok 1m       34%  0.83  42.0C      11    25
beta   running          8   7h20m  3f9c2d81a0b4 ok     today   none        26%  0.35  49.1C       2    14
gamma  AUTO-RESTART    14      12s  8b1e04c7d5aa DRIFT  LATE    ok 1m       13%  0.18  47.2C       0     0
scorer ok, up 9h51m, requests 54983 (+318 in 61m), failed 6, p95 0.92s

details
  alpha: release 20260901T215256Z-3f9c2d81a0b4
         digest due 20:45, last sent 2026-09-01 20:45; frames 09-01 18:00 -> 09-02 07:12; 149G free
  ...

findings
  gamma: tapo-monitor.service is crash-looping (activating/auto-restart) — is-active alone would call this healthy
  gamma: fingerprint drift: running 8b1e04c7d5aa, env file expects 3f9c2d81a0b4
  gamma: digest was due at 20:45 and has not gone out; last was 2026-08-31
```

Which machines make up the fleet is configuration, not code — the same rule as
`WATCH_TARGETS`. This repository is public, so `TAPO_FLEET_HOSTS` is required and the
script prints usage when it is unset; nothing about a particular installation is baked
into it. Column by column:

- **UNIT** — `AUTO-RESTART` is the crash loop `systemctl is-active` answers `activating`
  to, which is why the row shows the substate rather than the verdict.
- **RST** — `NRestarts`. Not a finding on its own; a deploy restart counts too. A number
  that has climbed between two mornings is the signal.
- **FINGERPRINT / EXPECT** — what the running package hashes to, against
  `TAPO_EXPECTED_FINGERPRINT` in that unit's own env file. `DRIFT` means the host is
  running something other than what the last deploy intended, `unset` that this host was
  never enrolled in the check. When the package cannot report its fingerprint at all the
  release directory's name is used instead, and the details block says so — that name
  cannot notice a file edited in place afterwards.
- **DIGEST** — `today` sent, `pending` (yesterday's went out, today's hour has not come
  yet), `LATE` (the hour passed and nothing went out), `MISSING`, `off` (no
  `TAPO_REVIEW_DIGEST_TIME`). Since the digest is also the fleet heartbeat, `LATE` on an
  otherwise healthy row usually means Telegram, not the daemon.
- **HOSTWATCH** — the peer watch timer and how long ago it last ran. `none` means this
  host watches nobody, which is correct for the host at the end of the chain and worth
  noticing when it is not.
- **PANLIM / SENT** — frames the pan guard archived and photos delivered to Telegram
  during the window named in the header: `--night HH:MM-HH:MM`, default `18:00-08:00`,
  evaluated in each host's own clock. A window that closes, rather than the last 24
  hours, so this morning's traffic cannot leak into a number about the dark.

The scorer gets its own line because it shares none of those columns. Its counters are
cumulative, so a single reading says nothing; the run remembers the previous one under
`${XDG_STATE_HOME:-~/.local/state}/tapo-fleet-status` and reports the movement since the
last check instead. A restart between two runs is called out, because the counters reset
with it.

One check has no column: if `current` was switched *after* the unit last started, the
daemon is still executing the previous release while every file on disk agrees with the
new one. That lands in `findings`.

It reads and nothing else — no restart, no deploy, no sudo, nothing written on any host,
and only three variables are ever read out of an env file that also holds credentials. A
host that does not answer within `TAPO_FLEET_TIMEOUT` (60 s) gets an `UNREACHABLE` row
and a finding rather than aborting the table. The exit status is 0 when the findings
block is empty and 1 when it is not. The rest of the knobs (`TAPO_FLEET_UNIT`,
`TAPO_FLEET_ROOT`, `TAPO_FLEET_PYTHON`, `TAPO_FLEET_SCORER_URL`, …) are documented in the
script's own header.

## Snapshot sources

The monitor chooses frames in this order:

1. Live RTSP snapshot via `ffmpeg`.
2. Event-time follow-up when `sd_snapshot` / `sd_motion` is enabled and the event deserves
   a second chance. By default this downloads the segment from the camera **SD card**; with
   `snapshot_source: recording` it instead reads the local recorder tree (`RECORDING_ROOT`)
   for the event window — full stream1 resolution even when detection runs on stream2.
   With a local scorer, both SD and recorder follow-ups compare all extracted candidates
   and pick the *sharpest* above-threshold frame (ffmpeg `blurdetect`). This costs extra
   scorer requests but no additional download. Missing blur measurements fall back to
   detection score; a scorer outage retains a previously confirmed candidate when possible.
   Sharpness measures the whole frame, so it cannot guarantee an unblurred moving subject.
   `recording` reuses the SD follow-up queue, so it requires `sd_snapshot: true`, and it
   falls back to the SD/live path when no matching segment exists or `RECORDING_ROOT` is unset.
   The recording read has its own per-camera timing, `recording_early_span` (first look,
   default 24 s, 6-60) and `recording_ready_margin` (default 15 s, 5-60). A lower margin
   can truncate the end of a window the recorder has not flushed yet, and a lower span
   moves subjects first seen later into the retry. Every `frame selection` log line ends
   with `offset=<s>`, the picked frame's seconds from the event's start (`n/a` when the
   frame name carries no capture time), to measure how many finds fall in the first 12 s
   before shortening either value.
   A person follow-up arrives a minute or two after its event, so it is held to the
   confirmed cooldown when its result is back: if another passage of the same camera
   reached the phone in between, the follow-up is not sent but audited as `sd` `cooldown`
   with `reason=sd_within_cooldown`, and its frame goes to the review log (verdict
   `cooldown`) when `TAPO_REVIEW_LOG_DIR` is set. The cooldown the live pass arms when it
   queues the follow-up does not count, and neither does a motion alert: a person found
   on the card still follows a motion photo.
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

## Soak report

`tools/soak_report.sh` answers the questions of a soak test before a release in one
command, from the workstation and read-only (one ssh login per host, nothing restarted or
written on it). Hosts are ssh aliases from the arguments or `SOAK_HOSTS`; no host is named
in the repository.

```bash
SOAK_HOSTS="host-a host-b" tools/soak_report.sh --since 24
tools/soak_report.sh --user --since 12 host-c          # host with a systemd user unit
tools/soak_report.sh --baseline soak.json host-a        # first run saves, later runs compare
tools/soak_report.sh --json host-a                      # machine-readable
```

Per host it prints the service state and package fingerprint (`tapo-monitor version`, the
`current` release), starts in the window, `Traceback|ERROR|CRITICAL` lines (grepped from the
journal text, not `journalctl -p`), alerts sent per camera and path, the median and p90 of
the first delivered alert per event (`event_age_s` of the audit `send` rows), `drop`, `hold`
and `cooldown` counts, `would_*` observe rows per camera, `host_switch`, outbox and hub
lines, and service memory and CPU from `systemctl show`.

A check never reads a missing thing as a clean one: a unit that is not loaded is judged by
`LoadState` (`systemctl show` on a nonexistent unit still says `Result=success`), a probe
without its end marker, an unreadable journal and an empty journal turn the counts into
`unknown` plus a finding, and a counter with accounting off shows `n/a`. `--baseline FILE`
stores the run as JSON when the file does not exist and otherwise marks deviations (alert
volume beyond `--tolerance`, default 30 %, and at least five alerts of difference; new or
growing errors, starts, hub/outbox problems and `host_switch`; a changed package or a service
that was healthy before). `--update-baseline` rewrites it after comparing. Exit status is 1
when any host has a finding or deviation. Parsing and statistics live in
`tools/soak_report.py` and are tested on fixtures in `tests/fixtures/soak`.

## Accelerated soak simulation

`tools/soak_sim.py` is the fast half of a soak test: it runs the real `daemon.loop_step`
(the function every production tick runs) against the scenario harness's fake cameras on
a fake clock, for N simulated days in seconds or minutes, and looks for what only a long
run shows. With `--wrapper` it goes through `daemon.tick` instead, the wrapper production
runs, so the stall watchdog and the status view are covered too. It needs a checkout with the dev extras (it imports `tests/scenario.py`), reads
no config and touches no network, camera or host.

```bash
python3 tools/soak_sim.py --days 7 --seed 3 --cameras 3 --features all
python3 tools/soak_sim.py --days 1 --features zones,loiter --max-rss-growth-mb 15 --json
nice -n 19 python3 tools/soak_sim.py --days 7 --tracemalloc    # also lists allocation growth
python3 tools/soak_sim.py --days 2 --seed 1 --wrapper           # what CI runs, per seed
```

The generated world: bare motion and camera-confirmed people (two thirds of them at night),
rain intervals, camera outages, scorer outages (the whole service, or the primary only),
the network dropping (cameras, scorer and Telegram at once), daemon restarts (`shutdown()`,
then state reloaded from `runtime.json` and `health.json`), lenses swinging out of their pan
span, and a scorer stand-in with random scores, boxes, vehicles and delays. `--features`
(comma list, default `all`) adds the opt-in options under test: `zones`
(`alert_zones_mode: observe`), `loiter` (`loiter_mode: observe`), `vehicle`
(`vehicle_mode: observe`), `hub` (a hub camera with `hub_clip_worker` and a real worker
thread), `host` (`host` candidate list with address changes) and `scorer` (`scorer.url`
candidate list). The summary lists the audit paths the run really exercised
(`path/action=count`): a feature that never fired proves nothing.

It measures resident memory (`/proc/self/statm`; the median of the last 10 % of the run
minus the median of the 10-30 % window, so start-up is not counted), thread count, the
size of `health.json`, `runtime.json` and `twin.json`, the sent-log and review-log
directories, the ledger, exceptions out of a tick plus ERROR log records, and the wall time
of each tick. A run that stops making progress is aborted with a stack dump
(`--hang-timeout`). Exit status is 1 when a limit is exceeded: `--max-rss-growth-mb`
(20), `--max-threads` (32), `--max-state-kb` (256), `--max-dir-growth-mb-per-day` (100),
`--max-tick-seconds` (5), `--max-exceptions` (0). `--inject leak|statefile|threads` plants
a fault on purpose (`filesleak` leaves one `sdjob_*` job dir with a file per tick); the
tests use it to prove the limits fire.

`--wrapper` replaces the direct `loop_step` call with `daemon.tick`: `loop_step` is wrapped
so it still gets the harness's fake collaborators, and one stretch of failing ticks per
simulated day (longer than `alerts.stall_threshold`) is injected. The stall watchdog must
send its alert and its recovery notice (the injected tick errors are counted separately,
not as exceptions), and every `--status-every` ticks (6) the `/status` payload is built
through `statusd.served_snapshot`, serialised and timed. Findings: an episode with no stall
alert, an alert with no recovery, a status snapshot that raises or takes longer than
`--max-status-seconds` (0.5). No port is opened: the HTTP thread itself is not simulated,
only what it serves. A restart is never planned inside a stall.

File leaks: at each sample the run counts the files in the sent log, review log and
`panlimit-log` (the pan guard's evidence grabs) and the `sdjob_*` temp dirs (SD and hub clip
jobs; the harness redirects the temp directory into its own and fetches hub clips through
the real job-dir lifecycle). The growth per simulated day is judged like the directory
sizes (median of the last 10 % minus the 10-30 % window) against
`--max-files-growth-per-day` (1000 per directory; a normal run adds about 100 a day to the
sent log). The oldest archived frame must not be older than its retention plus
`--retention-slack-days` (1.1), and the summary shows how many files retention removed.

How retention is exercised: the daemon's retention compares each file's mtime with the
`now` it is handed, which in the simulation is a fake clock, while the files are written in
real time, so nothing would ever look old. The harness therefore stamps the mtime of new
archive files with the simulated time (hourly) and gives the sent-log module the simulated
clock for names and index timestamps; then the daily prune and the index compaction really
run. It needs a run longer than the retention: 2 days for the sent log and `panlimit-log`,
7 for the review log (a shorter run shows only accumulation, no pruning). The granularity is
one simulated hour. Orphan `sdjob_*` dirs of a killed daemon are cleaned at a real start by
pid; the simulation keeps one pid, so that cleanup is not exercised.

CI runs `--days 2 --features all --wrapper` for seeds 1, 2 and 3 in the parallel `soak`
job (about a minute and a half in total, 15 minute timeout), so the test job is not
delayed.

What it does not show: real network and camera timing, pytapo and ffmpeg themselves, the
HTTP server thread and its socket, code that reads the wall clock instead of the `now` the
loop hands down, and the behaviour of a real disk (full, slow). A clean run is a reason to
start the real soak, not a replacement for it.

## Inspecting alert frames

Threshold tuning is easier when you can see the exact frame behind a score. Two opt-in aids
capture frames for review without changing alert behaviour.

**Archive what was sent.** Set `TAPO_SENT_LOG_DIR` and every photo delivered to Telegram is
also copied there as a timestamped JPEG beside an `index.jsonl` line: timestamp, filename,
caption and delivery flag, plus the camera name and the scorer's `person`/`animal`
confidences when they are known. The camera name is what lets a host running two cameras
tell from the archive which one fired. A frame sent for a camera event (every alert path:
live, sampler, SD follow-up, hub clip and its retry, hold rescue, hold expiry) also records
`incident` (`<camera>-<event start>`, see
[observability](observability.md#following-one-incident)) and `event_start`, the event's
start in whole epoch seconds, so the frames of one visit group without guessing from
timestamps; frames with no event behind them omit both. Every alert frame also records
`path`, the delivery path that sent it — the audit line's path, split where the audit
tells sends apart only by `reason`:

| `path` | sent by | audit line |
|---|---|---|
| `live` | the live pass, right after `getEvents` | `live` `send` |
| `sampler` | a sampler follow-up grab (also a corroborated hold) | `sampler` `send` |
| `sd` | the SD clip or local-recording follow-up | `sd` `send` |
| `hubpoll` | a hub clip | `hubpoll` `send` |
| `hubpoll_retry` | a hub clip whose first delivery failed | `hubpoll` `send` `reason=retry` |
| `hold_rescue` | a held frame after a pan-limit recall | `sampler` `send` `reason=hold_rescue_recall` |
| `hold_expiry` | a held frame when its hold expired (`sampler.hold_expiry: send`) | `sampler` `send` `reason=hold_expiry_send` |

Records written before `path` existed have none. Files older than `TAPO_SENT_LOG_RETENTION_DAYS`
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
person score; `index.jsonl` records the rest, including the same `incident` and
`event_start` as the sent log). This is the ground truth for confirming a hold
suppressed an animal/empty scene rather than a person. Pruned by `TAPO_REVIEW_LOG_RETENTION_DAYS`
(default 7); inert when unset; never raises into the alert path.

```bash
export TAPO_REVIEW_LOG_DIR=~/tapo-monitor/review-log
export TAPO_REVIEW_LOG_RETENTION_DAYS=7   # optional, default 7
```

The same directory also receives a random sample of frames dropped below
`scorer.threshold` (live pass, sampler, SD/recording selection; hub-clip drops are kept in
full), so a person the scorer rated far too low can still reach the
[labelling](labeling.md#collecting-a-dataset) queue. `TAPO_REVIEW_DROP_SAMPLE` sets the
share (default `0.05`, `0` turns it off, anything outside 0–1 falls back to the default)
and `TAPO_REVIEW_DROP_MAX_PER_HOUR` caps it per camera and clock hour (default 6), so a
rainy night cannot flood the log. Sampled records carry `sample_rate`; the daily digest
counts them on one line of their own and never sends their photos.

```bash
export TAPO_REVIEW_DROP_SAMPLE=0.05       # optional, default 0.05
export TAPO_REVIEW_DROP_MAX_PER_HOUR=6    # optional, default 6
```

**Archive what the pan guard saw.** Each `pan_limit` intervention saves one frame — the
out-of-bounds view, grabbed just before the recall erases it — into a `panlimit-log`
directory beside the review log (or the sent log, whichever is configured; with neither
there is nowhere sane to write and nothing is kept). The filename carries the camera, axis
and position, so a night of recalls is skimmable without an index. Deliberately its own
directory rather than the review log: the digest reads the review log, and twenty guard
recalls a night must not flood it. Retention is fixed at 2 days; archiving is best-effort
and never costs a recall.

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
and any self-heal a camera is refusing. It also names the running package fingerprint (the
value `tapo-monitor version` prints); set `TAPO_EXPECTED_FINGERPRINT=<fingerprint>` once in
the daemon's environment file and a mismatch becomes a failed check like any other — silent
code drift has twice been found only by manual inventory, after the fact. From then on
`deploy_release.sh` and `rollback_release.sh` keep the value current themselves: whatever
they just switched `current` to is by definition the intended release. They only ever
update an existing line — a host without one stays unenrolled.

Two rules keep it honest. It only claims `Fleet OK` for what it actually checked — a host
with no recorder gets no recorder line rather than a reassuring one, and a camera whose
state is not yet known is named as unchecked rather than counted either way. And any failed
check takes the headline away entirely (`🟠 Fleet degraded — …`), because a heartbeat that
says OK while a camera is down is worse than no heartbeat: it turns a silence you might
have questioned into a confirmation you will trust.

Self-heal refusal totals are labelled **since daemon start**. A recent Digital Twin
health check with no alertable drift can verify recovery without erasing those totals;
stale or missing control-health observations cannot certify recovery. Battery cameras
continue to use the existing hub-session observations rather than active camera probes.

When the standalone [network watcher](network-watch.md) is installed for the daemon's
user, the digest reads its local 24-hour cache: loss, p95 RTT, jitter, measurement
coverage and the worst UTC hour with its probe count. Partial hours and missing coverage
need context; ICMP loss is separate from detection-event loss. Reading the cache sends
no additional packets or camera requests.

```text
📋 Review digest: 3 suppressed frame(s) in the last 24h
yard: 2 (max p0.61)
shadow scan 2026-05-04: 192 of 192 segments, 1389 frames, 4 matched, 1 candidate(s)

💚 Fleet OK — front, yard reachable
   scorer 0 failed / 4180 req, p95 0.73s
   recorder newest file 47s old
   logs sent 12.3 MB / 140 files, review 85.0 MB / 912 files, pan-limit 1.2 MB / 30 files, 20480 MB free
   alerts 24h: 62 sent (yard 41, front 21), 1 undelivered
```

**Keep the frame logs from filling the disk.** The `logs` line gives the size and file count
of the sent, review and pan-limit logs (measured once per digest, flat and capped at 50,000
files per directory, shown as `50000+` when the cap bites) and the free space on the
filesystem holding them — the lowest one if they sit on different filesystems. It is a
detail line and never fails the check; below the floor it reads
`512 MB free (below the 1024 MB floor)`. The page comes from the daemon itself: it checks
free space every 10 minutes and, when it drops below `TAPO_LOG_DISK_MIN_FREE_MB`, sends one
Telegram warning (`💾 log disk low: … MB free, below the … MB floor`). It does not repeat
while the disk stays low; once free space is back 10 % above the floor it sends
`✅ log disk recovered` and re-arms. The state lives in memory, so a restart during a low
spell warns once more. An undelivered message is retried on the next check.

```bash
export TAPO_LOG_DISK_MIN_FREE_MB=1024     # optional, default 1024; 0 turns the warning off
```

Note what this still cannot do: a heartbeat the host sends itself can never report that the
host is dead. Noticing the *absence* of a daily message is a job for a human or an external
dead-man's switch, not for this daemon — that switch is
[watching the hosts themselves](#watching-the-hosts-themselves).

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

Schedule it with a systemd timer in the small hours, niced, from the same release layout
and environment file as the daemon (the console script does not exist in an extracted
release — use the venv interpreter with `-m`, as the unit does):

```ini
[Service]
Type=oneshot
Nice=15
IOSchedulingClass=idle
EnvironmentFile=-/etc/tapo-monitor/secrets.env
WorkingDirectory=%h/tapo-monitor/current
ExecStart=%h/tapo-env/bin/python -m tapo_monitor shadow-scan %h/tapo-monitor/cameras.yaml

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

## Watching the hosts themselves

Everything above reports from inside the host: the daemon's outage messages, `OnFailure`
hooks and the daily digest are all written by the machine they describe. A host that loses
power, its network or its disk sends nothing — and nothing is exactly what a healthy quiet
day looks like. Closing that gap needs a dead-man's switch where the alarm is raised by
something other than the host being watched.

`tools/host_watch.sh` is that switch. It runs on a *peer* host under a two-minute timer,
pings each target named in its env file (optionally probing an HTTP health endpoint too)
and alerts over the Telegram credentials the host already has for `pi_notify.sh`. One
watcher still leaves one unwatched host — the watcher itself — so point two hosts at each
other and the loop is closed. Which host watches which is deliberately configuration, not
code: it lives in `/etc/tapo-monitor/host-watch.env` on each host and never in this
repository.

```env
# /etc/tapo-monitor/host-watch.env — example values, addresses from RFC 5737
WATCH_TARGETS="alpha=192.0.2.10 beta=198.51.100.20,health=http://198.51.100.20:8766/health"
WATCH_FAILS=3
WATCH_COOLDOWN=1800
TAPO_ENV_FILE=/etc/tapo-monitor/secrets.env
```

The quotes around `WATCH_TARGETS` matter: it is a space-separated list. Each entry is
`name=ping_addr`, optionally followed by `,health=URL` for a service worth checking
separately — the scorer's `/health` is the natural candidate, because when it dies alerts
stop at every site at once. Ping and health are separate findings with separate state: a
failing health URL on a host that still answers ping alerts on its own ("service dead,
host alive"), while an unreachable host costs exactly one alert — the health check is
skipped until ping returns, since a dead host fails HTTP trivially.

The alerting rules are the ones the rest of the project already uses. A single miss never
alerts (`WATCH_FAILS` consecutive misses first — three at the two-minute cadence is about
six minutes); once alerted, the same finding repeats at most once per `WATCH_COOLDOWN`
seconds; a target that returns gets a recovery message carrying how long it was down.
Every send is confirmed against Telegram's `"ok":true` before it arms a cooldown or
closes an episode, so an unconfirmed message is retried on the next pass rather than
lost. Counters and cooldown stamps live under `/var/tmp/host-watch/`, which survives a
reboot of the watcher — a reboot must not forget that a peer is down.

Install on each watching host:

```bash
sudo install -m 0755 tools/host_watch.sh /usr/local/bin/host_watch.sh
sudoedit /etc/tapo-monitor/host-watch.env      # the four variables above
sudo cp systemd/host-watch.service systemd/host-watch.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now host-watch.timer
sudo systemctl start host-watch.service        # one manual pass
systemctl status host-watch.service            # should be inactive (dead), exit 0
```

A down peer is a finding, not a failure: the script exits non-zero only when the watcher
itself is broken (unparseable target, unconfirmed Telegram send), which the unit's
`OnFailure=pi-failure-notify@%n.service` hook then reports — a blind watchman is worse
than none, because it converts silence into false reassurance.

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
rather than to RAM. `tapo-monitor selfcheck` reports this on every run — a `journal:
WARNING held in RAM` line is this problem — and these are the commands behind it:

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

- Monitor: `tools/rollback_release.sh <ssh-host> <release-name>` re-points the `current`
  symlink and restarts — see [Release deploys and rollback](#release-deploys-and-rollback).
- Scorer: restore the previous venv/model, or set `scorer.url` empty to fall back to
  raw/Groq gating.

## Separate operational Telegram destination

Set both `TAPO_OPS_TELEGRAM_TOKEN` and `TAPO_OPS_TELEGRAM_CHAT_ID` in the
monitor's private environment file to route camera outages, event API failures,
monitor stalls, disk warnings, configuration drift and camera state notices to
an operator chat. Detection photos, their outbox summaries and daily digests keep
the destination configured under `telegram`. With neither override set, existing
notification routing is preserved. A partial override fails delivery rather than
mixing credentials from different destinations. `TAPO_OPS_LABEL` optionally adds
a site label to operational messages.

For a lightweight gateway, run external reachability checks on a peer. A systemd
timer override with `OnUnitActiveSec=30s`, `AccuracySec=1s` and `WATCH_FAILS=3`
detects a sustained outage in approximately 60–90 seconds plus probe time.
Keep local network measurement cadence independent from alert cadence. Configure
`alerts.outage_threshold`, `alerts.stall_threshold` and
`alerts.event_failure_threshold` explicitly when a shorter camera alert delay is
needed; this does not change camera polling or reboot thresholds.
