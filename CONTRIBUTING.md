# Contributing

Thank you for your interest in contributing. This project is a Python package
(`tapo_monitor`) targeting Linux and Raspberry Pi-class hosts with TP-Link Tapo PTZ
cameras.

Read the [documentation index](docs/README.md), [architecture](docs/architecture.md) and
[capability maturity table](docs/README.md#feature-maturity) before proposing a new runtime
path. Planned or researched features must remain clearly labelled until daemon-wired and
covered by tests.

## Setup

```bash
git clone <REPO_URL>
cd tapo-monitoring

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # package + dev tools (pytest, ruff)

cp cameras.example.yaml cameras.yaml
# Edit cameras.yaml: hosts, coordinates, capabilities. Secrets are referenced by
# environment-variable NAME only — never inline tokens.
export TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... GROQ_API_KEY=...
```

**Never commit `cameras.yaml` or any `.env` file** — they are excluded by `.gitignore`.

## Testing

```bash
tapo-monitor check cameras.yaml   # validate config + print a summary
pytest -q                          # run the test suite
ruff check .                       # lint
```

The pure logic (config parsing, scheduling, weather, tracking decisions, detection
classification, notification gating) is unit-tested without hardware. I/O collaborators
(camera, snapshot, Groq, Telegram) are injected so the pipeline is testable offline.

## Branch naming

| Type | Prefix | Example |
|---|---|---|
| New feature | `feature/` | `feature/onvif-events` |
| Bug fix | `fix/` | `fix/onvif-reconnect` |
| Documentation | `docs/` | `docs/deployment-guide` |

## Code style

- Python 3.10+
- 4-space indentation, LF line endings
- `ruff` clean (config in `pyproject.toml`)
- No credentials, tokens, private IPs, coordinates or personal names in code, comments,
  tests or docs

## What we accept

- Bug fixes with a clear explanation of the root cause
- New detection sources or Telegram alert improvements
- Documentation and deployment-guide updates

## What we do not accept

- Commits that include `cameras.yaml`, `.env` files or any credentials
- Cloud dependencies or mandatory paid services
- Personal data (real coordinates, hostnames, face IDs, names) anywhere in the tree
