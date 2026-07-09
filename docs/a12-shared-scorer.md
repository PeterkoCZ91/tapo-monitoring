# A12 shared scorer integration

This project owns the shared YOLO scoring service used by both tapo-monitor and A12.
The integration point is only HTTP. A12 must not import `tapo_monitor` modules, and
tapo-monitor must not import A12 modules.

For the full production process map, including the tapo-monitor daemon, shared scorer,
A12 container and optional recorder fallback, see
[`runtime-topology.md`](runtime-topology.md).

For GitHub/public-release notes that stay aligned with the A12 side of the integration,
see [`a12-github-repo.md`](a12-github-repo.md).

## Runtime shape

`tapo_monitor.scorer_service` runs a YOLOX ONNX model and exposes:

- `GET /health` -> `{"ok": true}`
- `POST /score` with JPEG bytes -> JSON scores

Example response:

```json
{
  "person": 0.87,
  "animal": 0.0,
  "classes": {
    "person": 0.87,
    "dog": 0.41
  }
}
```

Tapo uses the top-level `person` / `animal` values via `tapo_monitor.scorer.subject_score`.
A12 uses the `classes` map, filters its configured classes such as `person,dog`, applies
its own thresholds, and then continues its normal event and Telegram pipeline.

The scorer is intentionally stateless. It does not own camera sessions, event watermarks,
cooldowns, SD follow-up queues or Telegram sends. Those belong to the caller: tapo-monitor
for Tapo cameras, A12 for A12 cameras/events.

## A12 configuration

A12 is opt-in through environment/config:

```env
YOLO_BACKEND=http
YOLO_SCORER_URL=http://SCORER_HOST:8766/score
YOLO_CLASSES=person,dog
```

The local A12 `cv2.dnn` model path must remain present as fallback. If the scorer request
fails, A12 falls back locally when possible and otherwise returns no detections; scorer
outage must not crash A12.

When A12 runs in host networking on the same machine as the scorer, `127.0.0.1` refers to
the host namespace, so `YOLO_SCORER_URL=http://127.0.0.1:8766/score` is valid. Without host
networking, use the host or service address reachable from inside the container.

## Contract invariants

- `person` and `animal` stay backward-compatible for tapo-monitor.
- `classes` is per-COCO-class max confidence, keyed by A12-compatible COCO names.
- Low-confidence classes below the service floor are omitted from `classes`.
- A12's return type stays `list[tuple[str, float]]`.
- Thresholds are not shared. Tapo compares `person/animal` to each camera's
  `scorer.threshold`; A12 compares named classes to its own YOLO thresholds.

## Verification

From the A12 runtime environment:

```sh
python - <<'PY'
import os
import urllib.request

url = os.environ["YOLO_SCORER_URL"]
health = url.replace("/score", "/health")
print(urllib.request.urlopen(health, timeout=3).read().decode())
PY
```

Expected:

```json
{"ok": true}
```

A smoke `POST /score` should return the `classes` key. A blank frame may still return a
small nonzero `person` top-level score, but A12 should filter it below threshold and return
no detections.

## Known open work

The shared scorer can use a different model than A12's previous local model. After moving
A12 from local YOLO to the shared scorer, calibrate `YOLO_CONFIDENCE_THRESHOLD`,
`YOLO_NOTIFY_CONFIDENCE_THRESHOLD`, and PIR-specific thresholds against real footage.
