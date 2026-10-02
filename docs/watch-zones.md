# Watch zones, loitering and vehicle alerts

Three opt-in features narrow or extend what counts as an alert on a camera that already
uses the [local scorer](configuration.md#local-scorer-and-crop). All three are off by
default and change nothing until you enable them. The key-by-key reference is in
[configuration](configuration.md); this page is about when to use them and where they
stop working.

## Watch zones (`scorer.alert_zones`)

A zone is a polygon of at least three `[x, y]` points, given as fractions of the frame
(`0,0` top left, `1,1` bottom right). A person counts only when the bottom centre of its
box, roughly the feet, lies inside at least one zone. A person on the pavement behind
your fence no longer produces an alert; a person on your driveway does.

```yaml
cameras:
  - name: driveway
    host: 203.0.113.10
    role: static
    scorer:
      url: http://203.0.113.20:8766/score
      alert_zones:
        - [[0.40, 1.00], [1.00, 1.00], [1.00, 0.55], [0.62, 0.50]]   # driveway, lower right
```

Use it when the picture contains ground you do not own and the goal is to be told about
people on your side of the line. To find good polygons, collect some frames, run
`python3 tools/replay_zones.py score` on them, and look at where feet land with `feet`;
`zones` then lists the frames that would lose their person with the polygons you drew.

Limits:

- Zones are drawn on one framing. Use them on a fixed camera, or on a PTZ camera parked
  on a single preset. Auto-track, preset changes and the pan-limit guard all change the
  framing, and the polygon then no longer matches the picture. A camera with
  `role: tracking` (the default) or `pan_limit.enabled` loads fine but logs a warning at
  start-up; a dual-lens profile with a pan/tilt lens is refused outright.
- A zone is a filter. Alerts for people outside it are dropped, not just ranked lower,
  so it suits fixed cameras where you want less noise, not cameras where the goal is the
  full volume of alerts.
- The scoring service must return `person_boxes`; an older one is left alone with one
  warning.

## Loitering (`sampler.loiter_frames`)

With the sampler on, a person who stays in view produces many follow-up frames but only
one alert. `loiter_frames: K` adds one separate notice, "someone has been here for about
N min", once a person appears on `K` consecutive follow-up frames of one group.

```yaml
    sampler:
      enabled: true
      interval: 30
      max_frames: 8
      loiter_frames: 4          # about two minutes at interval 30
      loiter_miss_tolerance: 1  # a frame without the person does not reset the count
```

It sends at most one notice per group, needs a scorer, and `K` must not exceed
`max_frames`. Combined with `alert_zones`, only people standing inside a zone extend the
streak.

Limits:

- Only sampler follow-up frames count, not the live frame or SD clips.
- A PTZ move or pan-limit recall that turns the lens away makes the person disappear for
  a while and interrupts the count; `loiter_miss_tolerance` bridges a few such frames.
- Someone who walks back and forth out of view will not reach the streak.

`python3 tools/replay_zones.py loiter` replays saved sampler audit lines with different
`K` and tolerance values so you can see how many notices a setting would have produced.

## Vehicle alerts (`scorer.alert_vehicles`)

The scoring service also reports cars, motorcycles, buses and trucks. With
`alert_vehicles: true` a vehicle at or above `vehicle_threshold` raises its own alert.

```yaml
    scorer:
      url: http://203.0.113.20:8766/score
      alert_vehicles: true
      vehicle_threshold: 0.5
```

A frame that qualifies as a person alert is only ever a person alert; the vehicle alert
is a side alert with its own cooldown. With `alert_zones` set, a vehicle must stand in a
zone too.

Limits:

- Live poll frames, sampler frames and SD/recording follow-ups raise vehicle alerts; the
  shadow scan does not. A follow-up only alerts a vehicle when no frame of its window
  shows a person, and at most once per follow-up and event.
- `0.5` is a starting estimate, not a tuned value. Check it against your own frames
  before relying on it; parked cars in view will alert whenever they are scored, so
  pair it with a zone that excludes them.
- Roll out the scoring service first, then the daemons: an old service reports no
  vehicles, and nothing happens until it is replaced.
