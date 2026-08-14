# `hubpoll` — detection source for battery / hub-backed cameras

**Status:** design (approved 2026-08-14)
**Scope:** add a new per-camera detection source `hubpoll` for battery Tapo cameras
that have no usable RTSP, sleep to save power, and whose per-camera event API is dead
(no SD in the camera → `STORAGE_NOT_EXIST`). Detections are read from the **hub** the
cameras pair to; frames come from a go2rtc HTTP snapshot. The existing legacy "stok"
path (mains cameras via pytapo, e.g. C560WS) must remain untouched.

---

## 1. Problem & context

A third site is built around battery cameras (C410/C460 class) paired to an H200-class
hub instead of mains cameras. Empirically established (see the private rollout roadmap
for the site-specific values):

- **No RTSP / stok path.** These cameras speak the SslAes ("SMARTCAM") API, not the
  legacy stok protocol. The daemon's pytapo client cannot drive them.
- **They sleep even on external power.** Firmware treats them as battery devices. Deep
  night reachability measured ~6 % uptime in ~15 s windows; a reachability poll cannot
  reliably catch them. A camera *is* awake for the duration of a real motion event
  (it stays up to record), which is exactly when a frame is wanted.
- **No usable SD in the camera** → the camera's own `searchDetectionList` returns
  `STORAGE_NOT_EXIST`; getEvents is dead. **Recordings and detections live on the hub**
  (loop recording on the hub's storage, confirmed).
- **The hub is always online** and returns `searchDetectionList` with `error_code=0`.
  It is the correct, race-free event source.
- **The hub's SslAes handshake is flaky and strictly rate-limited.** A fresh session is
  accepted only sporadically after a long quiet period (observed ~1 acceptance per ~6
  fresh-handshake attempts). Crucially, **queries *inside* an established session are
  reliable** — the cost is establishing the session, not querying it. The phone app
  competes for the hub's single session slot.

### Why the existing pipeline can't absorb this unchanged

The sampler is **not** a standalone detector. `state.groups` are created **only** by the
getEvents path (`run_monitor_pass` → `sampler.ensure_group`); `process_sampler` merely
advances groups that already exist. A battery/hub camera produces no getEvents groups,
so it needs a **fully standalone pass** that triggers, captures, scores, gates, and sends
on its own.

## 2. Goals / non-goals

**Goals**
- A new per-camera detection source `"hubpoll"` that turns hub detections into scored
  Telegram alerts, reusing the existing scorer, cooldown gate, and alert sender.
- A hub client that respects the hub's hard operational limits (one held session,
  minimal queries, backoff on disconnect, survives app eviction).
- Zero behavioural change for legacy stok / getEvents cameras.

**Non-goals**
- No mutation of hub or camera settings (project stays read-only by default).
- No attempt to keep battery cameras awake or to poll them directly for events.
- No siren/reflector/speaker actuation.
- Not solving multi-hub topologies now (one hub per hubpoll-configured site).

## 3. Architecture (approach C — standalone pass + dedicated held-session hub client)

```
loop_step tick
  ├─ control block (periodic): rebuild pytapo cam_clients   ← unchanged; hub session NOT here
  ├─ monitor  = run_monitor_pass      (getevents; skips hubpoll cams at the sources gate)
  ├─ hubpoll  = run_hubpoll_pass      ← NEW, gated on "hubpoll" in cfg.detection.sources
  ├─ sample   = process_sampler       (unchanged)
  ├─ drain / guard / digest           (unchanged)
```

Two new units plus one new capture backend:

1. **Hub session manager** (`hubclient.py`, new) — owns one long-lived SslAes session to
   the hub, independent of `cam_clients`.
2. **`run_hubpoll_pass`** (in `daemon.py`) — the standalone detection pass.
3. **go2rtc snapshot backend** (in `snapshot.py`) — a sibling of `capture_rtsp`.

## 4. Components

### 4.1 Config (`config.py`)

- Add `"hubpoll"` to `DETECTION_SOURCES`.
- A hubpoll camera sets `sources: ["hubpoll"]` and gains fields on `CameraConfig`:
  - `hub_host` — hub address (creds referenced by env-var name, as elsewhere).
  - `hub_channel` — optional int; **if unset, auto-discovered at startup** (see 4.2).
  - `go2rtc_src` — go2rtc source name for the frame snapshot.
  - `hub_poll_interval` — seconds between hub detection polls (default e.g. 20).
- A hubpoll camera has **no stok login**. The `run_monitor_pass` gate
  (`if "getevents" not in cfg.detection.sources: continue`) already excludes it, so the
  legacy path needs no change.

### 4.2 Hub session manager (`hubclient.py`, new)

- Establishes **one** SslAes session (via kasa `Discover.discover_single` + the
  transport's `send`, wrapping every call in `multipleRequest`) and **holds it**.
- Exposes minimal, single-method queries: `search_detections(channel, since, until)`,
  `list_channels()`, and a playback clip/thumbnail fetch for the frame fallback.
- **Reconnect policy:** on `Server disconnected` / connection error, exponential backoff
  (cap a few minutes), never faster than the observed handshake tolerance. Eviction by
  the phone app is expected → **log, back off, reconnect; never emit a user alert for it.**
- **Channel discovery:** on first successful session, enumerate channels
  (`searchVideoDayList` / `searchDetectionList` across channel indices) to resolve the
  camera→channel mapping when `hub_channel` is unset. This is where the mapping that could
  not be pinned down by hand gets resolved at runtime; cache it in state.
- Lives in a **new `MonitorState` field**, NOT in `cam_clients` (which is `.clear()`ed
  every control pass — a held session there would be destroyed each cycle).
- Only single-method queries; no heavy batched `multipleRequest` (heavy batches were
  observed to drop the hub connection).

### 4.3 `run_hubpoll_pass` (`daemon.py`)

Called in `loop_step` immediately after `monitor(...)`, as a new injectable collaborator
(defaulted alongside the other passes). Per configured hubpoll camera:

1. If the hub session isn't up, ask the manager to (re)connect within its backoff budget;
   if still down, return quietly (no alert).
2. Poll `search_detections(channel, since=cursor, until=now)`.
3. For each detection newer than the per-camera **cursor** and not already seen
   (dedup by detection id / timestamp), in timestamp order:
   a. Capture a frame via the go2rtc backend (the camera is awake during a live event).
   b. **Fallback:** if the snapshot fails (camera already asleep / go2rtc not reconnected),
      fetch the detection's clip/thumbnail from hub playback and use a frame from it.
   c. Score via `score_for(cfg)` → `score(image_path)`.
   d. Gate via `alert_gate(state, cfg.name, cooldown, now)`.
   e. On pass, send via `send_alert_photo(cfg, secrets, frame, caption, score=...)`.
4. Advance the cursor to the newest processed detection time.

`score_for`, `alert_gate`, and `send_alert_photo` are reused **as-is** — the frame is a
filesystem path (a `Frame(str)`), which is the contract the scorer and sender already expect.

### 4.4 go2rtc snapshot backend (`snapshot.py`)

- New function mirroring `capture_rtsp`'s signature/return (an image path or `None`),
  wired through a hubpoll analogue of the daemon's default-snapshot injection point.
- Fetches `GET http://<go2rtc-host>:<port>/api/frame.jpeg?src=<go2rtc_src>`, writes bytes
  to a temp `.jpg`, returns the path. go2rtc runs as a sidecar and speaks the camera's
  native protocol, so it reconnects to the camera when motion wakes it.
- Reuses the same temp-file cleanup discipline as the RTSP path (no frame leak).

## 5. Not breaking existing behaviour

- **Watchdog false outage.** `_watchdog_pass` reads `state.network_reachable[name]` from a
  host ping; a sleeping battery camera always fails it → false "unreachable" alert.
  **Fix:** exclude hubpoll cameras from `_watchdog_pass`, and instead drive their liveness
  from **hub-poll success** (hub reachable + session healthy = site healthy). A genuine
  dead-site signal then comes from the hub session being down past a threshold, not from
  the camera ping.
- **Wasted connect.** `_connect_camera` would ping + attempt a stok login against the
  sleeping host every control pass. **Fix:** skip login for hubpoll-only cameras (no stok
  client is expected for them).
- **Legacy stok path.** Untouched: the `sources` gate auto-skips hubpoll cameras in
  `run_monitor_pass`, and all pass consumers already use `cam_clients.get(...)` and no-op
  on a missing client, so no `KeyError`/`None` deref is introduced.

## 6. Error handling & operational constraints (baked in)

- One held hub session; minimal single-method queries; no heavy batches.
- Exponential backoff on disconnect; app eviction is normal, not an alert.
- Frame capture best-effort with a hub-playback fallback; a detection that yields no
  frame is logged and skipped (never a blank alert).
- Cursor persists in `MonitorState` so a restart doesn't replay old detections
  (initialise cursor to "now" on first run to avoid an alert storm from history).

## 7. Config example (PII-free)

```yaml
cameras:
  - name: side-gate
    detection:
      sources: ["hubpoll"]
    hub_host: "<hub-ip>"            # creds via env-var name, per existing convention
    hub_channel: null              # auto-discovered at startup if null
    go2rtc_src: "side-gate"
    hub_poll_interval: 20
# unchanged mains camera keeps sources: ["getevents"] and its stok login
```

## 8. Testing strategy

- **Unit — hub client:** session lifecycle with a fake transport (connect, held query,
  disconnect → backoff → reconnect); `search_detections` param shaping; channel
  auto-discovery picks the non-empty channel; app-eviction path emits no alert.
- **Unit — hubpoll pass:** with a fake hub client (returns synthetic detections), a fake
  frame backend, and a fake scorer, assert: dedup by cursor (no reprocessing), ordering,
  `alert_gate` consulted, `send_alert_photo` called once per fresh confirmed detection,
  frame-fallback taken when snapshot returns `None`, blank frame → skip (no send).
- **Unit — no-regression:** a config with a hubpoll camera + a getevents camera; assert
  `run_monitor_pass` skips the hubpoll camera and `_watchdog_pass` does not fire an outage
  alert for it; the getevents camera behaves exactly as before.
- **Unit — snapshot backend:** mock the go2rtc HTTP endpoint (bytes → path; error → None);
  temp file cleaned up.

## 9. Open items resolved at deploy time

- **Camera→channel mapping.** The hub does not expose the cameras as `getChildDeviceList`
  children (`sum:0`); they are NVR-style channels. The exact channel index is discovered
  by `list_channels()` on first session rather than hard-coded — so the design does not
  block on it. `hub_channel` can be pinned in config once known, to skip discovery.

## 10. Rollout

1. Land config + hub client + pass + snapshot backend behind the `hubpoll` source (no
   camera uses it yet → inert).
2. Optional de-risking step: add a mains stok camera at the site first to validate the
   daemon→scorer→Telegram chain there before `hubpoll`.
3. Enable `hubpoll` for one battery camera; verify channel auto-discovery, a real
   detection → scored alert, and that no false outage alert fires.
4. Enable the second camera; tune `hub_poll_interval` and cooldowns to the site's volume.
