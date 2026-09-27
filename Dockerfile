# syntax=docker/dockerfile:1
#
# tapo-monitor container images. See docs/docker.md.
#
#   docker build -t tapo-monitor .                        # the monitor daemon (default)
#   docker build --target scorer -t tapo-monitor-scorer . # the optional YOLOX scorer
#
# Each image installs the package into a virtualenv in a throwaway build stage and copies
# only that venv into a clean runtime stage, so pip caches and the source tree never reach
# the final image.
#
# The two images use different bases on purpose. The monitor runs on Alpine: every one
# of its dependencies ships musl wheels, and Alpine's ffmpeg is about a quarter of
# Debian's (roughly 130 MB against 450 MB), which matters on a Raspberry Pi's SD card.
# The scorer needs no ffmpeg but does need onnxruntime, which publishes glibc wheels
# only, so it runs on Debian slim.

ARG PYTHON_VERSION=3.12

# A fixed, unprivileged account (uid/gid 10001). Its home is the data volume, so anything
# the code keeps under ~ (the scene-probe archive) lands on the volume too.

# ===================================================================== monitor build ===
FROM python:${PYTHON_VERSION}-alpine AS monitor-builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY tapo_monitor ./tapo_monitor

# Extras for the monitor image. mqtt is imported only when the config has an mqtt: block,
# so shipping it costs little and saves a rebuild. Add onvif for pan_limit:
#   docker build --build-arg MONITOR_EXTRAS=mqtt,onvif .
ARG MONITOR_EXTRAS=mqtt
RUN pip install ".[${MONITOR_EXTRAS}]"

# ====================================================================== scorer build ===
FROM python:${PYTHON_VERSION}-slim AS scorer-builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY tapo_monitor ./tapo_monitor
RUN pip install ".[scorer]"

# ============================================================================ scorer ===
# Stateless HTTP scoring service: POST /score, GET /health, GET /metrics. The .onnx model
# is not shipped; mount it under /models (read-only is fine).
FROM python:${PYTHON_VERSION}-slim AS scorer

RUN groupadd --system --gid 10001 tapo \
 && useradd --system --uid 10001 --gid tapo --home-dir /data --no-create-home \
        --shell /usr/sbin/nologin tapo \
 && mkdir -p /data /models \
 && chown tapo:tapo /data

COPY --from=scorer-builder /opt/venv /opt/venv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    HOME=/data \
    TAPO_SCORER_MODEL=/models/model.onnx \
    TAPO_SCORER_PORT=8766 \
    TAPO_SCORER_INPUT_SIZE=640 \
    TAPO_SCORER_METRICS_FILE=/data/metrics/scorer.jsonl

VOLUME ["/data"]
USER tapo
WORKDIR /data
EXPOSE 8766

# /health answers without touching the model, so it is cheap enough to poll.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('TAPO_SCORER_PORT', '8766'), timeout=4)"

# Through a shell so the settings above can be overridden per container with -e, and any
# extra arguments (e.g. --metrics-retention-days 3) are appended. exec keeps the service
# as PID 1, so `docker stop` (SIGTERM) flushes its metrics before exit.
ENTRYPOINT ["/bin/sh", "-c", "exec python -m tapo_monitor.scorer_service --model \"$TAPO_SCORER_MODEL\" --port \"$TAPO_SCORER_PORT\" --input-size \"$TAPO_SCORER_INPUT_SIZE\" \"$@\"", "tapo-scorer"]

# =========================================================================== monitor ===
# Last stage, so a plain `docker build .` produces the monitor image.
FROM python:${PYTHON_VERSION}-alpine AS monitor

# ffmpeg captures RTSP snapshots and clip frames; tzdata makes TZ=Europe/... work.
RUN apk add --no-cache ffmpeg tzdata \
 && addgroup -S -g 10001 tapo \
 && adduser -S -D -H -u 10001 -G tapo -h /data -s /sbin/nologin tapo \
 && mkdir -p /data /config \
 && chown tapo:tapo /data

COPY --from=monitor-builder /opt/venv /opt/venv

# Everything the daemon persists goes to the /data volume:
#   XDG_STATE_HOME  health.json, twin.json, runtime.json, hub cursor, events.sqlite3
#   STATE_DIR       weather (rain) cache; the volume root itself, because that module
#                   writes without creating a directory first
#   HOME            ~/tapo-monitor/probe-log of the scene probe
# The photo archives (TAPO_SENT_LOG_DIR, TAPO_REVIEW_LOG_DIR) are opt-in features that
# are switched on by setting them, so the image leaves them unset; tapo.env.example
# shows /data paths for both.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    XDG_STATE_HOME=/data/state \
    STATE_DIR=/data \
    HOME=/data

VOLUME ["/data"]
USER tapo
WORKDIR /data

# No HEALTHCHECK: the daemon's only cheap liveness signal is the opt-in status endpoint
# (observability.status_port), which is off by default. docs/docker.md shows how to add
# one in compose once it is enabled.
ENTRYPOINT ["tapo-monitor"]
CMD ["run", "/config/cameras.yaml"]
