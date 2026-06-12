# Security Policy

## Supported versions

Only the latest version on `main` receives security fixes.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Send a description to the maintainer via the email on the GitHub profile. Include:
- Affected component and reproduction steps
- Potential impact
- Any suggested mitigations

You will receive an acknowledgement within **7 days**.

## Scope

- ONVIF credential handling and RTSP URL construction
- pytapo authentication flow
- Telegram and Groq API key storage (env files)

## Out of scope

- Physical access to the camera or Raspberry Pi
- Vulnerabilities in third-party libraries (onvif-zeep, pytapo, opencv) — report those to their maintainers
- Tapo camera firmware — report to TP-Link
