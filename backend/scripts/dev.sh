#!/usr/bin/env bash
# Startet Worker und Web-Prozess lokal im Simulationsmodus (ohne Container).
# Aufruf aus dem Ordner backend:  ./scripts/dev.sh
# Danach im Browser:              https://127.0.0.1:8443
# Passwort setzen:                ./scripts/dev-admin.sh set-password
# Beenden mit Strg+C.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=dev-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/dev-env.sh"

cd "${BACKEND_DIR}"
uv run twinraid-preflight
uv run twinraid-init
uv run twinraid-worker &
WORKER_PID=$!
trap 'kill "${WORKER_PID}" 2>/dev/null || true; wait "${WORKER_PID}" 2>/dev/null || true' EXIT
uv run twinraid-web
