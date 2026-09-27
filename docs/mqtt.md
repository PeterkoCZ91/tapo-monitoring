# MQTT and Home Assistant

tapo-monitor can publish its view of each camera to an MQTT broker, with
[Home Assistant MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery),
so every camera shows up in Home Assistant as a device with ready-made entities. It is
**off unless the config has an `mqtt:` block**; without it nothing is imported and the
daemon behaves exactly as before.

This is an outbound, publish-only client. It subscribes to nothing, opens no port and
accepts no commands: nothing on the broker can move a camera or change a setting. Telegram
stays the alert channel; MQTT is a mirror for dashboards and automations.

## Install and enable

```bash
pip install 'tapo-monitor[mqtt]'      # adds paho-mqtt (1.6 or 2.x)
```

```yaml
mqtt:
  host: broker.example.org     # required; the block's presence is the switch
  # port: 1883                 # default 1883, or 8883 when tls is true
  user_env: MQTT_USER          # names of env vars holding the login, never the values
  password_env: MQTT_PASSWORD
  tls: false                   # true: TLS with the system CA store
  discovery_prefix: homeassistant
  base_topic: tapo_monitor     # set a distinct one per daemon when several share a broker
  publish_images: false        # true: also publish the last alert photo (see Privacy)
  motion_off_after: 60         # seconds person/motion stay on after the last event
```

Unknown keys are refused at load time, like everywhere else in `cameras.yaml`.
`tapo-monitor selfcheck` fails with `mqtt` when the block is set but paho-mqtt is not
installed. The daemon itself logs the same error and keeps monitoring without MQTT: an
integration never stops the cameras from being watched.

## Entities

Each camera becomes a device named after the camera, linked to one daemon device.

| Entity | Type | State | Source |
| --- | --- | --- | --- |
| Person | `binary_sensor` (occupancy) | `ON` when an alert is delivered, `OFF` `motion_off_after` s after the last one | the alert send path |
| Motion | `binary_sensor` (motion) | `ON` when the camera reports a detection event, `OFF` after `motion_off_after` s | the `audit … action=detect` point (getEvents cameras; a hub-polled battery camera has none, so its Motion stays unknown) |
| Reachable | `binary_sensor` (connectivity) | the latest reachability probe | outage watchdog |
| Privacy mode | `binary_sensor` | `ON` while the lens is parked | control-pass read, else the twin probe |
| Detection off | `binary_sensor` (problem) | `ON` while motion or person detection is switched off | control-pass read (`detection_notice: true`) |
| Health | `sensor` | Digital Twin health status (`ok`, `degraded`, …); layers and drift count as attributes | `observability.digital_twin` |
| Last alert | `sensor` (timestamp) | when the last alert went out | the alert send path |
| Last alert score | `sensor` | the scorer's subject confidence of that alert | the alert send path |
| Last alert photo | `image` | the whole-scene frame of the last alert | only with `publish_images: true` |

The daemon device carries **Running** (`online`/`offline`), **Loop failing** (a tick
raised) and **Dropped MQTT messages**.

"Person" follows the daemon's alert decision, so it is on for exactly the alerts that
reached Telegram. A camera with `telegram_alerts: false` (or silenced by
`follow_app_notifications`) records its alerts without sending them; those still turn
Person on, so MQTT can be the only alert channel for such a camera.

A value the daemon has never read is not published, and Home Assistant shows it as
unknown: privacy mode needs a control-pass read (`privacy_notice`, `pan_limit` or a preset
in the plan) or the twin; "Detection off" needs `detection_notice: true`; health needs the
Digital Twin.

## Topics

All states are retained and published only when they change.

```
<base>/status                          online | offline   (last will: offline)
<base>/daemon/tick_problem             ON | OFF
<base>/daemon/dropped                  integer
<base>/<camera>/person                 ON | OFF
<base>/<camera>/motion                 ON | OFF
<base>/<camera>/connectivity           ON | OFF
<base>/<camera>/privacy                ON | OFF
<base>/<camera>/detection_off          ON | OFF
<base>/<camera>/detection_off/attributes   JSON
<base>/<camera>/health                 status text
<base>/<camera>/health/attributes      JSON
<base>/<camera>/last_alert             ISO 8601 UTC
<base>/<camera>/last_alert_score       0.000–1.000
<base>/<camera>/image                  JPEG bytes (publish_images only)
<prefix>/<component>/<base>_cam_<camera>/<entity>/config   discovery
<prefix>/<component>/<base>_daemon/<entity>/config         discovery
```

`<camera>` is the camera name with every character outside `A-Z a-z 0-9 _ -` replaced by
`_` (`back yard` → `back_yard`); two names that end up equal are refused at load time. In
the discovery topics a `/` in `base_topic` becomes `_`.

## Delivery guarantees

The main loop never waits for the broker. Every hook updates an in-memory copy of the
retained state and appends to a bounded queue (256 messages); a worker thread owns the
connection. When the queue is full the oldest message is dropped and counted. A broker
that is down or slow therefore costs the loop nothing, and reconnects back off from 1 s to
5 minutes.

Drops during an outage are harmless: on every (re)connect the worker discards the queue
and republishes the whole retained state — discovery first, then every current value —
so a restarted broker without persistence is repopulated within one connection. What is
lost is only the history in between (a person ON/OFF pulse that began and ended while
the broker was away arrives as OFF).

Availability uses the MQTT last will: if the daemon dies, the broker publishes
`offline` and every camera entity turns unavailable. A clean stop publishes `offline`
itself.

## Privacy

`publish_images` is off by default. Alert photos show people, often neighbours or
visitors, and anything published to a broker is readable by every client with access to
that topic, and retained there. Turn it on only for a broker you control, with
authentication, and preferably TLS. No credentials, camera addresses or stream URLs are
ever published; camera names are, so pick names you are happy to see on the broker.

## Example Home Assistant automation

Entity ids follow the device and entity names; with a camera named `front` they are
`binary_sensor.front_person`, `binary_sensor.front_motion`, `sensor.front_last_alert`,
and so on; the daemon's own are `binary_sensor.tapo_monitor_running` and
`binary_sensor.tapo_monitor_loop_failing` (with a non-default `base_topic` its name is
appended). Checked against a live Home Assistant. Check them under
*Settings → Devices & services → MQTT*.

```yaml
automation:
  - alias: "Porch light when tapo-monitor sees a person at night"
    triggers:
      - trigger: state
        entity_id: binary_sensor.front_person
        to: "on"
    conditions:
      - condition: sun
        after: sunset
        before: sunrise
    actions:
      - action: light.turn_on
        target:
          entity_id: light.porch
      - delay: "00:03:00"
      - action: light.turn_off
        target:
          entity_id: light.porch

  - alias: "Tell me when a camera stops watching"
    triggers:
      - trigger: state
        entity_id:
          - binary_sensor.front_privacy_mode
          - binary_sensor.front_detection_off
        to: "on"
        for: "00:05:00"
    actions:
      - action: notify.notify
        data:
          message: "{{ trigger.to_state.name }} is on"
```

## Removing it

Delete the `mqtt:` block and restart. The retained topics stay on the broker until
cleared; publish an empty retained payload to each `<prefix>/…/config` topic (Home
Assistant then removes the entities) and to the state topics under `<base>/`.
