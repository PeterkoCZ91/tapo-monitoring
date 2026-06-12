# Contributing

Thank you for your interest in contributing. This project is Python-based and runs on a Raspberry Pi with a Tapo IP camera.

## Setup

```bash
git clone https://github.com/PeterkoCZ91/tapo-camera-monitor.git
cd tapo-camera-monitor

python3 -m venv .venv
source .venv/bin/activate
pip install onvif-zeep pytapo opencv-python-headless

cp tapo-camera.env.example tapo-camera.env
# Fill in your camera IP, credentials, Telegram token, Groq API key
```

**Never commit `tapo-camera.env` or any `.env` file** — they are excluded by `.gitignore`.

## Testing

Use the `--test` flag to run the full pipeline without ONVIF (snapshot → Groq → Telegram):

```bash
source tapo-camera.env
python person_monitor.py --test
```

Use the discovery scripts to inspect a camera's ONVIF events and pytapo API:

```bash
python scripts/event_discovery.py --env-file tapo-camera.env --duration 60
python scripts/tapo_api_probe.py --env-file tapo-camera.env --call-safe-defaults
```

## Branch naming

| Type | Prefix | Example |
|---|---|---|
| New feature | `feature/` | `feature/sound-detection` |
| Bug fix | `fix/` | `fix/onvif-reconnect` |
| Documentation | `docs/` | `docs/deployment-guide` |

## Code style

- Python 3.10+, no external dependencies beyond what is already in the project
- 4-space indentation, LF line endings
- No credentials, tokens, or private IPs in code or comments

## What we accept

- Bug fixes with a clear explanation of the root cause
- New detection types or Telegram alert improvements
- Improvements to the discovery / probe scripts
- Documentation and deployment guide updates

## What we do not accept

- Commits that include `.env` files or any credentials
- Cloud dependencies or mandatory paid services
- Changes that break the `--test` pipeline
