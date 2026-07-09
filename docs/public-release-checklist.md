# Public Release Checklist

This repository is being prepared for a possible public GitHub release. Treat camera credentials, API keys, bot tokens, private network topology, raw camera captures, and device identifiers as private data.

The shared scan workflow for Codex and Claude lives in [docs/agent-scan.md](agent-scan.md).

## Current Safety Decisions

- Runtime secrets live in local env files and are ignored by git.
- Historical notes that contained real secrets were copied to private/legacy-sensitive/ before redaction.
- private/, .claude/, .venv/, raw discovery captures, logs, packet captures, and captured media are ignored.
- Discovery scripts redact obvious sensitive keys such as tokens, passwords, MAC addresses, device IDs, coordinates, and camera IP by default.
- Public examples should use placeholders or RFC 5737 documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`), never live LAN/Tailscale addresses.
- Use --include-camera-ip only for private debugging output.
- Operational notes under `docs/superpowers/`, local env files, media captures and private
  deployment docs are ignored and must stay out of commits.

## Before Publishing

1. Rotate any Telegram/Groq/Tapo/ONVIF credentials that ever appeared in old notes or assistant tool state.
2. Keep private/legacy-sensitive/ out of the repository.
3. Re-run a secret scan across the public tree, excluding only ignored private files.
4. Re-run a git-history scan before pushing; fixing the working tree does not remove values already committed.
5. Replace deployment examples with placeholders such as <PI_HOST>, <LAN_CAMERA_IP>, <ONVIF_USER>, and <TELEGRAM_BOT_TOKEN>.
6. Publish only synthetic ONVIF/API fixtures, never raw camera dumps.
7. Add a privacy note: Groq and Telegram send images outside the local network.
8. Confirm public docs do not mention live hostnames, Tailscale IPs, SSH ports, chat IDs,
   camera names tied to private locations, local home paths, or real model/deploy paths
   unless intentionally generic placeholders.
9. Keep generated packaging metadata (`*.egg-info/`), caches and local recorder media out
   of git.

## Useful Local Checks

```bash
pytest -q
ruff check .
git status --short
rg -n --hidden \
  --glob "!.git/**" \
  --glob "!.venv/**" \
  --glob "!.pytest_cache/**" \
  --glob "!.ruff_cache/**" \
  --glob "!private/**" \
  --glob "!docs/superpowers/**" \
  --glob "!*.env" \
  "/home/|/Users/|token|password|secret|rtsp://|sshpass|gsk_|100\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.|10\.0\.0\." .

# History risk: this reports commits that introduced or removed sensitive-looking
# strings. Inspect the matching diff before pushing to a public remote.
git log --all --oneline -G"/home/|/Users/|100\.[0-9]+\.[0-9]+\.[0-9]+|gsk_|sshpass|bot[0-9]{8,}:|TELEGRAM_TOKEN=|TELEGRAM_CHAT_ID=|GROQ_API_KEY="
```

If the search returns real values instead of placeholders, code variable names, documentation ranges, or ignored private paths, fix that before publishing. If the history scan finds a real token, credential, live host, or private path that is already on a public remote, rotate the credential and decide whether the public history needs to be rewritten.
