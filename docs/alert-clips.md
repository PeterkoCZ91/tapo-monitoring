# Alert video clips

A photo says that someone was there; a short clip shows what they did. With `clip_video:
true` a camera sends a video of the incident to Telegram as a **reply to the alert photo**.
The feature is off by default and never delays or replaces an alert: the photo goes out
first, and the clip follows once the footage is available.

## How it works

1. An alert photo is delivered to Telegram. Only a photo Telegram really accepted queues a
   clip, once per incident (the incident id carries the event's start time). A camera that
   only collects data (`telegram_alerts: false`) sends no photo and therefore no clip.
2. The clip is due when its window can be read (see the table below). The window starts
   `clip_pre_roll` seconds before the event and ends `clip_seconds` seconds after its
   start.
3. The footage is read from one of two sources, then transcoded with `ffmpeg` to H.264 at
   `clip_height` lines (first video stream only, audio when the source has it) and sent
   with `sendVideo`. The caption is the photo's caption prefixed with a film-clip emoji.

| Source | Chosen when | Footage comes from | Due |
| --- | --- | --- | --- |
| **Camera card** | `clip_url` is not set; the camera needs `detection.sources` with `getevents` | The camera's SD card, read in a fresh subprocess like the card follow-up photo | `clip_seconds` + `sd_fresh_guard` + 9 s after the event start (about 100 s with the defaults) |
| **HTTP recorder** | `clip_url` is set | `GET` on your own recorder service | `clip_seconds` + 10 s after the event start |

The card read runs on the camera's card thread after that camera's SD follow-ups, so an
alert photo never waits behind a clip. The encode and the upload run on a separate sender
thread, so the main loop spends neither. With the defaults the clip reaches Telegram
about three minutes after the event (a card read, an encode and a small upload; one
measured example on a Raspberry Pi 4 with a 2K camera is in
[configuration](configuration.md#live-sd-and-local-recorder-media)).

## Minimal configuration

A camera with a readable SD card:

```yaml
cameras:
  - name: front
    host: 192.0.2.10
    telegram_alerts: true
    detection:
      sources: [getevents]
    clip_video: true
```

A camera without a readable card, with a recorder that serves the clip over HTTP:

```yaml
cameras:
  - name: yard
    host: 192.0.2.20
    telegram_alerts: true
    detection:
      sources: [reolink]
    clip_video: true
    clip_url: https://recorder.example.invalid/clip
    clip_token_env: YARD_CLIP_TOKEN    # optional; the environment variable holds the token
```

Other keys a camera needs (credentials, `reolink_triggers` and so on) are omitted here.
`clip_url` works with the `reolink` and `lastalarm` sources, which have no readable card.

`ffmpeg` must be on the monitor host (it already is for the live snapshot).

## Options

All options are per camera.

| Option | Default | Range | Meaning |
| --- | --- | --- | --- |
| `clip_video` | `false` | boolean | Send a clip of each alerted incident as a reply to its photo. |
| `clip_seconds` | `30` | 5-60 | Seconds of footage after the event start. |
| `clip_pre_roll` | `2` | 0-10 | Seconds of footage before the event start. |
| `clip_height` | `720` | 240-1080 | Output height in lines; the width follows the aspect ratio. |
| `clip_url` | unset | `http://` or `https://` URL | A recorder that serves the clip; replaces the card as the source. |
| `clip_token_env` | unset | environment variable name | Its value is sent as `Authorization: Bearer <value>` to `clip_url`. |

The clip is `clip_pre_roll` + `clip_seconds` long (32 s with the defaults). `clip_url` is
checked at load even while `clip_video` is `false`, but is only used when it is `true`.
The token itself never goes into `cameras.yaml`; put it in the environment file the unit
reads. An unset or empty variable means no `Authorization` header is sent.

Invalid values stop the load with a `ConfigError`:

- `<where>: 'clip_seconds' must be an integer` (also for `clip_pre_roll`, `clip_height`;
  a boolean is not an integer)
- `<where>: 'clip_seconds' must be between 5 and 60` (`0 and 10`, `240 and 1080` for the
  other two)
- `<where>: 'clip_url' must be an http:// or https:// URL`
- `<where>: 'clip_token_env' must be an environment variable name` (only the type is
  checked: the value must be a string; an unset or empty variable simply means no
  `Authorization` header is sent)
- `<where>: clip_video reads the camera's SD card and needs detection source 'getevents',
  or a recorder in 'clip_url'`

`<where>` names the camera entry.

## The recorder contract

With `clip_url` the daemon sends one request once the window has passed:

```text
GET <clip_url>?start=<epoch>&pre=<clip_pre_roll>&seconds=<clip_seconds>
Authorization: Bearer <value of clip_token_env>     (only when the variable is set)
```

- `start` is the event start in epoch seconds. For a push source it is the daemon host's
  clock, so keep the recorder host on NTP and leave a few seconds of `clip_pre_roll`.
- The answer must be an mp4 body. Anything over 200 MB or an empty body counts as no clip.
- The optional response header `X-Clip-Start` gives the epoch of the file's first frame,
  and the daemon seeks to `start - pre` inside the file. Without the header it encodes from
  the file's first second, so a recorder that returns a window beginning earlier than the
  event should send it.
- The recorder may hold the answer until the footage is on disk; the socket timeout of the
  request is `clip_seconds` + 60 s for each operation (connect, read), not a limit on the
  whole download.
- The daemon transcodes the file like a card clip (same height, same limits).

This repository does not ship a recorder service; any HTTP service that implements the
contract works.

## Failure behaviour

| Situation | What happens |
| --- | --- |
| Card unreadable, no recording of the window, or the window is shorter than 3 s | The read fails; one retry after 60 s, then the clip is dropped. The photo is unaffected. |
| Recorder unreachable, HTTP error, empty or oversized body | Same: one retry after 60 s, then dropped. |
| Transcode fails or times out (180 s) | The try counts as failed; same retry rule. |
| Clip is empty or larger than 45 MB | Not sent; counts as a failed try. |
| Telegram does not accept the upload | Sent again once after a short pause, then counted as a failed try. |
| Entry older than 15 minutes, or its camera removed from the configuration | Dropped without a try. |
| Monitor restarts | Queued clips are lost (the queue is in memory). Alert photos are not affected. |
| Photo not delivered | No clip is queued for it. |

At most two tries are made per incident. A clip that arrives after the photo was deleted is
still delivered, only not as a reply.

## Checking that it works

Look for these lines in the monitor log (the examples use the camera name `front`):

```text
SD clip start front: at=... timeout=...s          card read started
SD read front: window=32s offset=... lead=2 ...   card read finished; window and bytes
clip front: sent front-1767000000 (1048576 bytes) the clip reached Telegram
```

Problems log at warning level:

```text
clip front: no card read for <incident>            the source returned nothing
clip front: card read failed for <incident>: ...
clip front: recorder answered no clip: ...         (clip_url cameras)
clip front: recorder sent an empty clip
clip front: transcode failed for <incident>: ...
clip front: <n> bytes for <incident>, not sent
clip front: not delivered <incident> (<n> bytes)   Telegram refused it
clip front: giving up on <incident> (...)          second failed try
clip front: dropped <incident> (too old)           or (camera gone)
SD clip subprocess exit=...; stderr: ...           card read failed in the subprocess
SD clip subprocess returned no clip; stderr: ...
```

Quick test: enable `clip_video` on one camera, trigger an alert, and expect the clip about
three minutes later. `journalctl -u <unit> | grep -E "clip |SD clip"` shows the sequence.

## Troubleshooting

- **No clip appears and nothing is logged.** The photo was not delivered, the camera is
  collect-only, `clip_video` is not `true` on that camera, or the incident has no start
  time in its id. Check the alert photo first.
- **`no card read` or `SD clip subprocess returned no clip`.** The camera listed no recording
  for the window. Check that the card is present and recording on events, see
  [SD-card download returns no frames](troubleshooting.md#sd-card-download-returns-no-frames).
  An event at the very start of a recorded segment loses its pre-roll, which is expected.
- **The clip is cut short.** `sd_fresh_guard` is set too low: the camera lists a segment
  while it is still recording but about 10-20 s behind. Keep the default 60.
- **The clip starts at the wrong moment (recorder).** The recorder host clock differs from
  the monitor's, or the recorder does not send `X-Clip-Start`. Fix NTP and raise
  `clip_pre_roll`.
- **The recorder log shows no request.** The daemon asks only after `clip_seconds` + 10 s;
  check `clip_url` and that the incident was alerted (a photo reached Telegram).
- **`recorder answered no clip: HTTP Error 401`.** The bearer token is wrong, or the variable
  named in `clip_token_env` is not in the unit's environment.
- **Transcode fails.** Check that `ffmpeg` is installed and has the `libx264` encoder
  (`ffmpeg -encoders | grep libx264`).
- **The clip is late on a slow host.** The Pi-class encode is the largest share after the
  card read; lower `clip_height` or `clip_seconds`.
- **Clips are missing after a restart.** The clip queue is not persisted.

Telegram's bot upload limit is 50 MB; clips over 45 MB are never sent. A 30 s clip at the
default height is a few megabytes.
