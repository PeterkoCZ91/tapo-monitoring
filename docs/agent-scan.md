# Agent Scan Workflow

This file is the shared scan checklist for Codex and Claude before anything is prepared for a public GitHub release.

## Scope

- Scan the current working tree first.
- Then scan git history for values that may still exist in committed blobs.
- Treat anything under `private/`, ignored env files, raw captures, caches, and generated packaging metadata as out of scope for public release.

## What To Look For

- Real hostnames, LAN IPs, Tailscale `100.x.x.x` addresses, SSH endpoints, and private paths such as `/home/<user>`.
- API keys, bot tokens, passwords, chat IDs, RTSP URLs with embedded credentials, and `gsk_`-style secrets.
- MAC addresses, device IDs, coordinates, raw camera captures, and other deployment-specific identifiers.

## Working Tree Scan

```bash
rg -n --hidden \
  --glob "!.git/**" \
  --glob "!.venv/**" \
  --glob "!.pytest_cache/**" \
  --glob "!.ruff_cache/**" \
  --glob "!private/**" \
  --glob "!docs/superpowers/**" \
  --glob "!*.env" \
  "/home/|/Users/|token|password|secret|rtsp://|sshpass|gsk_|100\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.|10\.0\.0\." .
```

## History Scan

```bash
git log --all --oneline -G"/home/|/Users/|100\.[0-9]+\.[0-9]+\.[0-9]+|gsk_|sshpass|bot[0-9]{8,}:|TELEGRAM_TOKEN=|TELEGRAM_CHAT_ID=|GROQ_API_KEY="
```

## Decision Rule

- Placeholder values are fine.
- Documentation ranges such as `192.0.2.0/24`, `198.51.100.0/24`, and `203.0.113.0/24` are fine.
- Any real private address, token, credential, or local path that appears outside ignored files must be removed before publishing.
- If a sensitive value exists only in history, rotate the credential and decide whether the public history must be rewritten before the repo is published.

## Practical Use

- Codex: run the working-tree scan, then the history scan, then inspect the hits.
- Claude: use the same commands and treat the checklist as the source of truth when validating a public-release state.
