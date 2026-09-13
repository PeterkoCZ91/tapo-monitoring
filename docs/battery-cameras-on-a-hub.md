# Battery cameras that record to a hub

Notes from adding two battery Tapo cameras (C410 / C460 class) paired to an H200-class hub,
observed across firmware 1.6.x through 1.7.5 (the hub auto-upgrades overnight, so treat any
single version mentioned below as a snapshot, not a floor). They behave nothing like the
mains PTZ cameras the rest of this project was built for, and the parts that surprised us
are written down here because the upstream
libraries do not cover them yet: `python-kasa` issue #1723 describes the same symptom (a
hub that counts its cameras but lists none) and is still open and uncommented, and the
addressing below was pieced together with `pytapo` issue #194 as the starting point.

Everything here was verified against real hardware. Addresses, MACs, device ids and camera
names are the operator's, so they stay out of this repository — the shapes use placeholders.

## What is different about these cameras

- **No usable RTSP.** They speak the SslAes ("SMARTCAM") API, not the legacy stok protocol,
  so the daemon's pytapo client cannot drive them and port 554 stays closed even with
  third-party access enabled.
- **They sleep, even on external power.** Firmware treats them as battery devices. Measured
  over five hours of a quiet night, each answered ICMP about 6 % of the time in windows of
  ~15 s. A reachability poll is therefore useless as a trigger. The one useful exception: a
  camera stays awake for the *duration of a real event*, because it is recording.
- **No event index of their own** unless a working SD card is fitted. Without one, the
  camera's `searchDetectionList` returns `STORAGE_NOT_EXIST` and its `getEvents` equivalent
  is dead. The recordings — and therefore the detections — live on the hub.

## Reaching them through the hub

The hub is always online, so it is the event source. Two things about it cost us a day:

**Camera-class devices are not child devices.** `getChildDeviceList` with
`{"childControl": {"start_index": 0}}` answers `sum: 0` even while the hub's own
`getDeviceInfo` reports `child_num: 2`, and `controlChild` forwarding is refused per model
(`-50021`). That family covers sub-GHz sensors and doorbells — a paired doorbell shows up
there with `subg_*` signal fields. Wi-Fi cameras bound to the hub for recording come from a
different list:

```jsonc
getGeneralDeviceList  {"general_camera_manage": {"paired_general_device_list": {}}}
// → max_bound / current_bound, and per camera: alias, mac (no separators), device_id,
//   device_model, category:"camera", network_mode, hub_storage_enabled, plan_24h_record
```

**Recordings are addressed by device id + MAC, not by a channel.** The hub answers
channel-addressed `searchDetectionList` with `error_code: 0` and an empty body, which reads
like "no detections" and is really "wrong addressing"; `searchVideoDayList` does not exist
on the hub at all (`-40106`). The index is two stages:

```jsonc
searchDateWithVideo  {"playback": {"search_year_utility": {"channel": [0],
   "child_device_id": "<id>", "child_device_mac": "<mac>",
   "start_date": "YYYYMMDD", "end_date": "YYYYMMDD"}}}
// → the days that actually hold footage

searchVideoWithUTC   {"playback": {"search_video_with_utc": {"channel": 0,
   "child_device_id": "<id>", "child_device_mac": "<mac>",
   "start_time": <epoch>, "end_time": <epoch>,
   "start_index": 0, "end_index": 999, "player_id": "<uuid hex upper>"}}}
// → {startTime, endTime, video_type} per clip
```

Both answer in the hub's indexed-wrapper style: a list of single-key dicts
(`{"search_results_1": {...}}`), not a plain list.

**The clip index is an event index.** The paired-device list reports
`plan_24h_record: false`, days without motion are missing from the day index, and observed
clips run 11–13 s. So each indexed clip is a triggered recording and its `startTime` is the
detection time — which is exactly the cursor a poller needs.

**Per-camera storage** comes from `getSdCardChildUsedSpace` with
`{"harddisk_manage": {"device_list": []}}`: used space and bind status per camera, useful as
a sanity signal that a camera really is recording where you think it is.

## Operational rules the hub enforces on you

On H200 firmware 1.7.5, reusing an HTTP connection repeatedly disconnected the second
login request. Keeping the authenticated SslAes session while opening a fresh TCP
connection for each HTTP request made sequential queries work. The hub adapter therefore
owns an HTTP client with connection reuse disabled and closes that client, its worker
thread and event loop on shutdown, including when discovery fails.

- **The handshake is the expensive part, not the queries.** A fresh session is accepted only
  sporadically; several queries inside an established one are reliable (six sequential
  queries at ~1.5 s spacing, no trouble). Open one session and hold it.
- **A "connected" device object is not a session.** `Discover.discover_single` only sends a
  UDP discovery datagram — the real handshake happens on the first `send`. Prove a new
  session with one cheap query before trusting it, or a refused handshake gets misfiled as a
  failed query and the backoff ladder punishes a session that never existed.
- **Release the session when the process ends.** An orphaned session blocks every new
  handshake for at least six minutes. Measured: a clean close lets the next handshake
  through immediately, a killed process does not — so close on shutdown *and* on SIGTERM,
  which is what a `pkill` or a service restart sends.
- **The phone app competes for the same slot.** Being evicted is normal; log it, back off,
  reconnect, and never turn it into a user-facing alert.
- **Wrap every request one method at a time** in `multipleRequest`. A bare single-method call
  hits a framing bug, and an unknown method sent with empty params was observed to take the
  whole session down — so always use namespace-shaped params, never `{}`.
- **Send epoch windows as whole seconds.** The same clip window carrying `time.time()` floats
  came back empty; as integers it returned the clips.

Error codes seen: `0` ok, `-40101` bad params, `-40106` unknown method, `-50021` unsupported
model, `-60305` unsupported, `-71114` storage does not exist.

## Getting a frame

There is no JSON method for a clip. A download-type media session against the hub's media
port, naming the camera's MAC and device id, returns MPEG-TS; one frame is extracted a
second past the clip start, because the first frames of a recording are the most smeared. A
missing key-exchange nonce on the first attempt is answered by opening a throwaway playback
session once and retrying.

Two limits of that download path, learned from prior art rather than from our own hardware
(`phit/vigilatus`, whose Electron client plays the same recordings, and pytapo's own
`media_stream`):

- **Media parts are encrypted from a `key-exchange` header** — key `md5(nonce:hashed_pw)`,
  IV `md5(username:nonce)`. pytapo raises when that header carries no nonce, but some
  firmware variants legitimately omit it and serve the parts in plaintext, so a "nonce
  missing" failure is not proof that the session was set up wrong.
- **The stream expects flow-control ACKs**: parts carry a sequence number and the client is
  expected to acknowledge every window of them. pytapo sends none. Clips of 11–13 s (~3 MB)
  download fine without them, which is what a motion-triggered recording produces here — but
  a site with longer segments (24/7 recording on the hub) should expect a stall part-way and
  needs the ACKs implemented.

The alternative is a **go2rtc sidecar**, which speaks the camera's native protocol and
serves `GET /api/frame.jpeg?src=<name>`. It works — but only while the camera is awake.

**Prefer the clip.** The clip *is* the event: measured at 3–5 s and ~3 MB, it yields a frame
from the moment of detection. A live grab can only happen once the poll has noticed the clip,
20–30 s later, when the camera is usually asleep again and the frame shows an empty scene —
the same empty-scene false alert this project has already paid for once. The sidecar is the
backup for when a download fails.

**Both frame paths need `ffmpeg` on the daemon's own PATH** — the clip is decoded with it,
and the sidecar shells out to it too. This is worth checking before anything else when a
site detects events but never alerts: a daemon started from `cron` or a unit file inherits a
minimal PATH, so an `ffmpeg` installed under the operator's `~/.local/bin` is simply not
there. It cost this project two days at one site — 13 real detections, every one dropped as
`no_frame`, while the clips themselves downloaded perfectly. The daemon now says so once at
startup instead of leaving it to be inferred per event.

## A protocol-accurate test double

The hub's local API turns out to be impersonable, which is worth knowing before reaching
for real hardware to test against.

**The handshake is a mutual challenge over the account password, not a certificate.** The
client proves it knows the password (`SHA256(cnonce + pwd_hash + snonce)`), and the "hub"
proves the same thing back — so anything that knows the account password can play the hub's
side of the handshake and land on the *same* derived AES session key the real client
derives, no certificate involved. The TLS layer underneath is not checked at all
(`verify_mode = CERT_NONE` on the client): a self-signed certificate is accepted without
complaint. This is a different code path from the camera's outbound cloud-iot connection,
which *does* pin a private CA baked into the firmware and cannot be intercepted this way —
the distinction is local-device API vs. outbound cloud API, not a general weakness of the
protocol family.

**Consequence: a standalone mock hub is straightforward to build.** A small server that
speaks the real handshake, decrypts each `securePassthrough` request, and answers from a
table of previously-captured real responses (falling back to `UNSUPPORTED_METHOD` for
anything not yet recorded) is enough to develop and exercise the hub client against without
any hardware present, and without the H200's own quirks (the reused-connection disconnect,
the multi-minute session-eviction lockout) getting in the way of an unrelated test run. Seed
the response table by pointing a real client at a handshake-compatible proxy sitting between
it and the real hub once; every response it returns is genuine hub output, not a guess at
the schema.

**What this technique does *not* reach: live view.** The hub advertises a `preWakeUp` app
component, which reads like exactly the mechanism you'd want for shortening the wake-to-frame
latency described above. It was never observed as an invoked method in real traffic, despite
capturing a full session including a live-view attempt — the phone app's video path connects
directly to the camera rather than through the hub, and most likely negotiates over
WebRTC/SRTP (the hub separately advertises an `srtpWebrtc` component) once past the initial
handshake. That traffic never takes the shape of an HTTP request, so nothing at the HTTP
layer — including this technique — sees it. Whatever actually wakes the camera for a live
look, if it is a discrete signal at all rather than the camera simply polling its radio, lives
outside JSON API reach.

## Consequences for the daemon

See `docs/configuration.md` for the `hubpoll` detection source these notes produced. The
parts worth repeating:

- A hubpoll camera is **excluded from the ICMP watchdog** — a sleeping camera fails nearly
  every ping and would raise a nightly outage alert. Its liveness is a successful hub poll.
- It **skips the stok login** entirely; there is none to make.
- `rotate` is **rejected** in config rather than silently ignored, because a finished JPEG
  from either frame path has no capture-time filter behind it.
- The cursor starts at *now* on the first pass, so a hub full of stored clips is not replayed
  as an alert storm at startup.
- Expect **20–40 s** from motion to alert: ~13 s of recording before the clip is indexed,
  up to one poll interval, then a few seconds to download, score and send. On a mains camera
  the same path takes seconds; this is the price of a camera that sleeps.
- A camera the paired-device list reports as `plan_24h_record: true` is **not polled for
  clips**. Everything above assumes an event-only camera, where an indexed clip and a
  triggered recording are the same thing; on a camera recording continuously (H200 firmware
  1.6.5 added 24/7 Capture for compatible models, e.g. C460 — this C410 does not support it)
  that assumption breaks, and treating every segment as motion would alert on plain footage.
  `video_type` might eventually distinguish the two, but its values (`2`, `6` observed so
  far) are not confirmed against any visual ground truth yet, so the daemon refuses rather
  than guesses: it logs once and leaves the camera resolved but idle.
