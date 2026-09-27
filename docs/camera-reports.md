# Camera reports

`tapo-monitor report` describes what *your* Tapo camera answers, in a file you can
review and share. New models get supported from these reports instead of someone
buying each camera: which getters work, which error codes the rest return, and what the
event bits look like when a person walks past.

It needs no `cameras.yaml` and nothing is uploaded — the command only writes a local
JSON file.

## Before you run it

The report opens its **own authenticated session** to the camera. Stop `tapo-monitor`,
Home Assistant and anything else that polls the camera first: a second session can make
the camera refuse logins (`-40214`) and lock your computer out for up to ~30 minutes.
Only read-only getters are sent — the same ones the digital twin reads — and the
[API deny-list](tapo-local-api.md) stays on.

Credentials come from `TAPO_USER` / `TAPO_PASSWORD` (the camera account set in the app
under *Advanced settings → Camera account*), or a prompt. They are never taken from the
command line.

```sh
export TAPO_USER=... TAPO_PASSWORD=...        # or let it prompt
tapo-monitor report --host 192.0.2.10 --out my-camera.json
# walk past the camera during a two-minute watch, and probe the RTSP streams:
tapo-monitor report --host 192.0.2.10 --out my-camera.json --watch 120 --rtsp
# offline digest of any report:
tapo-monitor report --summarize my-camera.json
```

| option | meaning |
|---|---|
| `--host` | camera address (never stored in the report) |
| `--out FILE` | output file, default `tapo-report.json` |
| `--events-hours H` | how far back to read the event index (default 12) |
| `--watch SECONDS` | poll `getEvents` every `--interval` seconds (default 5, minimum 3) while you walk past; stops early after 5 failed polls in a row |
| `--rtsp` | `ffprobe` `stream1` / `stream2` (codec, size, frame rate only), when ffmpeg is installed |
| `--yes` | skip the confirmation prompt |
| `--summarize FILE` | offline: working and failing getters, unknown `events_1` bits, matching `event_profile` |

## What is in the file

- `schema` — `tapo-monitor-report/1`.
- `camera` — model, hardware and firmware version, lens channels, and the matching
  `event_profile` if this project already knows the model.
- `getters` — one row per getter: `available`, `error` with the camera's numeric
  `error_code`, or `unknown` with a reason (missing in pytapo, denied, empty answer).
  Error messages are never kept. A camera with more than one lens also gets the per-lens
  reads (`chn_id`).
- `values` — the anonymized answers.
- `events` — the last 20 `getEvents` entries: `age_s` (seconds before the report),
  `duration_s`, raw `alarm_type`, `events_1` and per-lens `chn_events`.
- `watch` — the same for every new or changed entry seen during `--watch`, with
  `seen_at_s` from the start of the watch.
- `rtsp` — ffprobe results, if asked for.
- `redacted_keys` — the path of every value that was withheld.

## How it is anonymized

By **allow-list**: a value survives only under a key known to describe the model or a
setting (`enabled`, `sensitivity`, `sw_version`, `total_space`, ...). Everything else
becomes `"<redacted>"` and its path goes into `redacted_keys`, so you can see what was
withheld and a maintainer can widen the list in a reviewed change. Keys naming identity,
network or location (MAC, serial, device/hardware/OEM ids, SSID, IP, names and aliases,
face data, coordinates, time zone, cloud ids) are withheld with everything under them,
and no absolute time is kept.

A second, narrower list keeps setting values only while they look like an enum token or a
number (`auto`, `on`, `3`, `107.3GB`): image scene and full-colour modes, codec and
bitrate defaults, stream quality names, OSD font/date display settings, the SD card's
exact byte counts, alert type names and plain `true`/`false` feature flags. Free text
under the same keys, OSD label text, schedule times and the card's record start time
stay withheld.

Before writing, a self-check scans the finished JSON for IPv4 addresses, MAC addresses,
long hex or numeric identifiers, e-mail addresses and the host/user you typed in. On any
hit the file is **not** written; please report that as a bug.

Review the file before sharing it anyway: an allow-list can only be as good as the keys
someone judged safe.

## Sharing a report

Open a [*Camera report* issue](https://github.com/PeterkoCZ91/tapo-monitoring/issues/new?template=camera_report.yml):
model, firmware, what works and what does not, what you did during `--watch`, the
`--summarize` output, and the JSON file attached (drag it into the issue) or pasted in a
code block. Do not edit values by hand; if something looks private, say so, so the
anonymizer gets fixed for everyone.

An accepted report is added unchanged as `tests/fixtures/cameras/<model>-<firmware>.json`
and becomes a regression test (`tests/test_camera_fixtures.py`): it must keep the schema,
pass the leak self-check, equal what the current sanitizer writes
(`report.resanitize`), decode through the real event normalizer and classifier, and
summarize offline.
