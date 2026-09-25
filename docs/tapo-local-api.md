# Tapo camera local API — field notes beyond pytapo

**Who this is for:** anyone talking to a Tapo camera over its local HTTPS API — users of
[pytapo](https://github.com/JurajNyiri/pytapo), the Home Assistant Tapo integration, or
their own scripts — who has hit a call pytapo does not wrap, an error code it does not
explain, or a model that answers differently from the one they tested on.

**What it adds over pytapo:** pytapo is the client; it knows how to log in and wraps a
long list of methods. This page collects what we learned while running
[tapo-monitoring](../README.md) against real cameras for months: the parameter
conventions behind the wrappers, what the error codes mean in practice, SD card states
the app hides (including counterfeit cards), how events look on a dual-lens camera,
methods pytapo does not wrap, and — most important — calls that take the camera's API
down. Nothing here needs tapo-monitoring; every example is a plain pytapo call.

**How to read it:** firmware behaviour differs between models and even between builds of
one model. Every finding says where it was seen and how sure we are. Treat each row as
"seen on", never "guaranteed".

| Tag | Camera | Firmware | pytapo | When |
|---|---|---|---|---|
| **C545D** | Tapo C545D (dual lens), HW 1.0, EU | 1.1.7 Build 260421 | 3.4.18 | 2026-09 |
| **C560WS** | Tapo C560WS (two cameras) | 1.1.10 Build 260330 | 3.4.14 | 2026-06 |
| **C260** | Tapo C260 | 1.1.12 Build 260128 | 3.4.14 | 2026-06 |

Confidence words used below: **reproduced** (seen on repeated, deliberate tries),
**n=…** (number of independent observations), **seen once** (a single sample — a lead,
not a fact).

> [!WARNING]
> **Dangerous calls — read this first.**
>
> - **`checkDetectEventState` with `{}` takes the API down on the C545D.** Port 443
>   refuses connections for about 12 s, then comes back with the session invalidated
>   (the HTTP API process restarts). *C545D, reproduced (2 of 2); a 5-minute idle
>   baseline without API calls had 0 s of refused connections.* The same call on a
>   C560WS just returns `{}`.
> - **`getInfLampCapability` with `{}` did the same on the C545D.** *Seen once, not
>   repeated on purpose.* It answers normally on a C560WS.
> - **Methods borrowed from another model family are not harmless probes.** Call unknown
>   methods one at a time, a few seconds apart, and watch whether port 443 still accepts
>   connections after each one.
> - **`reverseWhitelampStatus` is a toggle, not "switch on".** Fired at a lit lamp, it
>   switches it off. Read `getWhitelampStatus` first and only toggle a lamp that is off.
>   *C560WS, reproduced.*
> - **pytapo's `executeFunction` writes behind your back on `-64303`.** When a call fails
>   with `-64303` (motor busy) it sends `setCruise(False)` and retries. A "read-only"
>   probe must block that path too.
> - **Setters accept fields they do not know and report success.** A `set…` that returns
>   `error_code: 0` has not necessarily stored anything — read the value back.
>   *C560WS / C260, reproduced on several setters.*

## 1. Transport and call shape

- HTTPS on port 443 with a self-signed certificate. Login returns a session token
  (`stok`) that goes into the URL path: `POST /stok=<token>/ds`.
- `Tapo.executeFunction(method, params)` wraps one call in a batch and unwraps the first
  answer:

  ```json
  {"method": "multipleRequest",
   "params": {"requests": [{"method": "getSdCardStatus",
                            "params": {"harddisk_manage": {"table": ["hd_info"]}}}]}}
  ```

  The inner answer has `method`, `result` and `error_code`; pytapo returns `result` when
  `error_code` is 0 and raises otherwise.
- A second, generic form addresses a module directly: `{"method": "get", "<module>":
  {"name": [...]}}` for reads and `{"method": "set", "<module>": {...}}` for writes.
  pytapo uses it internally for, e.g., `getModuleSpec`. Example that returns the
  capability flags (`ptz`, `motor`, `target_track`, `smart_detection`,
  `storage_api_version`, `stream_max_sessions`, …) — all models above:

  ```python
  tapo.performRequest({"method": "get", "function": {"name": ["module_spec"]}})
  ```

- **Pacing:** keep calls at least ~1.5 s apart. At 0.7 s spacing the C545D answered
  `-40109` twice; at 1.6–3 s it never did (*C545D, n=2 errors in one session*). Bursts
  right after login are the usual trigger.
- **Two clients at once:** two pytapo sessions against one camera mostly work, but once a
  call that coincided with the other client's poll failed with a top-level `-40214`
  (*C545D, seen once*). Production code should keep one session per camera.
- **Firmware auto-upgrade** was on by default on a new C545D (03:00 ± 120 min) and
  upgraded it from 1.1.2 to 1.1.7 within minutes of first setup; a 1–2 minute API outage
  in that first session was most likely the upgrade applying. Record `sw_version` (`getBasicInfo`) with every
  finding — method behaviour can change overnight.

## 2. Parameter conventions

Most `-40101` errors come from the right method with the wrong parameter shape. The
patterns we have seen:

| Pattern | Example params | Notes |
|---|---|---|
| `name` list (or string) | `{"motion_detection": {"name": ["motion_det"]}}` | the common read form; an unknown sub-key → `-40101` |
| `table` | `{"harddisk_manage": {"table": ["hd_info"]}}`, `{"system": {"table": "chn_info"}}`, `{"msg_alarm": {"table": "msg_alarm_type"}}` | list-shaped data |
| channel-prefixed name | `{"msg_push": {"name": ["chn1_msg_push_info"]}}`, `{"record_plan": {"name": ["chn1_channel"]}}` | on the C545D `chn2_…` still gives `-40101`: these settings exist once per device |
| `chn_id` list (multi-lens) | `{"motion_detection": {"name": ["motion_det"], "chn_id": [1, 2]}}` | answer keyed per lens, see §7; without `chn_id` the camera answers for lens 1. Not every getter takes it |
| action sub-object | `{"system": {"get_user_id": "null"}}`, `{"patrol": {"get_patrol_action": {}}}`, `{"dual_cam_linkage": {"read_linkage_target_setting": {}}}` | |
| playback search | `{"playback": {"search_detection_list": {...}}}` | see §5 |

**Start with `getAppComponentList`** (`{"app_component": {"name": "app_component_list"}}`)
on a new model: it lists the feature components the firmware has and their versions (52
on the C545D, e.g. `dualCam`, `dualCamLinkage`, `multiLensCam`, `panoramicView`). It tells
you which families of methods are worth trying before you try them.

## 3. Error codes in plain words

pytapo's `const.py` names most codes; this is what they meant in practice.

| Code | pytapo name | What it meant for us | What to do |
|---|---|---|---|
| `0` + data | — | works | — |
| `0` + `{}` or `{"<module>": {}}` | — | method and params accepted, but the feature is absent or not set up (e.g. bark detection on a camera without that model) | treat as "not available", not as an error |
| `-40101` | Parameter to set does not exist | wrong parameter shape or sub-key, or a key this model does not have (face detection on a C545D, `chn_id` on a device-wide setting) | check the shape (§2); on a new model it often just means "absent" |
| `-40106` | UNSUPPORTED_METHOD | the method exists but will not run like this on this model. On the C545D `getWhitelampStatus` with `chn_id` returned `-40106` **and** a filled `result` | drop the extra parameter; do not trust a result that comes with an error |
| `-40109` | ONE_SECOND_REPEAT_REQUEST | calls too close together | slow down (§1) |
| `-40209` | Invalid login credentials | wrong password — and also returned for some methods to a plain (non-pytapo) HTTP login on the C260, which pytapo's session could call | use pytapo's session; beware of lockout after failed logins |
| `-40210` | METHOD_DO_NOT_EXIST | the firmware does not know this method name | try the name another model uses, or give up |
| `-40214` | *(not in pytapo)* | undocumented. Seen for a valid search that has nothing to return (empty window), right after a white-lamp switch (C560WS, transient), on the first `getCloudInfo` of a session (C560WS), and once as a top-level error while a second client was polling (C545D) | retry once after a pause; do not read it as "unsupported" |
| `-40401` | Invalid stok value | the session expired or the API restarted | pytapo logs in again by itself |
| `-64303` | MOTOR_BUSY | the motor is busy | note pytapo's automatic `setCruise(False)` (see the warning box) |
| `-71101` / `-71102` | USER_ID_FULL / USER_ID_EMPLOYED | playback user slots full / taken | pytapo retries with a fresh `getUserID` |
| `-71103` / `-71105` | USER_ID_INVALID / PLAYBACK_SEARCH_FAILED | the playback user id expired, or the search failed | pytapo refreshes `getUserID` and retries |
| `-71112` | *(not in pytapo)* | playback search refused: extra keys in `search_detection_list` on the C260, and the video searches on the C560WS (§5); also when `searchEventList` had nothing to return | send exactly the documented keys |
| `-71114` | STORAGE_NOT_EXIST | no SD card, so there is no event or recording index | insert a card, or use `getLastAlarmInfo` (§6) |
| connection refused on 443 | — | the HTTP API process is down: a restart after a dangerous call (see the box) or a firmware upgrade | wait 10–120 s and log in again |

An earlier probe of the C260 reported face-gallery methods (`getFaceManagement`,
`searchFaceList`, `getFaceDB`, …) as "Connection aborted". A later capture of the raw
HTTP traffic on the same firmware got an ordinary `-40210` for all of them, so we do not
treat "connection aborted" as a sign that a method exists.

## 4. SD card

### Status — `getSdCardStatus` (pytapo `getSDCard()`)

Request params: `{"harddisk_manage": {"table": ["hd_info"]}}`. Answer on the C545D with a
freshly formatted 64 GB card (the `*_accurate` twins of each size field are left out):

```json
{"harddisk_manage": {"hd_info": [{"hd_info_1": {
  "disk_name": "1", "type": "local",
  "status": "normal", "detect_status": "normal",
  "rw_attr": "rw", "write_protect": "0",
  "percent": "0", "loop_record_status": "0",
  "record_duration": "0", "record_free_duration": "0",
  "record_start_time": "1700000000",
  "total_space": "0B", "free_space": "55.0GB",
  "video_total_space": "55.3GB", "video_free_space": "55.0GB",
  "picture_total_space": "0B", "picture_free_space": "0B",
  "crossline_total_space": "0B", "crossline_free_space": "0B",
  "msg_push_total_space": "0B", "msg_push_free_space": "0B"
}}]}}
```

- **Read capacity from `video_total_space` / `video_free_space`.** On the C545D
  `total_space` stayed `"0B"` and `percent` `"0"` on a working card (*C545D, reproduced
  across sessions*). On the C560WS `total_space` is filled in.
- `loop_record_status` was `"0"` on the C545D although loop recording was on (see below);
  on a C560WS it was `"1"`. Read the loop setting from `getCircularRecordingConfig`.
- **`detect_status` values seen:** `offline` (no card inserted), `normal`, and
  **`dilatant_suspect`** — the camera suspects the card reports more capacity than it
  really has, i.e. a counterfeit ("expanded") card. Two such cards would not format in
  the app; the one we also formatted over the API went `formatting` → back to
  `unformatted` with `dilatant_suspect` within ~15 s, and read as `rw_attr: "r"`
  (read-only) (*C545D, n=2 cards; API format and `rw_attr` checked on one*). The app does
  not say this clearly; the API does. **Replace the card.** The API does not enumerate
  the possible values, so there may be others.

### Format — `formatSdCard` (pytapo `Tapo.format()`) — a write, erases the card

pytapo already wraps it; no need for a raw call:

```python
tapo.format()
# sends: {"method": "formatSdCard", "params": {"harddisk_manage": {"format_hd": "1"}}}
```

On a good card the call returned `error_code: 0`, and about 10 s later `getSdCardStatus`
showed `status: normal`, `rw_attr: rw` (*C545D, n=1 good card*). On the
`dilatant_suspect` card we tried it on, it did not produce a writable card.

### Loop recording — `getCircularRecordingConfig`

`{"harddisk_manage": {"name": "harddisk"}}` → `{"harddisk_manage": {"harddisk": {"loop":
"on"}}}` (*C545D, C560WS*).

### Near-full cards

With a nearly full card and loop recording, event clips appeared in the index before
they were fully written, and downloads came back empty (0 bytes). Leaving a margin
(clip end at least ~90 s old) before downloading reduced it (*C560WS, one camera with
~36 MB free*).

## 5. Events and recordings (playback)

### Playback user id — `getUserID`

`{"system": {"get_user_id": "null"}}` → `{"user_id": 2}`. A small integer slot number the
recording searches take as `id` (2 on the C545D, 13 on a C560WS). pytapo fetches and
refreshes it for you.

### Event list — `searchDetectionList` (pytapo `getEvents()`)

What `getEvents()` sends:

```json
{"playback": {"search_detection_list": {
  "start_index": 0, "end_index": 999, "channel": 0,
  "start_time": 1700000000, "end_time": 1700000660}}}
```

- **The default window is short.** `getEvents()` with no arguments searches from
  10 minutes ago to 1 minute ahead (camera time). For anything older pass the window:
  `getEvents(startTime, endTime)` with epoch seconds. pytapo adds the camera's time
  correction to the returned `start_time` / `end_time` and adds `startRelative` /
  `endRelative`; the raw API does neither.
- **No server-side filtering.** Extra keys (`detection_type`, `with_face_info`, …) were
  rejected with `-71112` on the C260 and silently ignored on the C560WS; in neither case
  did they filter anything.
- Response wrapper: `{"playback": {"snapshot_enable": true, "search_detection_list":
  [...], "total_num": N, "to_be_continued": 0}}`. `snapshot_enable` did not come with any
  local way to fetch snapshots (`searchSnapshotList` → `-40210`).

**Single-lens event** (C560WS / C260):

```json
{"start_time": 1700000000, "end_time": 1700000054, "alarm_type": 2,
 "events_1": 524290, "event_info": []}
```

**Dual-lens event** (C545D) — there is **no top-level `events_1`**; each lens that fired
reports its own bitmask under `chn_events` (`"1"` = fixed wide lens, `"2"` = pan/tilt
lens):

```json
{"start_time": 1700000000, "end_time": 1700000112, "alarm_type": 6,
 "chn_events": {"1": {"events_1": 34, "event_start_time": 1700000002},
                "2": {"events_1": 34, "event_start_time": 1700000000}}}
```

Code that reads `event["events_1"]` sees nothing on such a camera. OR-ing the lenses'
`events_1` gives a value comparable to a single-lens camera's.

**The `channel` parameter** on the C545D (one session, 10 real events, *reproduced for
each value*):

| `channel` | Answer |
|---|---|
| `0` (pytapo's default) | one row per event with both lenses merged under `chn_events`; `total_num` counts rows **per lens** (18 for 10 events) |
| `1` | only events the wide lens saw, `chn_events` holds only `"1"` (10) |
| `2` | only events the pan/tilt lens saw, `chn_events` holds only `"2"` (8) |

### The `events_1` bitmask

| Bit | Value | Meaning | Evidence |
|---:|---:|---|---|
| 1 | 2 | motion | all models above |
| 3 | 8 | unknown, seen with `alarm_type` 4 | C560WS, unconfirmed |
| 5 | 32 | C560WS: PIR sensor (always together with `alarm_type` 6). C545D: came with every person walk-by, together with `alarm_type` 6, on both lenses — the C545D has no PIR | C560WS: ~10,400 events over 2.5 months, two cameras. C545D: n=8 person walks, one camera, one day |
| 7 | 128 | unknown, seen with `alarm_type` 8; vehicle suspected | C560WS, unconfirmed |
| 8 | 256 | unknown, seen with `alarm_type` 9; pet or line crossing suspected | C560WS, unconfirmed |
| 19 | 524288 | on-device AI person | C560WS, C260. **Not set** on the C545D for any of its 8 person walks |

So the same bit means different things on different models — decode per model. On the
C545D plain motion was `alarm_type` 2 with `events_1` 2 on the wide lens only (n=2).
`alarm_type` alone is not a reliable class either: on the C560WS it separates a
PIR-corroborated channel (6) from the plain one (2) rather than naming what was seen.
More detail: [the `events_1` bitmask](events1-bitmask.md).

Also observed on the C545D: a long event is split into consecutive events about every
180 s, `end_time` grows while an event is active, and a later query returned one event's
`start_time` 1 s earlier than the first poll had.

### Recording searches

| Method | Params (`{"playback": {<key>: ...}}`) | Answer |
|---|---|---|
| `searchDateWithVideo` | `search_year_utility: {channel: [0], start_date, end_date}` (dates `YYYYMMDD`) | `search_results: [{search_results_1: {date}}]` |
| `searchVideoOfDay` | `search_video_utility: {channel, date, id, start_index, end_index}` | `search_video_results: [{search_video_results_N: {startTime, endTime, vedio_type}}]`, `filter_enable`. On the C545D: **two clips per event** (one per lens, starts 0–14 s apart) with no lens label |
| `searchVideoWithUTC` | `search_video_with_utc: {channel, id, start_time, end_time, start_index, end_index}` | same list; on the C545D **one clip per event** with `chn_times: {"<lens>": {"event_start_time"}}` and `vedio_type` as a string |
| `searchEventList` *(not in pytapo)* | `search_event_list: {start_time, end_time, start_index, end_index}` | `{total_event, page_event, index_order, search_event_list: [{start, end, type}]}` — every event clip on the card, no detection metadata |

- `vedio_type` (sic): `1` = continuous recording segment, `2` = event clip.
- On the C545D the `channel` field of the video searches was ignored (`1` answered the same
  as `0`).
- In one probe of a C560WS, `searchDateWithVideo` and `searchVideoOfDay` returned
  `-40106` and `searchVideoWithUTC` `-71112` (*seen once*; the exact params of that probe
  were not kept, so this may be a parameter detail rather than a missing feature). The
  C545D answered all three.
- `searchEventList` returned more rows than `searchDetectionList` for the same window (95
  vs 51 in 24 h on a C560WS; on the C545D one row per lens clip, 18 for 10 events). Its
  times matched `searchDetectionList` on the C545D but were ~21 s off on the C560WS. An
  empty window returned `-40214` or `-71112` instead of an empty list.

### Downloading clips

pytapo's `Downloader` (v1) works; `DownloaderV2` is marked unfinished in pytapo's own
source. The output-directory argument needs a trailing slash (`"/tmp/clips/"`) — without
it the file name is glued onto the directory name. `Downloader` also takes the camera's
time correction (`getTimeCorrection()`) as an argument.

## 6. Without an SD card — `getLastAlarmInfo`

With no card, `getEvents()` fails with `-71114`. The camera still remembers its last
alarm, reachable with parameters pytapo does not use:

```python
tapo.executeFunction("getLastAlarmInfo", {"system": {"name": ["last_alarm_info"]}})
# -> {"system": {"last_alarm_info": {"last_alarm_type": "motion",
#                                    "last_alarm_time": "1700000000"}}}
```

It gives only the last alarm: no bitmask, no lens, no end time, and the type is coarse —
it called a person event `motion` (*C545D, n=1 comparison*). Polling it for a change of
`last_alarm_time` is a usable, if crude, card-less motion signal.

Do not confuse it with pytapo's `getAlarmConfig()`, which sends the **same method name**
with `{"msg_alarm": {"name": ["chn1_msg_alarm_info"]}}` and gets the siren/light alarm
configuration back.

## 7. Multi-lens cameras (C545D)

- The lenses are **channels** of one device, not child devices: `getAllChnInfo`
  (`{"system": {"table": "chn_info"}}`) lists channel 1 "Fixed Lens" and channel 2
  "PT Lens"; `getChildDeviceList` → `-40210`.
- **pytapo 3.4.18 already takes `chn_id`** on the detection getters and setters
  (`getMotionDetection(chn_id=[1, 2])`, `getPersonDetection(...)`, …). The raw answer is
  keyed per lens, e.g. `{"motion_detection": {"motion_det_chn": {"1": {...}, "2":
  {...}}}}` (`detection_chn` for person/vehicle/pet/line-crossing/tamper).
- **Per lens** (take `chn_id`): motion, person, vehicle, pet, line-crossing and tamper
  detection; night-vision mode; white-lamp config; day/night mode; rotation; lens
  distortion correction (lens 2 → `null`, it has none).
- **White lamp per lens:** `getWhitelampConfig(chn_id=[1, 2])` returns
  `wtl_intensity_level` for each lens separately (`3` out of the box); `wtl_force_time`
  exists only on lens 1. pytapo's `setWhitelampConfig(intensityLevel=…, chn_id=[2])`
  changed only the pan/tilt lens, and the same call **without `chn_id` changed only lens 1**
  (the wide lens); setting the intensity left the force time alone. *C545D, each step read
  back, n=1 run.* `setForceWhitelampState(…, chn_id=[…])` uses the same per-lens form
  (not tried).
- **Per device** (`-40101` with `chn_id`): lens mask (privacy), video qualities,
  night-vision capability, target track, presets. Record plan, message
  push and alarm config exist only as `chn1_…` names (`chn2_…` → `-40101`).
  `getWhitelampStatus` with `chn_id` gives `-40106` (see §3).
- Dual-lens specific: `getDualCamCapability` (`{"image_capability": {"name":
  ["dualCam"]}}` → `zooms: ["1.0x", "5.0x"]`), `getDualCamLinkage` (`{"dual_cam_linkage":
  {"name": "linkage_state"}}` → `{"enabled": "on", "linkage_type": 0}`; the firmware turns
  the pan/tilt lens toward a target the wide lens detected), `getLinkageTargetCapability`
  and `getLinkageTargetSetting` (people / pet / vehicle switches).
- Method names that do **not** exist (`-40210`): `getMultiLensCamCapability`,
  `getDualCamLinkageCapability`, `getPanoramicViewConfig`, `getPanoramaInfo`,
  `getMotorCapability`.
- RTSP and ONVIF address the lenses differently (per-lens RTSP paths; ONVIF media shows
  only the wide lens while ONVIF PTZ moves the pan/tilt lens) — see
  [capabilities §7](capabilities.md#7-dual-lens-cameras-tapo-c545d).

## 8. Face recognition at API level

Seen on the C260 and C560WS; the C545D has no face detection (`-40101`).

- **Config** — `getFaceDetectionConfig` with `{"face_detection": {"name":
  ["detection"]}}` (other sub-keys → `-40101`):

  ```json
  {"face_detection": {"detection": {
    "enabled": "on", "sensitivity": "100",
    "tags": ["family", "friend", "courier", "neighbor", "colleague", "schoolmate", "others"],
    "max_familiar_face_num": "20", "max_stranger_face_num": "30", "max_sub_face_img": "5",
    "hub_face_ai_enhance": "0", "last_device_mac": "<MAC>"}}}
  ```

  `last_device_mac` was all `F` on a camera that never had a face enrolled and a real
  MAC on one that had; it looks like the device that last wrote to the gallery.
- **In events** — recognised faces show up in `searchDetectionList` rows as
  `"event_info": [{"face_id": <integer>}]`; an empty list means no face matched. The id
  stayed the same for the same person across events, and one event can carry several
  ids. The names live in the cloud/app, so map ids to names yourself.
- **Face alerts** are a separate switch: `face_detection` in `getAlertEventType`. With it
  off, `face_id`s still appear in events.
- **No local gallery access.** Every gallery/list method we tried (`getFaceManagement`,
  `searchFaceList`, `getFaceDB`, `getFaceList`, …) returned `-40210` (see the note under
  §3).
- `setFaceDetectionConfig` accepted unknown fields with `error_code: 0` and kept only the
  known ones — the general setter pattern from the warning box.

## 9. Methods pytapo does not wrap (or wraps differently)

| Method | Params | Seen result |
|---|---|---|
| `getAppComponentList` | `{"app_component": {"name": "app_component_list"}}` | component names and versions (C545D) |
| `getLastAlarmInfo` | `{"system": {"name": ["last_alarm_info"]}}` | last alarm type and time (§6, C545D) |
| `searchEventList` | §5 | every event clip (C545D, C560WS) |
| `getSmartTrackCapability` | `{"smart_track": {"name": "smart_track_capability"}}` | `people/pet/vehicle/baby_support` (C545D) |
| `getWhitelampCapability` | `{"image_capability": {"name": ["supplement_lamp"]}}` | same answer as `getNightVisionCapability` (C545D) |
| `getAudioCapability` | `{"audio_capability": {"name": ["device_speaker", "device_microphone"]}}` | codecs, sample rates, noise cancelling, echo cancelling, half duplex (C545D) |
| `getAlertConfig` + `capability` | `{"msg_alarm": {"name": ["chn1_msg_alarm_info", "capability"]}}` | limits for user-defined alarm sounds (count, max seconds) (C545D) |
| `getAlertEventType` | `{"msg_alarm": {"table": "msg_alarm_type"}}` | per-type alert switches (C560WS, C545D); called with `{}` it returns `{}` |
| video encoder write | `{"method": "set", "video": {"main": {"smart_codec": "on", "frame_rate": "65551"}}}` | a **write**; `setVideoQualities` does not exist (`-40210`). Frame-rate codes 65551 / 65556 / 65561 = 15 / 20 / 25 fps. Applied live without breaking a running RTSP recording (C560WS, n=1) |
| `getWhitelampStatus` / `reverseWhitelampStatus` | `{"image": {"get_wtl_status": ["null"]}}` | `{status, rest_time}`; the reverse call is a **toggle** (warning box). A transient `-40214` can follow a switch (C560WS) |
| getters from the C225 family: `getSmartAE*`, `getImageStyle*`, `getLightSensor*`, `getDnSwitchMode*`, `getExpConfig`, `getPrivacyZoneConfig`, `getGlobalDetectionRegion`, `getTapoDetectionRegion`, `getSmartInfLampConfig`, `getSmartWhitelampConfig`, `get*DetectionConfig` for LPR / glass / bark / meow / package | varies | answered on the C560WS, many with `{}`. **Not probed on the C545D** after `getInfLampCapability` from the same family took its API down |

## 10. Contributing findings

This page grows from other people's cameras as much as ours. If you see a model or
firmware behave differently, or confirm something marked unconfirmed here:

1. [Open an issue](https://github.com/PeterkoCZ91/tapo-monitoring/issues/new) with the
   **model**, **hardware version** and **firmware** (`getBasicInfo` → `device_model`,
   `hw_version`, `sw_version`) and the pytapo version.
2. Paste the **request params** and the **response**, redacted: remove MAC addresses,
   serials, device ids (`dev_id`, `chn_dev_id`, `oem_id`, `hw_id`), `face_id`s, IP
   addresses, Wi-Fi names, user names and passwords. Keep the structure and the error
   codes — those are what matter.
3. Say how many times you saw it and whether you could reproduce it.
4. If a call made the camera drop off the network, say so first — those findings go
   straight into the warning box.

Please probe read-only, one method at a time, a few seconds apart, and never try a
method listed in the warning box on a camera you depend on.
