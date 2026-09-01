# Security Policy

## Supported versions

Only the latest version on `main` receives security fixes.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Send a description through the repository's configured private security contact. Include:
- Affected component and reproduction steps
- Potential impact
- Any suggested mitigations

You will receive an acknowledgement within **7 days**.

## Scope

- ONVIF credential handling and RTSP URL construction
- pytapo authentication flow
- Telegram and Groq API key storage (env files)
- Redaction and private permissions of health, Digital Twin and Shadow ledger state
- Leakage of camera identifiers, credentials or media through observability output

Known and deliberate: the scorer service binds `0.0.0.0` with no authentication — it is
meant to be shared across hosts on a trusted network. Run it behind a firewall; do not
expose the port. The JSON status endpoint, by contrast, defaults to `127.0.0.1`.

## Out of scope

- Physical access to the camera or Raspberry Pi
- Vulnerabilities in third-party libraries (onvif-zeep, pytapo, onnxruntime, Pillow) — report those to their maintainers
- Tapo camera firmware — report to TP-Link
