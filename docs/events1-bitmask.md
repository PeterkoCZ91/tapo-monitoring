# The Tapo `events_1` bitmask

The camera's `getEvents` API is how the on-device AI reports activity — but on the C560WS
many events arrive with `event_type = None`, and the only usable signal is the integer
`events_1` **bitmask**. This field is barely documented anywhere, so the values below were
reverse-engineered from ~24 h of real captures across two C560WS cameras. `tapo_monitor`
decodes it in [`detection.decode_events_1()`](../tapo_monitor/detection.py) and logs every
event's decoded flags (see the audit log), so the still-unmapped bits can be ground-truthed
from your own traffic.

A single event can carry several bits at once — e.g. `events_1 = 524290` is bits 19 **and**
1, i.e. an AI person who is also moving.

## Confirmed bits

| bit | value | meaning | notes |
|----:|------:|---------|-------|
| 1   | 2        | motion          | basic/software motion; a frequent false positive on its own |
| 5   | 32       | PIR sensor      | fires 1:1 with `alarm_type=6` — confirmed over ~10,400 events across two C560WS cameras over 2.5 months (2026-07 to 2026-09), never once alongside any other `alarm_type` |
| 19  | 524288   | AI person       | the on-device AI confirmed a person — this is what `strict_people` alerts on |

## `alarm_type`: two parallel channels, only one hardware-corroborated

`alarm_type` isn't just correlated with the bits above, it gates which of two channels an
event came in on. Across both fleet cameras' full retained history (674 events on one,
9,722 on the other):

| `alarm_type` | PIR (bit 5) | share of events | AI-person (bit 19) rate |
|-------------:|:-----------:|-----------------:|-------------------------:|
| 2            | never       | ~82%             | 2–7%                     |
| 6            | always      | ~18%             | 36–43%                   |

So `alarm_type=6` is the **PIR-corroborated** counterpart of the plain motion/person
class (`alarm_type=2`), not a rare or unmapped value — earlier text in this doc claimed
PIR "never once observed firing" and treated one `alarm_type=6` capture as a fluke; both
were wrong, corrected 2026-09-22 against the real fleet history instead of a ~24h sample.

The practical upshot: an `alarm_type=6` event is 5–15× more likely to carry the AI-person
bit than a plain `alarm_type=2` one, but well under half still don't — a PIR hit raises
the odds, it doesn't confirm a person by itself. `strict_people` still gates on bit 19
alone, so an unconfirmed `alarm_type=6` event (motion+PIR, no person bit) can still only
alert via a downstream image scorer, same as unconfirmed `alarm_type=2`.

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
  what the app reported. The C545D has no PIR, so here bit 5 / `alarm_type=6` is the
  person class, not the PIR it is on the C560WS.

The per-model reading is a small table, `EVENT_PROFILES` in
[`detection.py`](../tapo_monitor/detection.py), picked per camera with `event_profile`
(`default` | `c545d`). `default` is the C560WS table above; `c545d` maps bit 5 and
`alarm_type=6` to `person`. The audit line `event ... alarm_type=... channels=...
profile=...` shows the raw values next to the verdict, so a row can be amended when more
samples disagree — a new model gets a new row rather than a change to `default`.

## Observed but not yet ground-truthed

Reported as `unknown_bits` rather than guessed at:

| bit | value | correlated `alarm_type` | suspected (unconfirmed) |
|----:|------:|------------------------:|-------------------------|
| 3   | 8     | 4 | another AI category |
| 7   | 128   | 8 | **vehicle** — by far the most common non-person event |
| 8   | 256   | 9 | pet / line-crossing? |

`alarm_type` correlates with the bits above; in our data `alarm_type = 2` accompanies the
motion/person class, while `4 / 8 / 9` line up with bits `3 / 7 / 8`. These mappings are
empirical, not from a spec — treat the unconfirmed rows as hypotheses and verify against the
audit log before relying on them. If your captures pin down bits 3/7/8, a PR updating this
table is very welcome.
