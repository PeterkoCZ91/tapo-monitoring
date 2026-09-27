# Running with Docker

The repository ships a `Dockerfile` with two targets, an example Compose file and an
example environment file. This is the shortest way to a running monitor; the systemd
layout in [Operations](operations.md) remains the reference deployment.

| Image | Built with | Contents |
| --- | --- | --- |
| monitor (default) | `docker build .` | Alpine, Python 3.12, ffmpeg, `tapo-monitor[mqtt]` |
| scorer | `docker build --target scorer .` | Debian slim, Python 3.12, `tapo-monitor[scorer]` (onnxruntime) |

Both run as an unprivileged user (uid/gid 10001) and keep all state under the `/data`
volume. Release builds are published for `linux/amd64` and `linux/arm64` (Raspberry Pi 4/5
with a 64-bit OS) as `ghcr.io/peterkocz91/tapo-monitoring:<version>` / `:latest` and
`:<version>-scorer` / `:scorer`, starting with the first release after this file was
added. Until then, or for a local change, build from the checkout.

## Quick start

```bash
git clone https://github.com/PeterkoCZ91/tapo-monitoring.git
cd tapo-monitoring

cp docker-compose.example.yml docker-compose.yml
cp cameras.example.yaml cameras.yaml && $EDITOR cameras.yaml
cp tapo.env.example tapo.env && $EDITOR tapo.env
chmod 600 tapo.env

docker compose run --rm monitor selfcheck /config/cameras.yaml
docker compose up -d
docker compose logs -f monitor
```

`selfcheck` loads the config, confirms every credential variable it names is set and
finds ffmpeg; `check` prints only the config summary. To build instead of pulling,
replace `image:` with `build: .` in the Compose file (or run `docker compose build`).

`cameras.yaml`, `tapo.env` and `docker-compose.yml` are git-ignored or local copies;
`.dockerignore` keeps them, `private/`, tests and media out of the build context, so no
secret can end up in an image layer.

## Configuration and secrets

- `cameras.yaml` is mounted read-only at `/config/cameras.yaml`, which is the image's
  default `run` argument.
- Secrets come from `tapo.env` through `env_file:`. The YAML names the variables
  (`token_env: TELEGRAM_TOKEN`, `password_env: CAM_PASSWORD`, ...); `tapo.env.example` lists
  the names the example config uses, with placeholder values only. Keep the two in step.
- Set `TZ` (for example `Europe/Prague`) so log times and fixed `HH:MM` schedules use your
  local time. `astral` schedules follow the `location:` block either way.

## What lands in `/data`

The image sets the environment so that everything the daemon persists goes to the volume:

| Variable | Value | What it holds |
| --- | --- | --- |
| `XDG_STATE_HOME` | `/data/state` | `tapo-monitor/health.json`, `twin.json`, `runtime.json`, hub cursor, `events.sqlite3` (ledger) |
| `STATE_DIR` | `/data` | weather (rain) cache |
| `HOME` | `/data` | `tapo-monitor/probe-log` of the scene probe |

The individual `TAPO_*_FILE` overrides ([Configuration](configuration.md)) still win.
The sent-photo and review-log archives are opt-in: setting `TAPO_SENT_LOG_DIR` /
`TAPO_REVIEW_LOG_DIR` is what switches them on, so the image leaves them unset. Point them
under `/data` (`tapo.env.example` has the lines commented out). Temporary snapshots use the
container's `/tmp` and disappear with it.

Inspect the state with the same CLI: `docker compose exec monitor tapo-monitor status`,
`... twin-status`, `... incident <id>`.

## Networking

The default bridge network is enough. The daemon only opens outbound connections — the
camera's HTTPS API, RTSP over TCP (snapshots use `-rtsp_transport tcp`),
ONVIF for `pan_limit`, Telegram and Groq — and Docker NATs those through the host like
any other client. Cameras behind a VPN subnet route that the host itself can reach work
the same way.

Use `network_mode: host` only when:

- the container must reach a service bound to the host's loopback, such as a go2rtc
  (`127.0.0.1:1984`) or a scorer listening only on `127.0.0.1`; or
- you want `observability.status_port` reachable from the host without widening
  `status_bind`. On a bridge network the endpoint binds the *container's* loopback by
  default, so it is visible only inside the container unless you set `status_bind:
  0.0.0.0` and publish the port — which exposes an unauthenticated endpoint; keep it on a
  trusted interface.

## Health check

The monitor image has no built-in `HEALTHCHECK`: its only cheap liveness signal is the
status endpoint, which is off by default. With `status_port: 8730` in `observability:` (the
default `127.0.0.1` bind is fine, since the check runs inside the container), add this to
the `monitor` service. It fails when the main loop has not completed a tick for five
minutes, not merely when the HTTP thread is down:

```yaml
    healthcheck:
      test: ["CMD", "python", "-c", "import json, time, urllib.request; t = json.load(urllib.request.urlopen('http://127.0.0.1:8730/status', timeout=4))['tick']['at']; raise SystemExit(0 if t and time.time() - t < 300 else 1)"]
      interval: 60s
      timeout: 10s
      start_period: 120s
      retries: 3
```

## Optional scorer

The scorer image serves `POST /score`, `GET /health` and `GET /metrics` on port 8766 and
has a built-in health check on `/health`. The YOLOX `.onnx` model is not shipped: put it
in `./models/` and uncomment the `scorer` service in the Compose file. Its settings are
environment variables with these defaults:

| Variable | Default |
| --- | --- |
| `TAPO_SCORER_MODEL` | `/models/model.onnx` |
| `TAPO_SCORER_PORT` | `8766` |
| `TAPO_SCORER_INPUT_SIZE` | `640` |
| `TAPO_SCORER_METRICS_FILE` | `/data/metrics/scorer.jsonl` |

The other `TAPO_SCORER_METRICS_*` variables from `systemd/tapo-scorer.env.example` work
unchanged. With both services in one Compose project, set the camera's `scorer.url` to
`http://scorer:8766/score`. The scorer has no authentication; publish its port only on a
network you trust. The image runs inference on the CPU; GPU providers need a different
base and are not covered here.

## Extras and upgrades

- `pan_limit` needs the ONVIF client: `docker build --build-arg MONITOR_EXTRAS=mqtt,onvif .`
- Upgrade with `docker compose pull && docker compose up -d`. State in `/data` carries
  over; `docker stop` sends SIGTERM, which the daemon handles by releasing its camera
  sessions before it exits.
- Pin a version tag instead of `latest` if you want upgrades to be a deliberate step.
