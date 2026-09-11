"""Preserve settled recorder evidence outside the rolling recording tree."""

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

from .recclip import parse_segment_start

log = logging.getLogger(__name__)


class OutagePreserver:
    """One background copy at a time; bounded retries independent of alert delivery."""

    def __init__(self):
        self._worker = None
        self._attempts = {}

    def submit(self, root, host, *, outage_at, observed_at):
        if not root or not (Path(root) / host).is_dir():
            return
        if self._worker is not None and self._worker.is_alive():
            return
        key = (str(root), host)
        previous, attempts, retry_at = self._attempts.get(key, (None, 0, 0))
        if previous != outage_at:
            attempts, retry_at = 0, 0
        if attempts >= 5 or observed_at < retry_at:
            return
        self._attempts[key] = (outage_at, attempts + 1, observed_at + 60)

        def preserve():
            try:
                result = preserve_outage(root, host, outage_at=outage_at,
                                         observed_at=observed_at)
                if result is not None:
                    self._attempts[key] = (outage_at, 5, observed_at)
                    log.info("outage recording preserved: %s", result.name)
                elif attempts == 4:
                    log.warning("outage recording preservation exhausted: no settled segments")
            except (OSError, ValueError) as exc:
                log.warning("outage recording preservation failed: %s", type(exc).__name__)

        self._worker = threading.Thread(target=preserve, daemon=True,
                                        name="incident-preservation")
        self._worker.start()


def preserve_outage(recording_root, host, *, outage_at, observed_at,
                    max_bytes=2 * 1024**3, max_incidents=16):
    """Archive the final two nonempty MKVs; return the completed archive or None.

    Call on a confirmed outage, off the monitor thread. Sources must have stopped
    changing for 30 seconds. Repeated calls (including after restart) reuse the same
    archive. A full archive raises OSError and never deletes older evidence. This
    module does not invoke camera APIs, transcode, or change source recordings.
    """
    if not recording_root:
        return None
    if not host or Path(host).name != host or host in (".", ".."):
        raise ValueError("camera host must be a single directory name")
    root = Path(recording_root).expanduser().resolve()
    camera = root / host
    if camera.is_symlink():
        raise ValueError("camera recording directory must not be a symlink")
    candidates = []
    for path in camera.glob("*/*/zaznam_*.mkv"):
        if path.is_symlink() or not path.resolve().is_relative_to(camera):
            continue
        try:
            start = parse_segment_start(path)
            info = path.stat()
        except (OSError, ValueError):
            continue
        if info.st_size:
            candidates.append((start, path, info))
    selected = sorted(candidates, key=lambda entry: entry[0])[-2:]
    if not selected or any(observed_at - info.st_mtime < 30 for _, _, info in selected):
        return None
    identity = selected[-1][1].name
    fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
    camera_key = hashlib.sha256(host.encode()).hexdigest()[:24]
    archive_root = root.with_name(root.name + "-incidents")
    camera_root = archive_root / camera_key
    for directory in (archive_root, camera_root):
        if directory.is_symlink():
            raise ValueError("incident directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    destination = camera_root / fingerprint
    if (destination / "manifest.json").is_file():
        return destination
    omitted = []
    while len(selected) > 1 and sum(info.st_size for _, _, info in selected) > max_bytes:
        omitted.append(selected.pop(0)[1].name)
    if sum(info.st_size for _, _, info in selected) > max_bytes:
        raise OSError("incident size limit exceeded")
    if sum(1 for item in camera_root.iterdir() if item.is_dir()) >= max_incidents:
        raise OSError("incident archive capacity reached; export evidence before freeing space")
    temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=camera_root))
    try:
        entries = []
        for start, source, before in selected:
            digest = hashlib.sha256()
            target = temporary / source.name
            with source.open("rb") as reader, target.open("xb") as writer:
                target.chmod(0o600)
                remaining = before.st_size
                while remaining:
                    block = reader.read(min(1024 * 1024, remaining))
                    if not block:
                        raise OSError("recording changed during incident preservation")
                    writer.write(block)
                    digest.update(block)
                    remaining -= len(block)
                writer.flush()
                os.fsync(writer.fileno())
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size, after.st_mtime_ns, after.st_ino):
                raise OSError("recording changed during incident preservation")
            entries.append({"name": source.name, "start_at": start,
                            "modified_at": before.st_mtime, "bytes": before.st_size,
                            "sha256": digest.hexdigest()})
        manifest = temporary / "manifest.json"
        with manifest.open("x") as stream:
            manifest.chmod(0o600)
            json.dump({"version": 1, "camera_key": camera_key, "outage_at": outage_at,
                       "preserved_at": observed_at, "segments": entries,
                       "omitted_for_size": omitted}, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.rename(destination)
        return destination
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
