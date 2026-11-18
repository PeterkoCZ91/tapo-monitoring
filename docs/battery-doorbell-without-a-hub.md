# A battery doorbell without a hub (Tapo D205)

Notes from probing one Tapo D205 battery video doorbell, firmware **1.2.8 Build 260514**,
not paired to any hub. Everything below was observed on that single device and firmware;
other doorbell models, or later firmware, may behave differently. `tapo_monitor` has no
doorbell support yet — this page records what the local API offers, so that support can
be built on measured behaviour rather than guesses.

Addresses use placeholders (`<DOORBELL_IP>`); device ids, MACs and the like are the
operator's and stay out of this repository.

Compare [battery cameras on a hub](battery-cameras-on-a-hub.md): those cameras keep no
event index of their own, so the hub is the event source. The D205 is the opposite case —
with no hub, the doorbell itself has to be queried, and it is asleep most of the time.

## What the local API exposes

The D205 speaks the SslAes ("SMARTCAM") API on port 443, with the same camera account
as the other Tapo cameras. A sweep of read-only `get*` methods answered about two thirds
of them. The useful ones:

- **Battery:** capacity, charge level, voltage, temperature, a 30-day statistic, and the
  power mode (`battery_life_first` here, with a 20 s clip, 10 s retrigger and 6 s
  pre-buffer). The power source can be battery, adapter or solar.
- **Detection:** motion, person and PIR, each with a sensitivity. Vehicle, package and
  tamper detection are not supported on this model.
- **Video:** 2304x1296 H264 main stream plus a 640x360 sub stream; IR night vision, no
  white light.
- **SD card:** status and capacity. Recordings go to the card (see below).
- **Ring:** `getRingStatus` reports only whether ringing is enabled.

Component list highlights (`getAppComponentList`, 46 components on this firmware):

| component | version | note |
|-----------|--------:|------|
| `noHubBatteryCam` | 1 | the app treats the device as a low-energy camera without a hub |
| `ring` | 4 | |
| `quickResponse` | 3 | |
| `wakeUp` | 2 | |
| `pir` | 3 | |
| `battery` | 3 | |
| `snapshot` | 2 | in the app this means "can set the detection type", **not** a photo capture |
| `recordDownload` | 2 | |

### Undocumented read-only methods

These methods are in the Tapo Android app's method list and were verified to answer on
the D205. Use the namespace-shaped params shown; do not send `{}`.

```jsonc
getLastBatteryUseTime  {"battery": {"name": "battery_use_time"}}
// → last_battery_use_time, alarm_event_num
//   alarm_event_num is an event counter: a cheap "has anything new happened?" check

getRingSoundConfig     {"ring": {"name": "config"}}
// → the ring tone and its volume

getMsgPushEventList    {"msg_push": {"table": "msg_push_event"}}
// → which event classes send a push notification (e.g. person on, motion off)
```

`getCallLog` and `getCallStateInfo`, which look like they would report rings, are not
supported (`METHOD_DO_NOT_EXIST`).

## It sleeps

The D205 spends most of its time asleep with the radio off. With the doorbell idle it
answered about one ping in thirty. It wakes on its own for a PIR detection or a ring
(`wake_up_type: detection`), stays awake while the event lasts, and goes back to sleep
seconds later.

Two measured facts shape any client:

- **ICMP ping does not wake it, and it answers only while awake.** A ping every second over
  several minutes of quiet got no reply at all; when someone walked past, replies started
  about 4 s after the event began and stopped about 20 s later as it fell asleep again.
  So a 1 s ping is a free wake detector: it costs the doorbell nothing and tells you the
  moment it is up. (One ~20 s awake window with no event was seen, presumably an upload or
  keepalive.)
- **A TLS login to port 443 does wake it.** A login sent just after it fell asleep woke it
  again, and a TCP probe every second kept it bouncing between awake and asleep. Do not
  poll the API on a timer; act immediately when the ping says it is awake, while the
  window lasts. A login takes about 2 s.

No network traffic from the doorbell was visible to a passive listener on the LAN when it
woke, so there is nothing to sniff instead of pinging.

## No RTSP, no doorbell broadcast

- **Port 554 is closed.** There is no RTSP stream. Live view and recording download go
  over the media port **8800**, the same download-type media session used for hub clips.
- **No UDP 20005 broadcast.** [Home Assistant Tapo Control](https://github.com/JurajNyiri/HomeAssistant-Tapo-Control)
  offers a doorbell sensor fed by a UDP broadcast on port 20005 for other doorbells. A
  capture on the same LAN segment while ringing this D205 (fw 1.2.8) saw no UDP from it at
  all. A ring has to be read from the event list instead.

## Events

`getEvents` in pytapo (the `searchDetectionList` method) works on the doorbell itself,
with the same event shape as the C-series cameras, **once the SD card holds recordings**.
Before the first recording the index is empty and the call fails with
`PLAYBACK_SEARCH_FAILED` (`-71105`) or `STORAGE_NOT_EXIST` — an empty index, not a broken
card.

Observed events:

| what happened | `alarm_type` | `events_1` | codes |
|---------------|-------------:|-----------:|-------|
| motion | 2 | 2 | 2 motion |
| person walking past | 6 | 34 | 2 motion, 6 person |
| ring | 17 | 66050 | 2 motion, 10 ring, 17 ring missed |
| ring | 18 | 131618 / 131586 / 131584 | (2 motion, 6 person,) 10 ring, 18 ring answered |

Every ring carries code 10 (bit 9) plus 17 or 18; the meanings come from the app's
playback event list, see [the `events_1` bitmask](events1-bitmask.md). A ring event lasts
about 30 s, a motion event 10–20 s, and an event appears in the list within about a
minute.

**Start times shift by about one second between queries** of the same event. Deduplicate
with a tolerance of a couple of seconds, not on an exact `start_time`.

## SD recordings

The day's recordings come from pytapo `getRecordings("YYYYMMDD")` (the
`searchVideoOfDay` method); `getRecordingsList` returns the days that hold footage. Clips
are 10–30 s and their start times match the events.

A clip downloads over port 8800 with pytapo's `Downloader`. Measured: a 30 s clip,
2304x1296 H264 with AAC audio, about 4 MB, in about 5 s while the doorbell was awake.

## Putting it together

What these measurements suggest for a client, not yet implemented:

1. Ping the doorbell every second; it costs nothing.
2. On the first reply, log in at once and hold the session while it is awake.
3. When the event ends, read `getEvents` and `getRecordings`, download the clip, and
   take the alert frame from it.

Still open: how much battery each awake session costs, and whether `alarm_event_num` can
replace reading the event list on every wake.

## Using it with tapo-monitor

The `batterywake` detection source puts the steps above into the monitor. It is opt-in
and has no effect on other cameras.

```yaml
cameras:
  - name: front-door
    host: 192.0.2.50
    user_env: DOORBELL_USER           # Tapo account e-mail (env var name, not the value)
    password_env: DOORBELL_PASSWORD   # Tapo account password
    detection:
      sources: [batterywake]
    battery_wake:
      ping_interval: 1.0   # seconds between pings (number above 0)
      event_wait: 75       # seconds to wait after a wake for the event to end (min 20)
      frames: 6            # frames cut from the clip for scoring
      send_clip: true      # send the clip as a reply to the photo
```

All four `battery_wake` options are shown with their defaults; leave the block out to use
them. `battery_wake` is rejected on a camera that does not use the source.

How it behaves:

- The monitor only pings the camera. It never logs in while the camera sleeps, so it
  does not wake it.
- When the camera becomes reachable after being unreachable, the monitor logs in once,
  reads the events since the last one it handled, downloads their SD clips and cuts
  `frames` frames from each. That one session is the only login per wake.
- If an event is still running, the monitor keeps asking until all listed events have
  ended or `event_wait` runs out; events still running then are handled on the next
  wake.
- A ring is always sent. It bypasses the scorer threshold, the alert cooldown and
  `night_only` / quiet hours. The scorer only picks the best frame and puts its score on
  the caption; without any frame the ring goes out as text. The caption says whether the
  ring was answered or missed.
- Other events are scored like a hub clip: a person above the threshold is sent,
  vehicles and motion-only events are dropped, as are events outside the camera's alert
  window.
- With `send_clip: true` the clip follows the photo as a reply.
- When the clip cannot be fetched or yields no frame, the event's own snapshot is used
  instead. If no frame can be had at all, a ring is still sent as text and anything else
  is skipped.
- The cursor (the start of the last handled event) is saved with the runtime state, so a
  restart does not repeat events. With no cursor yet it is set to now and nothing older is
  replayed.
- Audit and sent-log rows use the delivery path `batterywake`, and `tools/soak_report`
  lists it next to the other paths.

Not available with this source: `clip_video`, `sd_snapshot` and `mac` are rejected, and
`batterywake` cannot be combined with any other detection source.

Limits:

- Wake detection needs the host running the monitor to reach the camera by ping. A camera
  behind a subnet route or a firewall that drops ICMP will never look awake.
- Anything else that wakes the camera (opening the app, another login) is noticed as a
  wake too and triggers a login; keep other clients off it.
- A long session keeps the camera awake and warms it up. A large `event_wait` or slow
  clip downloads lengthen the session.
- A camera with no saved cursor (first run, or runtime state lost) starts from now: events
  that happened before are not replayed. After a normal restart the saved cursor is used,
  so events that ended while the monitor was down are collected on the next wake.
