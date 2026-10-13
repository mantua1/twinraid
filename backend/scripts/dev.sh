#!/usr/bin/env bash
# Startet Worker und Web-Prozess lokal im Simulationsmodus (ohne Container).
# Aufruf aus dem Ordner backend:  ./scripts/dev.sh
# Beenden mit Strg+C.
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "${BACKEND_DIR}/.." && pwd)"

# Alle Werte ausdrücklich setzen; TWINRAID_MODE hat keinen Standard (Konzept 6.4).
export TWINRAID_MODE=mock
export TWINRAID_CONFIG="${TWINRAID_CONFIG:-${REPO_DIR}/config.example.yaml}"
export TWINRAID_WORKER_SOCKET="${REPO_DIR}/.run/worker.sock"
export TWINRAID_WEB_UID="$(id -u)"
export TWINRAID_FRONTEND_DIR="${REPO_DIR}/frontend/dist"
export TWINRAID_BIND_HOST=127.0.0.1

cd "${BACKEND_DIR}"
uv run twinraid-preflight
uv run twinraid-worker &
WORKER_PID=$!
trap 'kill "${WORKER_PID}" 2>/dev/null || true; wait "${WORKER_PID}" 2>/dev/null || true' EXIT
uv run twinraid-web
