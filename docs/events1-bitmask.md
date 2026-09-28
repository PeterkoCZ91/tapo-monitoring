# The Tapo `events_1` bitmask

The camera's `getEvents` API is how the on-device AI reports activity — but on the C560WS
many events arrive with `event_type = None`, and the only usable signal is the integer
`events_1` **bitmask**. This field is barely documented anywhere; the meanings below come from the Tapo app's
own list of event codes and were checked against the fleet's captures. `tapo_monitor`
decodes it in [`detection.decode_events_1()`](../tapo_monitor/detection.py) and logs every
event's decoded flags (see the audit log), so the still-unmapped bits can be ground-truthed
from your own traffic.

A single event can carry several bits at once — e.g. `events_1 = 524290` is bits 19 **and**
1, i.e. an AI person who is also moving.

## What the bits are

`events_1` is a set of the camera's **alarm codes**, one bit per code: bit *n* is code
*n* + 1. The codes are the Tapo app's playback event types (its `PlayBackEventType`
enum, the same numbers the playback search takes as `event_type`), and `alarm_type` is
the event's main code:

| code | bit | value | meaning |
|-----:|----:|------:|---------|
| 2    | 1   | 2       | motion |
| 3    | 2   | 4       | tamper |
| 4    | 3   | 8       | line crossing |
| 5    | 4   | 16      | area intrusion |
| 6    | 5   | 32      | person |
| 8    | 7   | 128     | vehicle |
| 9    | 8   | 256     | pet |
| 20   | 19  | 524288  | face (see below) |
| 21 / 22 | 20 / 21 | | unfamiliar face / unfamiliar person |

The app's list also has timing (1), baby cry (7), doorbell, bark, meow, glass break,
smoke and CO alarms, package, anti-theft, panoramic and animal codes. A missing
`alarm_type` is read as 2 by the app. Sources: a pytapo issue comment that took the list
from the app ([JurajNyiri/pytapo#199](https://github.com/JurajNyiri/pytapo/issues/199)),
and the labels an H500 hub shows next to the same codes
([Sujeom/tapo-h500-local-recordings](https://github.com/Sujeom/tapo-h500-local-recordings)).

What we measured on the C560WS agrees:

- `alarm_type` 6 comes with bit 5 and `alarm_type` 2 with the plain motion class — trivially,
  since `alarm_type` is the main code. `alarm_type` 4 / 8 / 9 line up with bits 3 / 7 / 8,
  i.e. line crossing, vehicle and pet.
- **Bit 5 is the camera's person class.** Events carrying bit 5 without bit 19 reached the
  local scorer's person threshold in 43–80 % of incidents on three cameras, against
  59–79 % for bit 19 and 4–17 % for bare motion.
- **Bit 19 is still read as the AI person.** On the C560WS a recognised face (`face_id`)
  comes with only 6–12 % of bit-19 events, and never without it: the camera sets it for a
  detected face, which is a person, not for a known one.

### Correction

Until 2026-09 this page called bit 5 the **PIR sensor**, because it fired 1:1 with
`alarm_type` 6 on ~10,400 events. That was never evidence of PIR: `alarm_type` is the main
code, so the two always go together. The default event profile still decodes bit 5 as
`pir` (a PIR-backed burst skips the sampler's corroboration hold), because routing every
bit-5 event as a person was measured and is worse: a person event whose live frame is
empty queues a card follow-up, which stands the sampler down and arms the cooldown, and on
the fleet's journals that lost more alerted passages than it rescued. The narrower
`scorer.person_bit_skips_hold` lets a bit-5 live frame skip the corroboration hold too.

## Model-specific meaning

Everything above was measured on the C560WS (and the C260, which matches it). The bits
are not universal:

- **Shape.** The dual-lens C545D puts no `events_1` at the top level. Each lens that
  fired reports its own mask under `chn_events: {"1": {"events_1": N, "event_start_time":
  T}, "2": {...}}` (1 = fixed wide lens, 2 = pan/tilt lens).
  [`detection.normalize_event()`](../tapo_monitor/detection.py) turns that into a
  top-level `events_1` (OR of the lenses; a top-level value wins if a firmware sends
  both) and a `channels` list, for every camera, before anything else reads the event.
- **Meaning.** On the C545D a person walking by arrived as `alarm_type=6` with
  `events_1 = 34` (bits 1 + 5) on both lenses, and bit 19 was **not** set; plain motion
  was `alarm_type=2`, `events_1 = 2`, wide lens only. Observed, n=10 (8 person walks, 2 plain motion), checked against
  what the app reported. Bit 5 / `alarm_type=6` is the person class here as on the
  C560WS; the difference is that the C545D sets no bit 19, so bit 5 is its only person
  signal.

The per-model reading is a small table, `EVENT_PROFILES` in
[`detection.py`](../tapo_monitor/detection.py), picked per camera with `event_profile`
(`default` | `c545d`). `default` decodes bit 5 as `pir` and bit 19 as `person` (see the correction above); `c545d` maps bit 5 and
`alarm_type=6` to `person`. The audit line `event ... alarm_type=... channels=...
profile=...` shows the raw values next to the verdict, so a row can be amended when more
samples disagree — a new model gets a new row rather than a change to `default`.

## Not yet ground-truthed

The codes above come from the app, not from captures of every class. Bits 3, 7 and 8
(line crossing, vehicle, pet) match the `alarm_type` they arrive with, but `tapo_monitor`
still reports them as `unknown_bits` in the default profile rather than acting on them.
If your captures confirm one, a PR updating this page and the profile is welcome.
