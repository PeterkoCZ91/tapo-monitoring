# Tapo C260 Local Test Plan

Purpose: use the local/home Tapo C260 as a controllable lab camera for face-recognition and ONVIF event research, without risking the production C560WS night monitoring setup.

Official TP-Link/Tapo material describes C260 as a 4K pan/tilt AI camera with human face recognition, local AI processing, face management, event retrieval, and ONVIF support. This makes it a better bench device for face experiments than the remote C560WS, because you can physically trigger it.

Sources:

- https://www.tp-link.com/us/home-networking/cloud-camera/tapo-c260/
- https://www.tp-link.com/uk/products/details/tapo-c260.html
- https://static.tp-link.com/upload/product-overview/2025/202501/20250124/Tapo%20C260%201.0_Datasheet.pdf

---

## Safety Boundary

The C260 should be treated as a lab camera:

- Do not point it at private spaces while dumping raw discovery files.
- Do not commit raw JSON/JSONL captures.
- Use a separate env file from production C560WS.
- Prefer read-only probes first.
- Only toggle camera config in short, reversible windows.

Production C560WS config remains in:

```text
tapo-camera.env
```

C260 lab config should be:

```text
tapo-c260.env
```

Use `tapo-c260.env.example` as the template.

---

## Bring-Up Checklist

1. Add the C260 in the Tapo app.
2. Enable ONVIF/RTSP account in the Tapo app if required.
3. Put the camera on the same LAN/Tailscale-reachable network as the machine running discovery.
4. Find its IP from router/Tapo app/network scan.
5. Create `tapo-c260.env` from `tapo-c260.env.example`.
6. Run read-only probes.

### Port/API Preflight

Before drawing conclusions about face/person events, verify the actual camera surface. C260 firmware and app settings can change whether ONVIF/RTSP are reachable, and old lab notes may no longer match the current state.

Use a quick port preflight from the same machine that will run discovery:

```bash
nmap -p 80 <C260_IP>
nmap -p 2020 <C260_IP>   # ONVIF
nmap -p 554 <C260_IP>    # RTSP
```

Interpretation:

- `2020/tcp open` means ONVIF discovery is worth running.
- `554/tcp open` means snapshot/stream validation can be tested.
- Closed ONVIF is a valid result; record it and continue with local Tapo API/app behavior instead of repeating ONVIF captures.

Run the read-only API probe with event getters included:

```bash
python3 scripts/tapo_api_probe.py \
  --env-file ./tapo-c260.env \
  --call-safe-defaults \
  --out /tmp/tapo-c260-api-probe.json
```

Check these fields first:

- `getAlertEventType`: whether `people_detection`, `motion_detection`, `vehicle_detection`, `face_detection`, and related alert types are enabled.
- `getPersonDetection`: whether person detection itself is enabled.
- `getPrivacyMode`: if privacy mode is enabled, physical event tests may produce no useful video/event signal.
- `getEvents`: whether the camera exposes recent event history through local API.


### Gentle Probe Rules

The C260 local Tapo API can become unstable when several `pytapo` sessions or raw probes run in parallel. Keep lab probing conservative:

- Run only one Tapo API probe at a time.
- Do not run `tapo_api_probe.py` and `tapo_raw_probe.py` in parallel against the same camera.
- Keep at least 2 seconds between raw API calls.
- Start with `--max-calls 4`, then expand if the camera stays responsive.
- If the API returns `Invalid authentication data` after successful earlier probes, stop for a minute or reboot the camera; do not keep retrying rapidly.

Gentle raw probe example:

```bash
python3 scripts/tapo_raw_probe.py \
  --env-file ./tapo-c260.env \
  --hours 3 \
  --delay 2 \
  --max-calls 4 \
  --out /tmp/tapo-c260-raw-probe.json
```

Full raw probe should only be run when the camera is stable:

```bash
python3 scripts/tapo_raw_probe.py \
  --env-file ./tapo-c260.env \
  --hours 3 \
  --delay 2 \
  --out /tmp/tapo-c260-raw-probe.json
```

Example ONVIF discovery command:

```bash
python3 scripts/event_discovery.py \
  --env-file ./tapo-c260.env \
  --duration 300 \
  --capabilities \
  --out /tmp/tapo-c260-onvif-triggered.jsonl
```

---

## Current C260 Lab Observation

As of 2026-06-01, the local C260 lab camera was reachable and useful for discovery:

- ONVIF port `2020/tcp` was open.
- RTSP port `554/tcp` was open.
- `pytapo` read-only probe connected successfully.
- ONVIF PullPoint subscription was created successfully with no authentication error.
- A short passive ONVIF window produced no events; this only means no event was observed in that window.
- API probe reported `face_detection` alert disabled, while people/motion/pet/vehicle/line-crossing style alert types were enabled.
- API probe reported privacy mode enabled, which should be checked before physical trigger tests.

Do not treat a quiet passive dump as proof that person/face events are unavailable. The useful test is a timed physical trigger while the camera is out of privacy mode and the Tapo app is watched in parallel.

---


### 2026-06-01 Trigger Findings

After privacy mode was turned off, C260 produced useful lab signals:

- ONVIF PullPoint emitted many messages, but they were `PropertyOperation=Initialized` records with no topic and no `SimpleItem` values. ONVIF proves liveness, but did not expose semantic person/face labels in this test.
- Local Tapo API `getEvents()` / raw `searchDetectionList` returned detection history with `alarm_type=6` and `events_1` bitmasks.
- Some `searchDetectionList` rows contained `event_info` face entries. Face IDs are treated as private identifiers and should be redacted; for public/debug summaries keep only `event_info_count` and `has_face_info`.
- `getFaceDetectionConfig` is readable and reports face detection enabled with familiar/stranger capacity limits.
- Obvious face library methods such as `getFamiliarFaceList`, `getStrangerFaceList`, `getFaceList`, `getFaceInfo`, `getFaceImage`, `searchFaceDetectionList`, and `searchFaceRecognitionList` returned `METHOD_DO_NOT_EXIST` on C260 firmware tested here.

Conservative conclusion: C260 exposes face-related event presence locally through detection history, but not a known/stranger face library through the obvious raw method names. The next useful step is empirical mapping of `alarm_type`, `events_1`, `event_info_count`, and Tapo app timeline during controlled triggers.


### Post-Restart Gentle Probe Results

After the camera reboot, a paced raw probe was successful with `--delay 3` and small call batches:

- Batch 1, calls 1-4: `getFaceDetectionConfig`, `getAlertEventType`, `getLastAlarmInfo`, `searchDetectionList` all succeeded.
- Batch 2, calls 5-11: person, motion, vehicle, pet, tamper, and line-crossing configs succeeded. `getPackageDetectionConfig` returned `-40101` / parameter does not exist.
- Batch 3, calls 12-19: obvious face-list methods returned `METHOD_DO_NOT_EXIST`.

Useful C260 data currently available through local API:

- face detection config: enabled, sensitivity, tags, familiar/stranger capacity limits,
- alert event type state: people/motion/etc. alert flags plus `face_detection` alert flag,
- detection history via `searchDetectionList`, including `alarm_type`, `events_1`, derived bit indexes, and whether `event_info` contains face records,
- baseline detection config for person/motion/vehicle/pet/tamper/line-crossing.

Not currently available through obvious local method names:

- familiar face list,
- stranger face list,
- known/unknown label for a detected face,
- direct face image retrieval by obvious method name.

Important privacy note: `event_info` can contain stable face identifiers. Probe outputs must keep `face_id` and full `event_info` redacted. Public examples should show only `event_info_count` and `has_face_info`.

## Trigger Protocol

Run the ONVIF discovery command, then perform these actions in front of the camera:

1. Empty scene for 20 seconds.
2. Walk into frame without looking at the camera.
3. Face the camera directly for 10-20 seconds.
4. Walk out of frame.
5. Repeat with face known in Tapo app.
6. Repeat with a different/unregistered face if available.

Keep the Tapo app open during the run and note whether it shows person, motion, face, known-face, or stranger-face activity. Record a simple manual timeline in a text file, for example:

```text
00:20 entered frame
00:35 faced camera
00:55 left frame
01:30 known face entered
02:10 unknown face entered
```

This makes raw ONVIF timestamps interpretable.

After the run, record the result in this compact form:

```text
ONVIF available: yes/no
RTSP available: yes/no
Tapo app showed person event: yes/no
Tapo app showed face event: yes/no
ONVIF event observed: yes/no
ONVIF face/known/stranger metadata observed: yes/no
Local getEvents returned entries: yes/no
Privacy mode during test: on/off
Notes:
```

---

## What We Want To Learn

### ONVIF

Does C260 emit any of these in `Topic`, `SimpleItem`, or raw payload?

- `IsPeople`
- `IsMotion`
- `IsFace`
- `FaceDetection`
- `FamiliarFace`
- `StrangerFace`
- `HumanFace`
- line crossing / region / smart event labels

### Local Tapo API

Does C260 expose richer methods than C560WS?

Known useful probes:

```python
cam.getAlertEventType()
cam.executeFunction("getFaceDetectionConfig", {"face_detection": {"name": ["detection"]}})
cam.getEvents()
```

Unknown method families to test carefully:

```text
getFaceDetectionConfig
getAlertEventType
searchDetectionList
getEvents
possible face list / face library methods
```

### Tapo App Behavior

If the app shows known/stranger face events but local API/ONVIF does not, then face identity may be app/cloud/hub-only or behind undocumented method names.

---

## Expected Outcomes

Best case:

- ONVIF includes face/known/stranger metadata.
- We can add low-noise known/unknown alerts without cloud reverse engineering.

Good case:

- Local Tapo API exposes face event history via `getEvents()` or a discovered method.
- We can poll face events periodically.

Acceptable case:

- Only generic face detection config is exposed.
- We still use face config as diagnostics and rely on our OpenCV/Groq validation pipeline.

Worst case:

- Face recognition results are app/cloud-only.
- Further extraction requires Tapo app traffic research and stronger privacy handling.

---

## How This Feeds The Project

If C260 exposes useful face metadata, we can add:

- camera capability profiles (`C560WS`, `C260`),
- `probe-camera` report command,
- optional face event parser,
- Telegram `/status` with face/person/vehicle/pet detection state,
- GitHub-ready docs showing how to discover capabilities on any Tapo camera.
