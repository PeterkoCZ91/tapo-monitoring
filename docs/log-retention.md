# Log storage and retention

Archive retention uses the configured sent and review durations and the pan-limit
policy. A daily background worker cleans images and compacts expired metadata even
when no new camera events arrive. Invalid metadata is preserved for inspection.
Concurrent appends are protected during atomic index replacement. Enabled event
ledgers are cleaned in bounded batches; SQLite can reuse freed pages without
returning them to the filesystem.

The daily digest reports the active ledger's database and WAL sizes using file
metadata only. It does not open SQLite, create a missing database, run a checkpoint
or vacuum. An absent WAL, missing database and unreadable file are distinct states;
disabled ledgers are omitted. Actual disk measurements help decide whether later
database compaction is needed.

## System journal

Journald policies apply to every service on a host. Preserve existing storage mode
and any tighter size limits. A small dedicated host can use a drop-in such as
`/etc/systemd/journald.conf.d/storage-limits.conf`:

```ini
[Journal]
SystemMaxUse=300M
SystemKeepFree=1G
MaxRetentionSec=30day
```

A general-purpose computer can use a larger size and free-space allowance, such as
1 GiB and 2 GiB. Effective policies depend on the host's filesystem and workload.
Apply with the host's administrative privileges and verify the merged configuration
and journald service afterwards. Existing stricter policies need not be loosened.

Applying a smaller cap can automatically expire old archived journals. Active files
can make measured usage exceed the archive cap temporarily. An age limit is a maximum
age, not a guarantee of that many days of retained history: a size limit can remove
older history sooner. No manual vacuum or truncation is required to set a policy.

## Duplicate syslog files

Where rsyslog also writes files, add `maxsize` to its existing logrotate stanza while
preserving its file list, rotation count, compression and postrotate action. Examples
are `maxsize 32M` on a small host and `maxsize 64M` on a larger host. Validate the
configuration with `logrotate --debug` before applying it.

`maxsize` is checked only when logrotate runs. A daily schedule can exceed that size
between checks; it is not a strict byte cap on the active file. Keep the existing
schedule unless the observed growth requires a more frequent check.
