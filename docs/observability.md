# Camera observability: Digital Twin and Shadow Detection Auditor

The first health implementation answered a necessary but narrow question: can the host
reach the camera, and how long has that network observation stayed up or down? That is
still the correct outage signal—an unplugged cable should be diagnosed without attempting
camera authentication—but it cannot prove that the camera is useful. A reachable camera
may have a broken API, stale `getEvents`, failed RTSP, unhealthy storage or a silently
disabled person detector.

The current design therefore keeps the simple uptime monitor and builds two opt-in layers
above it:

- the **Camera Digital Twin** answers “is each service layer healthy, and does observed
  camera state match the control plan?”;
- the **Shadow Detection Auditor** answers “how do camera events compare with observations
  made independently from the camera event trigger?”

Neither subsystem changes detector thresholds or camera settings. Both are observation
tools first.

## Configuration and rollout

All features default off. Start with persistence and CLI inspection, then enable alerts
only after the observed state is understood:

```yaml
observability:
  digital_twin: true
  probe_interval: 900
  drift_alerts: false
  ledger: true
  ledger_retention_days: 30
  shadow_match_window: 20
```

`probe_interval` has a 60-second minimum and should normally remain much wider than the
event poll. The daemon probes only clients it already connected during its control pass;
it does not create a second login loop. A failing or unsupported getter is isolated and
cannot stop event polling or Telegram delivery.

Recommended rollout:

1. enable `digital_twin` and inspect several probe cycles;
2. verify unknown/unsupported fields on every model and firmware;
3. enable `ledger` and confirm retention and disk growth;
4. feed shadow observations from a local worker;
5. enable `drift_alerts` only when desired/actual mappings are stable.

## Digital Twin data and commands

The snapshot uses a fixed allow-list of public read-only getters. Each probe is recorded
as `available`, `unknown` or `error`, and the payload is recursively redacted before it is
persisted. Raw transport/module enumeration is deliberately disabled because it has been
unsafe on tested firmware. Empty or missing methods are evidence of unknown/unsupported
capability, not permission to guess or mutate camera state.

Health is reported independently for:

| Layer | Evidence |
| --- | --- |
| network | unauthenticated reachability observation |
| API | results from isolated safe getters |
| events | latest `getEvents` poll outcome |
| RTSP | latest event-triggered snapshot outcome |
| storage | safe SD-card status getter |

The desired-state comparison currently covers person detection enabled, vehicle detection
disabled, motion sensitivity and auto-track state. Unknown and unsupported actual values
never alert. Stable drift keys deduplicate repeated mismatches; with `drift_alerts: true`,
Telegram receives only a newly observed drift and its later recovery.

Latest state is stored atomically with mode `0600` at
`$XDG_STATE_HOME/tapo-monitor/twin.json` (normally
`~/.local/state/tapo-monitor/twin.json`). Override it with `TAPO_TWIN_STATE_FILE`.
Inspection is offline and never contacts a camera:

```bash
tapo-monitor twin-status
tapo-monitor twin-status --json
tapo-monitor twin-status /path/to/twin.json
```

The file contains only the latest fleet view and alert-deduplication keys. Bounded
transition history and an explicit one-shot probe remain roadmap items.

## Shadow ledger and commands

When `ledger: true`, the daemon mirrors its existing structured audit records into SQLite:
camera detections plus send, defer, cooldown, scorer and Telegram decisions. The database
stores timestamps, local camera labels, event types, sources, confidence and a small
metadata allow-list. It stores no frames, video, stream URLs, credentials, device IDs,
MAC addresses or face IDs.

The path is `$XDG_STATE_HOME/tapo-monitor/events.sqlite3` by default and can be overridden
with `TAPO_LEDGER_FILE`. The file and containing state directory are private. Startup
cleanup applies `ledger_retention_days`; inserts are idempotent. Audit writes cross a
bounded background queue, so a locked or slow database cannot stall event polling or
Telegram delivery; saturation drops observability records instead of live alerts.

An independent recorder/scorer worker can use the Python API
`EventLedger.record_shadow_event(...)` or the stable CLI boundary:

```bash
tapo-monitor shadow-record front person 1710000000 --confidence 0.91 \
  --adapter local_scorer
tapo-monitor shadow-report front --hours 24 --window 20
tapo-monitor shadow-report front --hours 24 --window 20 --json
```

The report pairs same-type observations one-to-one within the symmetric time window:

- `matched`: both sources observed a compatible event;
- `camera_only`: the firmware emitted an event without matching shadow evidence;
- `shadow_only`: independent evidence had no matching camera event;
- `precision_like`, `recall_like`, `f1_like`: operational hints, not ground-truth model
  metrics.

In particular, `shadow_only` is a review candidate, not an automatically proven camera
miss. Clock alignment, worker coverage and detector calibration must be checked first.
The independent always-watching worker is the next major phase; the foundation delivered
here is its privacy-safe ingestion, storage and reporting contract.

## Development hand-off

The architecture is split deliberately so later work does not require camera hardware:

- `capabilities.py`: safe getter manifest, probe isolation and redaction;
- `drift.py`: pure desired/actual comparison and layered-health aggregation;
- `twin.py`: desired-state mapping and atomic latest-state persistence;
- `ledger.py`: SQLite observations, decisions, retention and correlation;
- `daemon.py`: opt-in scheduling using already-connected clients;
- `cli.py`: offline/operator and external-worker interfaces.

The implementation sequence, remaining gaps and longer research tracks are maintained in
[`roadmap.md`](roadmap.md). Tests cover redaction, unsupported/error behavior, deterministic
matching, retention, callback health, interval throttling, drift deduplication/recovery,
failure isolation, persistence and CLI output.
