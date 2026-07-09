# A12 GitHub repo notes

This page is the public-repo companion for the A12 integration.
Use it when the A12-side codebase or docs are being prepared for GitHub and must stay
aligned with the shared scorer contract documented here.

## Repo boundary

- A12 and tapo-monitor share only the HTTP scorer contract.
- A12 must not import `tapo_monitor` modules.
- tapo-monitor must not import A12 modules.
- The scorer is shared; alert rules, thresholds and notifications are not shared.

## What is safe to publish

- HTTP contract details for `/health` and `/score`.
- Environment-variable names.
- Documentation ranges such as `192.0.2.0/24`, `198.51.100.0/24`, and `203.0.113.0/24`.
- Generic localhost examples like `127.0.0.1` when they are clearly local-only.

## What should stay private

- Real LAN IPs, Tailscale IPs, SSH ports, hostnames, camera names tied to locations.
- Token values, passwords, chat IDs, RTSP URLs with credentials, and cloud/API keys.
- `/home/<user>` style local paths, packet captures, raw camera dumps, and device IDs.

## Public-release scan

Run the shared scan checklist before any public GitHub update:

- working tree scan
- git history scan
- inspect ignored private files separately if needed

The canonical command set lives in [`agent-scan.md`](agent-scan.md).

## A12-side checks

- Keep the local fallback path present when the shared scorer is enabled.
- Compare A12 threshold changes separately from tapo-monitor threshold changes.
- Verify the A12 container can reach the scorer address that is documented in the repo.
- Do not copy private deployment paths from a local machine into GitHub docs.
