# `hubpoll` — detection source for battery / hub-backed cameras

**Status:** design approved 2026-08-14; hub API **verified live** the same day (§4.2 shapes all
answered `error_code=0` against a real H200-class hub on firmware 1.6.5).
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
  `STORAGE_NOT_EXIST`; getEvents is dead. **Recordings live on the hub's storage**, which
  reports per-camera used space, so each camera's footage is individually accounted for.
  Adding SD cards to the cameras is therefore *not* a prerequisite for this design.
- **The hub is always online** and indexes those recordings (§4.2). It is the correct,
  race-free event source.
- **The hub's SslAes handshake is flaky and strictly rate-limited**, but **queries inside an
  established session are reliable** — six sequential queries at ~1.5 s spacing completed in
  one held session without a hiccup, while a fresh handshake was accepted on the second
  attempt after a ~90 s pause. The cost is establishing the session, not querying it. The
  phone app competes for the hub's single session slot.

### Why the existing pipeline can't absorb this unchanged

The sampler is **not** a standalone detector. `state.groups` are created **only** by the
getEvents path (`run_monitor_pass` → `sampler.ensure_group`); `process_sampler` merely
advances groups that already exist. A battery/hub camera produces no getEvents groups,
so it needs a **fully standalone pass** that triggers, captures, scores, gates, and sends
on its own.

## 2. Goals / non-goals

**Goals**
- A new per-camera detection source `"hubpoll"` that turns hub-indexed recordings into
  scored Telegram alerts, reusing the existing scorer, cooldown gate, and alert sender.
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
  - `hub_device_id` / `hub_device_mac` — optional; **auto-discovered at startup** from the
    hub's own paired-device list (§4.2), matched to the camera by alias or configured hint.
  - `go2rtc_src` — go2rtc source name for the frame snapshot.
  - `hub_poll_interval` — seconds between hub polls (default e.g. 20).
- There is **no channel field**: recordings are addressed by device id + MAC, not by an
  NVR-style channel index (§4.2).
- A hubpoll camera has **no stok login**. The `run_monitor_pass` gate
  (`if "getevents" not in cfg.detection.sources: continue`) already excludes it, so the
  legacy path needs no change.

### 4.2 Hub session manager (`hubclient.py`, new) — verified API

Establishes **one** SslAes session and **holds it**. Every call is wrapped in
`multipleRequest`; a single-call request hits a framing bug. Verified method set:

**Camera discovery** — newer hub firmware moved camera-class discovery off the child-device
family onto its own paired-device list:

```jsonc
getGeneralDeviceList  {"general_camera_manage": {"paired_general_device_list": {}}}
// → max_bound / current_bound plus, per camera: alias, mac (no separators), device_id,
//   device_model, category:"camera", network_mode, hub_storage_enabled, plan_24h_record
```

**Recording index (= the event index)** — two stages, addressed by `child_device_id` +
`child_device_mac`:

```jsonc
searchDateWithVideo  {"playback": {"search_year_utility": {"channel": [0],
   "child_device_id": "<id>", "child_device_mac": "<mac>",
   "start_date": "YYYYMMDD", "end_date": "YYYYMMDD"}}}
// → playback.search_results[].search_results_N.date   (days that actually have footage)

searchVideoWithUTC   {"playback": {"search_video_with_utc": {"channel": 0,
   "child_device_id": "<id>", "child_device_mac": "<mac>",
   "start_time": <epoch>, "end_time": <epoch>,
   "start_index": 0, "end_index": 999, "player_id": "<uuid hex upper>"}}}
// → playback.search_video_results[].search_video_results_N = {startTime, endTime, video_type}
```

The clip window must be sent as **whole seconds**: measured against a real hub, a window
carrying `time.time()` floats answered `error_code=0` with an empty list, while the
identical window as integers returned the clips. The window itself is free-form — it does
not have to sit inside one calendar day.

**Why this is an event index, not a tape:** the paired-device list reports
`plan_24h_record: false`, and the day index skips days without motion; observed clips are
~11–13 s long. So each indexed clip corresponds to a triggered recording, and its
`startTime` is the detection time the pass needs.

**Per-camera storage** (`getSdCardChildUsedSpace` with
`{"harddisk_manage": {"device_list": []}}`) returns each camera's used space and
`bind_status`, useful as a liveness/sanity signal.

**Client responsibilities**

- Exposes minimal single-method queries: `list_cameras()`, `search_days(...)`,
  `search_clips(...)`, and the clip fetch used for the frame fallback (§4.4).
- **Reconnect policy:** on `Server disconnected` / connection error, exponential backoff
  (cap a few minutes), never faster than the observed handshake tolerance. Eviction by
  the phone app is expected → **log, back off, reconnect; never emit a user alert for it.**
- **Never send an unknown method with empty params** — that was observed to raise an
  internal hub error and drop the session. Always namespace-shaped params.
- Lives in a **new `MonitorState` field**, NOT in `cam_clients` (which is `.clear()`ed
  every control pass — a held session there would be destroyed each cycle).
- Only single-method queries; no heavy batched `multipleRequest`.

**Ruled out (do not re-litigate):** the child-device family (`getChildDeviceList`,
`getChildDeviceComponentList`) reports an empty list even though the hub counts the cameras,
and `controlChild` forwarding is refused per-model — that family covers sub-GHz children
(sensors, doorbells), not Wi-Fi cameras bound for recording. The hub's channel-addressed
`searchDetectionList` answers `error_code=0` with an empty body, and `searchVideoDayList`
does not exist on the hub. Prior art: pytapo issue #194 (same firmware family);
python-kasa issue #1723 is the same symptom, still open and uncommented upstream.

### 4.3 `run_hubpoll_pass` (`daemon.py`)

Called in `loop_step` immediately after `monitor(...)`, as a new injectable collaborator
(defaulted alongside the other passes). Per configured hubpoll camera:

1. If the hub session isn't up, ask the manager to (re)connect within its backoff budget;
   if still down, return quietly (no alert).
2. On the first successful session, resolve the camera's `device_id`/`mac` from
   `list_cameras()` and cache it in state.
3. Poll `search_clips(device, since=cursor, until=now)`.
4. For each clip whose `startTime` is newer than the per-camera **cursor** (dedup by
   `startTime`), in chronological order:
   a. Capture a frame via the go2rtc backend (the camera is awake while it records).
   b. **Fallback:** if the snapshot fails (camera already asleep / go2rtc not reconnected),
      fetch the clip from the hub (§4.4) and take a frame from it.
   c. Score via `score_for(cfg)` → `score(image_path)`.
   d. Gate via `alert_gate(state, cfg.name, cooldown, now)`.
   e. On pass, send via `send_alert_photo(cfg, secrets, frame, caption, score=...)` with the
      caption timestamped at the clip's `startTime`, not at send time.
5. Advance the cursor to the newest processed clip time.

`score_for`, `alert_gate`, and `send_alert_photo` are reused **as-is** — the frame is a
filesystem path (a `Frame(str)`), which is the contract the scorer and sender already expect.

### 4.4 Frame capture

**Primary — go2rtc snapshot backend** (`snapshot.py`) — verified live: three consecutive
grabs against a running sidecar returned real ~180 KB JPEGs.

- New function mirroring `capture_rtsp`'s signature/return (an image path or `None`),
  wired through a hubpoll analogue of the daemon's default-snapshot injection point.
- Fetches `GET http://<go2rtc-host>:<port>/api/frame.jpeg?src=<go2rtc_src>`, writes bytes
  to a temp `.jpg`, returns the path. go2rtc runs as a sidecar and speaks the camera's
  native protocol, so it reconnects to the camera when motion wakes it.
- Reuses the same temp-file cleanup discipline as the RTSP path (no frame leak).

**Fallback — clip from the hub** (still to be validated end-to-end)

- There is no JSON download method. The clip comes from a **media session against the hub
  on port 8800** with a download-type request naming the camera's MAC and device id; the
  response is MPEG-TS, which ffmpeg remuxes / frame-extracts.
- Known gotcha from prior art: a missing-nonce key-exchange error on the first attempt →
  open a throwaway playback-type session, wait, then retry the download.
- The JSON index path (§4.2) is verified through the kasa SslAes transport; **the port-8800
  transport is the one remaining unverified piece** and should be validated before this
  fallback is relied on. Until then, a clip-only detection with no live frame is logged and
  skipped rather than sent blank.

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

- One held hub session; minimal single-method queries; no heavy batches; no unknown method
  with empty params.
- Exponential backoff on disconnect; app eviction is normal, not an alert.
- Frame capture best-effort; a detection that yields no frame is logged and skipped
  (never a blank alert).
- Cursor persists in `MonitorState` so a restart doesn't replay old clips
  (initialise cursor to "now" on first run to avoid an alert storm from history).

## 7. Config example (PII-free)

```yaml
cameras:
  - name: side-gate
    detection:
      sources: ["hubpoll"]
    hub_host: "<hub-ip>"            # creds via env-var name, per existing convention
    hub_device_id: null             # auto-discovered from the hub's paired-device list
    hub_device_mac: null
    go2rtc_src: "side-gate"
    hub_poll_interval: 20
# unchanged mains camera keeps sources: ["getevents"] and its stok login
```

## 8. Testing strategy

- **Unit — hub client:** session lifecycle with a fake transport (connect, held query,
  disconnect → backoff → reconnect); param shaping for the discovery and both index
  queries; `list_cameras()` parses the paired-device list and matches a camera by alias;
  app-eviction path emits no alert.
- **Unit — hubpoll pass:** with a fake hub client (returns synthetic clips), a fake frame
  backend, and a fake scorer, assert: dedup by cursor (no reprocessing), chronological
  ordering, `alert_gate` consulted, `send_alert_photo` called once per fresh clip, caption
  stamped at clip start time, frame-fallback taken when the snapshot returns `None`, and
  no send when no frame could be obtained.
- **Unit — no-regression:** a config with a hubpoll camera + a getevents camera; assert
  `run_monitor_pass` skips the hubpoll camera and `_watchdog_pass` does not fire an outage
  alert for it; the getevents camera behaves exactly as before.
- **Unit — snapshot backend:** mock the go2rtc HTTP endpoint (bytes → path; error → None);
  temp file cleaned up.

## 9. Open items

- **Clip download transport (port 8800).** The only unverified piece; needed for the frame
  fallback, not for detection itself. Validate before relying on it (§4.4).
- Camera→hub addressing is **resolved**: device id + MAC from the paired-device list, no
  channel discovery, nothing to hard-code.

## 10. Rollout

1. Land config + hub client + pass + snapshot backend behind the `hubpoll` source (no
   camera uses it yet → inert).
2. Enable `hubpoll` for one battery camera; verify device auto-discovery, a real clip →
   scored alert, and that no false outage alert fires.
3. Enable the second camera; tune `hub_poll_interval` and cooldowns to the site's volume.
4. Optional: validate the port-8800 clip fetch and switch the frame fallback on.
