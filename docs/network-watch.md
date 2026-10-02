# Camera network measurements

`tools/network_watch.py` measures ICMP packet loss and latency independently of the
monitor. It uses only Python's standard library, `ping` (iputils), and `ip` (iproute2).
It opens no camera sessions. The timer takes five pings per target about every two
minutes, with two targets at most being measured at once.

Configure each host in `~/.config/tapo-network-watch/targets.json`:

```json
{
  "default_gateway": true,
  "targets": [{"name": "camera-a", "host": "camera-a.example.invalid"}]
}
```

The default router is discovered on each pass, so moving a monitoring host onto a
different network changes the measured router automatically. Include powered cameras,
hubs, and gateways as appropriate. Sleeping battery cameras should not be configured
as ping targets: their silence is expected, not evidence of a faulty network.

Install `network_watch.py` under `~/.local/share/tapo-network-watch/` and the supplied
service and timer under `~/.config/systemd/user/`. Enable the timer with:

```sh
systemctl --user daemon-reload
systemctl --user enable --now tapo-network-watch.timer
```

User timers need lingering enabled to run after logout and after reboot. Where a
user manager cannot run persistently, schedule the same `sample` command in that
user's crontab every two minutes. A file lock prevents overlapping measurements.

History is stored in `~/.local/state/tapo-network-watch/` as daily JSONL files with
mode 0600, retained for at most 30 days and 64 MiB total per host. The oldest files
are removed first when either limit is reached; an oversized current-day file keeps
its most recent complete records. Normal two-minute sampling stays below this limit.
Scheduled measurements write only errors to the system journal, avoiding a second
copy of every successful ping record. Addresses and target names remain local operator
configuration. Read a summary with:

```sh
python3 tools/network_watch.py summary --hours 24
python3 tools/network_watch.py summary --hours 168 --json
```

The summary weights packet loss by packets sent, reports mean/p95/max RTT, and
computes jitter as the mean absolute RTT difference between consecutive replies
within each burst. It also exposes RTT standard deviation in JSON. Lost replies
do not form jitter pairs, and no jitter is inferred across the two-minute gaps.
Probe errors and stale or missing measurements are shown separately from packet
loss. A router answering well while a camera answers poorly narrows the problem
to the path beyond that router or the camera's own ICMP response; ping alone cannot
prove which part is at fault. ICMP loss is not an estimate of lost detection events.

For a fleet summary, provide a private inventory of SSH argument lists:

```json
{"hosts": [{"name": "site-a", "command": ["ssh", "-o", "BatchMode=yes", "site-a"]}]}
```

```sh
python3 tools/network_watch.py fleet --inventory /path/to/private/fleet.json --hours 24
```

An unreachable monitoring host has an unavailable row; it is never reported as
100% loss at its cameras. The sample count and age show how much of the requested
time window was actually measured.

Each sample also atomically refreshes a private `summary.json` cache for the daily
review digest. The cache covers 24 hours without sending additional probes. Coverage
is the percentage of two-minute slots with a recorded attempt, including failed
attempts; probe errors are reported separately. The worst hour is a UTC clock-hour
bucket and includes packet and probe counts, so a partial hour is identifiable.
For a gateway collecting the probes on behalf of another monitoring host, mirror the
cache locally and set `TAPO_NETWORK_SUMMARY_FILE` in that daemon's environment to the
mirrored file. Preserve its original timestamp so failed transfers become stale data.
These probes describe the gateway's path to the cameras, not the monitoring host's
end-to-end path.

## Passive Wi-Fi evidence

Each existing measurement pass also reads the collecting host's local wireless
interfaces. It retains signal in dBm, negotiated transmit link rate, power-save
state and available transmit retry/failure counters. It reads sysfs and
`/proc/net/wireless`, then uses an already installed `iw` or `iwconfig` with bounded
local queries. It does not scan, install packages, change power-save, contact camera
APIs or add network probes. Battery cameras stay excluded.

`host_radio` in daily history, `latest.json` and `summary.json` describes the host
running this watcher. When a gateway's summary is mirrored elsewhere, its radio
snapshot still describes that gateway, not the daemon host or any camera. Missing
tools, permission denials, disconnected interfaces and unsupported metrics are
explicitly partial or unavailable; they never become ICMP loss or probe errors.
SSID, access-point addresses and raw command output are not retained.

The counters `tx_retries`, `tx_failed` and `tx_excessive_retries` remain separate:
`iwconfig` excessive retries means retry-limit failures and does not substitute for
`iw`'s retry count. Deltas have their observed interval attached and are unknown
on first observation, source changes, decreased counters, changed local interface
index or carrier-change count, or a baseline older than ten minutes. Counters are
driver dependent; when local link-generation counters are unavailable, an unobserved
reset followed by a higher value cannot be identified reliably. Link rate is not measured transfer throughput,
and host signal says nothing directly about the camera's radio.
