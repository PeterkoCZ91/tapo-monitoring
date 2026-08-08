**Thread title:**
tapo-monitor — the alert photo actually has the person in it: local-first detection for Tapo cameras on a Raspberry Pi

**Category:** Share your Projects!

---

Hi all 👋

If you own a Tapo camera you know this notification: *motion detected*, and the photo is an
empty driveway. The person who triggered it walked out of frame before the snapshot was
taken, because the grab happens at motion-*start* and the image is fetched a second or two
later. You get an alert and no idea whether it mattered.

**tapo-monitor** (MIT, Python) is a daemon that fixes that, and then does the rest of the
job locally. It has been running continuously on a handful of cameras for a few months.

**Repo:** https://github.com/PeterkoCZ91/tapo-monitoring — currently v0.4.0

### The bit I think is genuinely new

When the live frame comes back empty, the daemon does **not** grab another live frame.
It goes into the camera's **own SD-card recording** and pulls frames from the event's own
start/end timestamps — footage the camera recorded *before the daemon even knew the event
existed* — then sends the one the subject is actually in.

That "before we knew" part is the whole trick. Every tool that starts recording when the
notification arrives is already too late for the first seconds, which is exactly where the
subject usually is. If you've tried the ONVIF-based scripts around here, this is the
limitation they run into.

### The camera's AI is a trigger, not a verdict

These cameras will happily report "person" for a parked car, a swaying branch, or an empty
yard at 3am under IR. So the camera event only starts the pipeline — a small self-hosted
**YOLO HTTP service on your LAN** decides whether an alert goes out. Nothing leaves the
network for that decision.

```
getEvents (events_1 bitmask) → motion / PIR / person
        ↓
local YOLO scorer  →  person score below threshold? drop
        ↓
live frame empty?  →  frames from the SD recording around the event's
                      own timestamps, send the one with the subject
        ↓
Telegram alert (+ optional scene caption)
```

### It runs on hardware you already have

Because nothing decodes video until the camera says something happened, the footprint is
small: the fleet includes a **Pi Zero 2 W**, and a Pi 4 handles a camera comfortably with
room to spare. There is no continuous stream decoding and no accelerator.

### Other things it does

- **On-device AI detection** via `getEvents`, decoding the `events_1` bitmask into named
  signals (motion / PIR / person). No cloud round-trip.
- **PTZ**: people-only SmartTrack, presets, astral (sunset/sunrise) day/night scheduling,
  rain-aware sensitivity and parking via a weather API.
- **Forced IR night mode** for sharper night frames, **tiled scoring** so distant people
  aren't lost in a wide view, **crop-to-subject zoom** (optionally cut from a
  native-resolution grab, so a distant figure stays legible, and shaped to the scene's own
  aspect ratio so a standing figure doesn't arrive as a vertical sliver), and an **ONVIF
  soft pan limit** — the local Tapo API has no pan limit, but ONVIF exposes position, so
  auto-track can be held within your presets instead of swinging into a wall.

### The reliability layer — where most of the recent work went

Running this for real surfaced failures I hadn't designed for, and each one became a feature:

- **Empty-scene night false positives** → marginal motion is *held* until a second frame
  corroborates it. Camera-confirmed people and PIR-backed motion are never delayed by this.
- **A daemon that broke and went quiet for hours** → the per-camera watchdog ran *inside*
  the main loop, so a fault in the loop silenced the very alerting meant to report it.
  There is now a dead-man's switch in the outer loop: if every tick has raised for 15
  minutes you get a 🔴, and a 🟢 when it recovers. Silence should never be
  indistinguishable from a calm night.
- **"Why did it send that?"** → an opt-in archive keeps the frames that went out *and* the
  ones corroboration suppressed, each line carrying the camera and the scores behind it, so
  tuning a threshold is a data question, not a guess. A camera that sends a zoom archives
  the *uncropped* scene — a cropped empty yard tells you nothing about a false positive.
- **A feature that quietly filled `/tmp`** → the native-resolution crop hands the full-size
  original along with the delivery-size frame, and one of the three send paths cleaned up
  only the frame it could see. 300 MB of orphaned 4K JPEGs in a day and a half, on a
  tmpfs. Reviewing the *archived photos* is what surfaced it — the same review showed that
  same path was never cropping at all, so it paid for the detail and threw it away.
- Plus ICMP preflight that tells a dead network from a locked-out login, outage/recovery
  notices with durations, and opt-in camera config-drift detection.

### What it is not

It is **not a Frigate replacement**. Frigate is an NVR with zones, a UI, recordings and a
mature HA integration; if you have the hardware for it, it does far more. This is the
opposite trade: no UI, one vendor, Telegram out — in exchange for running on a Pi Zero and
leaning on detection hardware you already paid for inside the camera.

### Privacy

Everything is local **except one optional feature**: short scene captions use Groq's cloud
vision API, and it can be turned off entirely. The local scorer is what gates alerts. No
dependency on the vendor cloud.

### Where Home Assistant fits — and where I'd love help

Honestly: right now it is a **standalone systemd daemon**, not an HA integration. The
existing Tapo integration is a control and streaming layer — it does not do detection
analytics, photos or false-positive filtering — so there is a real gap rather than a
duplicate.

The next step is an **MQTT / MQTT-discovery bridge** so detections land as HA entities and
automations. It is on the roadmap and I would genuinely welcome it as a contribution: the
internals already emit a clean per-event decision record, so it is mostly a transport layer.

### Requirements

- A Tapo **C560WS** (other Tapo PTZ models likely work — feedback very welcome), a
  Raspberry Pi (**Pi Zero 2 W** and up), Python 3.10+.
- Config-driven `cameras.yaml`; secrets are referenced by environment-variable *name* only,
  so nothing sensitive lives in the config file.

Docs, a capability catalog and an `events_1` bitmask reference are in the repo.

Feedback, issues and PRs very welcome — especially from anyone reverse-engineering Tapo's
`events_1` bits (there are bits I still haven't mapped), anyone running a different Tapo
PTZ model who can tell me what breaks, or anyone who fancies building that MQTT bridge. 🙏

Thanks for reading!
