# Recorder evidence on camera outage

When a network outage reaches `alerts.outage_threshold`, the monitor preserves the
camera's newest two nonempty local recorder MKVs. Set `RECORDING_ROOT` to enable
this for cameras with an existing recording directory; it works with either SD or
recorder snapshots. Battery cameras using hub polling are excluded because sleeping
does not establish a network outage. The existing `night_only` watchdog schedule
also applies.

Archives live beside the recording tree, in
`<RECORDING_ROOT>-incidents/<camera-key>/<incident-key>/`. Rolling retention must
remain scoped to `RECORDING_ROOT`. The archive contains unmodified copies and a
`manifest.json` with segment start/modification times, outage/preservation times,
byte sizes and SHA-256 checksums. Keys are hashes; filenames retain recorder times.
Directories use mode 0700 and files 0600.

One background worker copies at a time. The final files must have been unchanged
for 30 seconds, and changes during copying abort publication. Unsettled files or
I/O failures retry at most five times, at least 60 seconds apart. Alert delivery
failures do not cause repeated copies. Completed archives are reused after restart
when the final segment filename matches. No camera downloads or transcoding run here.

The limits are 2 GiB per incident and 16 archive directories per camera. Reaching
the incident size limit omits the preceding segment and records that fact in the
manifest. If the final segment alone exceeds the limit, or archive capacity is
reached, preservation fails and leaves existing evidence intact.
Export evidence before manually freeing archive space; archives are never aged out
automatically. An interrupted copy may leave a `.pending-*` directory; inspect it
before manual removal. The manifest records observed outage timing, not a certified
incident time. This feature preserves local video only; related notification photos
remain in the existing alert storage.
