#!/usr/bin/env bash
# Gemeinsame Umgebung für die lokale Entwicklung ohne Container.
# Wird von dev.sh und dev-admin.sh eingebunden (source), nicht direkt aufgerufen.

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "${BACKEND_DIR}/.." && pwd)"
RUN_DIR="${REPO_DIR}/.run"

# Alle Werte ausdrücklich setzen; TWINRAID_MODE hat keinen Standard (Konzept 6.4).
export TWINRAID_MODE=mock
export TWINRAID_CONFIG="${TWINRAID_CONFIG:-${REPO_DIR}/config.dev.yaml}"
export TWINRAID_DATA_DIR="${RUN_DIR}/data"
export TWINRAID_RUNTIME_TLS_DIR="${RUN_DIR}/tls"
export TWINRAID_CUSTOM_TLS_DIR="${RUN_DIR}/custom-tls"
export TWINRAID_WORKER_SOCKET="${RUN_DIR}/worker.sock"
TWINRAID_WEB_UID="$(id -u)"
TWINRAID_WEB_GID="$(id -g)"
export TWINRAID_WEB_UID TWINRAID_WEB_GID
export TWINRAID_FRONTEND_DIR="${REPO_DIR}/frontend/dist"
export TWINRAID_BIND_HOST=127.0.0.1

# Datenablage wie im Container: gehört dem Benutzer, nur er darf schreiben (Konzept 5.9).
mkdir -p "${TWINRAID_DATA_DIR}"
chmod 755 "${TWINRAID_DATA_DIR}"
