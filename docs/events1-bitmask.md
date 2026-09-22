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
| 5   | 32       | PIR sensor      | named by the firmware docs; fired exactly once in our captures (2026-09-22, `alarm_type=6`, see below) against a long run of never firing before that |
| 19  | 524288   | AI person       | the on-device AI confirmed a person — this is what `strict_people` alerts on |

Where other docs mention hardware PIR, that confirmation arrives via `alarm_type`, not
via bit 5 — the decoder still maps the bit, and `alarm_type` is logged with every event
so the mapping stays checkable against real traffic.

## `alarm_type = 6`: motion+PIR without the AI-person bit, but the companion app said "person"

One capture (2026-09-22, night, C560WS): `events_1 = 34` (bits 1 + 5, motion and PIR —
both already-named bits, no `unknown_bits`), `alarm_type = 6`. Bit 19 (AI person) never
set, in this single poll or any later one for the same event. The Tapo phone app tagged
the same event "Person" — a subject slowly walking through frame, not running, despite
the app also showing a running-person icon on the thumbnail.

So `alarm_type = 6` correlates with a real person here, but not through any bit our
decoder can see — the app's classification isn't sourced from local `getEvents` at all
(cloud-side or a richer on-device pipeline this API doesn't expose). `strict_people`
alerts would have missed this event outright were it not for a downstream image scorer
independently confirming a person from the frame. One capture is not a mapping — logged
so a second one can confirm or contradict it.

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
