# Public Release Checklist

This repository is being prepared for a possible public GitHub release. Treat camera credentials, API keys, bot tokens, private network topology, raw camera captures, and device identifiers as private data.

## Current Safety Decisions

- Runtime secrets live in local env files and are ignored by git.
- Historical notes that contained real secrets were copied to private/legacy-sensitive/ before redaction.
- private/, .claude/, .venv/, raw discovery captures, logs, packet captures, and captured media are ignored.
- Discovery scripts redact obvious sensitive keys such as tokens, passwords, MAC addresses, device IDs, coordinates, and camera IP by default.
- Use --include-camera-ip only for private debugging output.

## Before Publishing

1. Rotate any Telegram/Groq/Tapo/ONVIF credentials that ever appeared in old notes or assistant tool state.
2. Keep private/legacy-sensitive/ out of the repository.
3. Re-run a secret scan across the public tree, excluding only ignored private files.
4. Replace deployment examples with placeholders such as <PI_HOST>, <LAN_CAMERA_IP>, <ONVIF_USER>, and <TELEGRAM_BOT_TOKEN>.
5. Publish only synthetic ONVIF/API fixtures, never raw camera dumps.
6. Add a privacy note: Groq and Telegram send images outside the local network.

## Useful Local Checks

```bash
python3 -m py_compile person_monitor.py camera_automation.py scripts/event_discovery.py scripts/tapo_api_probe.py
rg -n --hidden --glob "!private/**" --glob "!*.env" "token|password|secret|rtsp://|sshpass|gsk_" .
```

If the second command returns real values instead of placeholders or code variable names, fix that before publishing.
