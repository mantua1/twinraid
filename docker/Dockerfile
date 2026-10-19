# syntax=docker/dockerfile:1
#
# TwinRaid – Multi-Stage-Build. Aufruf aus dem Repo-Hauptordner:
#   docker build -f docker/Dockerfile -t twinraid:dev .
#
# Basis-Images sind per Digest gepinnt, s6-overlay per Prüfsumme.
# Die FROM-Zeilen stehen bewusst ohne ARG, damit Dependabot sie aktualisieren
# kann. Das Python-Image kommt dreimal vor und muss überall gleich sein.

ARG S6_OVERLAY_VERSION=3.2.3.2
# SSH-Client für Herunterfahren und VM-Shutdown (Konzept 3.3 und 5.6). Exakt gepinnt;
# Anheben der Version: README, Abschnitt „openssh-client aktualisieren“.
ARG OPENSSH_VERSION=1:10.0p1-7+deb13u4

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

# Feste Angaben; Version und Commit setzt die CI beim Bauen (Konzept 5.8).
# source verknüpft das Paket auf ghcr.io mit dem Repository.
LABEL org.opencontainers.image.title="TwinRaid" \
      org.opencontainers.image.description="Web UI to control two Unraid servers: wake-on-LAN, status, gaming VM and mirror sync." \
      org.opencontainers.image.source="https://github.com/mantua1/twinraid" \
      org.opencontainers.image.licenses="MIT"

COPY --from=s6 /s6root/ /

# openssh-client in genau der gepinnten Version. Bietet Debian sie nicht mehr an
# (meist nach einem Sicherheitsupdate), bricht der Build mit der neuen Version ab.
# ssh-keysign (setuid, Host-basierte Anmeldung) braucht TwinRaid nicht und wird
# entfernt; ssh-agent verliert sein setgid-Bit (TwinRaid nutzt keinen Agent).
ARG OPENSSH_VERSION
RUN <<EOF
set -eu
apt-get update
if ! apt-get install -y --no-install-recommends "openssh-client=${OPENSSH_VERSION}"; then
  available="$(apt-cache policy openssh-client | sed -n 's/^ *Candidate: //p')"
  echo "ERROR: openssh-client ${OPENSSH_VERSION} is no longer available from Debian." >&2
  echo "ERROR: Debian now offers openssh-client ${available:-(none)}." >&2
  echo "ERROR: Set OPENSSH_VERSION=${available:-?} in docker/Dockerfile (see README)." >&2
  exit 1
fi
rm -f /usr/lib/openssh/ssh-keysign
chmod g-s /usr/bin/ssh-agent
rm -rf /var/lib/apt/lists/*
EOF

# Web-Benutzer und Verzeichnisse (Konzept 3.2 und 5.4):
# /srv/worker gehört root mit 0700; ab v0.5.0 hängen in /srv/worker/source-guard die
# Auftragsdatei und der Status des Wächters (Konzept 3.6 und 5.9).
RUN groupadd --system --gid 10001 twinraid \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin twinraid-web \
    && install -d -m 0700 -o root -g root /srv/worker /srv/worker/source-guard \
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
    TWINRAID_DATA_DIR=/data \
    TWINRAID_RUNTIME_TLS_DIR=/run/twinraid-tls \
    TWINRAID_CUSTOM_TLS_DIR=/config/tls \
    TWINRAID_WORKER_SOCKET=/run/twinraid/worker.sock \
    TWINRAID_WEB_UID=10001 \
    TWINRAID_WEB_GID=10001 \
    TWINRAID_FRONTEND_DIR=/app/frontend \
    TWINRAID_BIND_HOST=0.0.0.0

# TWINRAID_MODE ist bewusst nicht gesetzt: ohne ausdrückliche Angabe startet der
# Container nicht (Konzept 6.4).

# Healthcheck als Web-Benutzer ohne Capabilities: nur TLS-Handshake zu
# 127.0.0.1, keine HTTP-Anfrage, plus Herzschlag des Workers (Konzept 5.4).
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["/usr/bin/setpriv", "--reuid=10001", "--regid=10001", "--clear-groups", \
         "--inh-caps=-all", "--bounding-set=-all", "--no-new-privs", \
         "--", "/app/.venv/bin/twinraid-healthcheck"]

EXPOSE 8443
ENTRYPOINT ["/init"]
