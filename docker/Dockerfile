# syntax=docker/dockerfile:1
#
# TwinRaid – Multi-Stage-Build. Aufruf aus dem Repo-Hauptordner:
#   docker build -f docker/Dockerfile -t twinraid:dev .
#
# Basis-Images sind per Digest gepinnt, s6-overlay per Prüfsumme.
# Die FROM-Zeilen stehen bewusst ohne ARG, damit Dependabot sie aktualisieren
# kann. Das Python-Image kommt dreimal vor und muss überall gleich sein.

ARG S6_OVERLAY_VERSION=3.2.3.2

FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv

# --- Stufe 1: Frontend bauen -------------------------------------------------
FROM node:24-trixie-slim@sha256:173f125896c3b47ddf056734c7ea789d04595a6a08769a8f78e0df642781fb66 AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci --ignore-scripts --no-audit
COPY frontend/ ./
RUN npm run build

# --- Stufe 2: Python-Umgebung bauen ------------------------------------------
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS backend
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY backend/src ./src
RUN uv sync --locked --no-dev --no-editable

# --- Stufe 3: s6-overlay prüfen und entpacken --------------------------------
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS s6
ARG S6_OVERLAY_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends xz-utils \
    && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12 \
    https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp/
ADD --checksum=sha256:e6befcc96a437a3831386ecfc51808c5d3e939dc5fe3c02ae9284599e8aa2408 \
    https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz /tmp/
RUN mkdir /s6root \
    && tar -C /s6root -Jxpf /tmp/s6-overlay-noarch.tar.xz \
    && tar -C /s6root -Jxpf /tmp/s6-overlay-x86_64.tar.xz

# --- Stufe 4: Laufzeit-Image -------------------------------------------------
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

COPY --from=s6 /s6root/ /

# Web-Benutzer und Verzeichnisse (Konzept 3.2 und 5.4):
# /srv/worker gehört root mit 0700; dort hängen Shares und Templates.
RUN groupadd --system --gid 10001 twinraid \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin twinraid-web \
    && install -d -m 0700 -o root -g root /srv/worker \
    && install -d -m 0755 -o root -g root /config /data

COPY --from=backend /app/.venv /app/.venv
COPY --from=frontend /build/dist /app/frontend
COPY docker/rootfs/ /

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    S6_READ_ONLY_ROOT=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=30000 \
    TWINRAID_CONFIG=/config/twinraid.yaml \
    TWINRAID_WORKER_SOCKET=/run/twinraid/worker.sock \
    TWINRAID_WEB_UID=10001 \
    TWINRAID_FRONTEND_DIR=/app/frontend \
    TWINRAID_BIND_HOST=0.0.0.0

# TWINRAID_MODE ist bewusst nicht gesetzt: ohne ausdrückliche Angabe startet der
# Container nicht (Konzept 6.4).

EXPOSE 8443
ENTRYPOINT ["/init"]
