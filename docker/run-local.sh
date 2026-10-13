#!/usr/bin/env bash
# Baut das Image und startet den Container lokal im Simulationsmodus,
# mit denselben Härtungsoptionen wie später auf Unraid (Konzept 5.4).
#
# Aufruf aus dem Repo-Hauptordner:   docker/run-local.sh
# Danach im Browser:                 https://127.0.0.1:8443
# Passwort setzen (zweites Terminal): docker exec -it twinraid-dev twinraid-admin set-password
# Beenden mit Strg+C. Die Daten (Zertifikat, Passwort) bleiben im Volume twinraid-dev-data.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${TWINRAID_LOCAL_CONFIG:-${REPO_DIR}/config.dev.yaml}"

docker build -f "${REPO_DIR}/docker/Dockerfile" -t twinraid:dev "${REPO_DIR}"

docker run --rm --name twinraid-dev \
  --read-only \
  --tmpfs /run:rw,exec,nosuid,nodev,mode=0755,size=16m \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --cap-drop=ALL \
  --cap-add=SETUID --cap-add=SETGID --cap-add=SETPCAP --cap-add=KILL --cap-add=DAC_READ_SEARCH \
  --security-opt=no-new-privileges \
  -e TWINRAID_MODE=mock \
  -v "${CONFIG}:/config/twinraid.yaml:ro" \
  --mount type=volume,src=twinraid-dev-data,dst=/data \
  -p 127.0.0.1:8443:8443 \
  twinraid:dev
