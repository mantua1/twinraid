#!/usr/bin/env bash
# Starttest für das Container-Image (Konzept 5.4 und 6.4).
#
# Prüft: Start mit read-only Root und --cap-drop=ALL plus nur den nötigen
# Capabilities, Rechte der Prozesse, Dateirechte, Schutz des Worker-Sockets,
# sauberes Stoppen und dass der Container bei fehlendem/ungültigem Modus oder
# ungültiger Konfiguration nicht startet.
#
# Aufruf aus dem Repo-Hauptordner:
#   docker/smoke-test.sh [IMAGE]          Standard-Image: twinraid:dev
#   CHECK_EACH_CAP=1 docker/smoke-test.sh  prüft zusätzlich, dass jede Capability nötig ist
set -euo pipefail

IMAGE="${1:-twinraid:dev}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${REPO_DIR}/config.example.yaml"
INSPECT_SCRIPT="$(cat "${REPO_DIR}/docker/smoke/inspect_processes.py")"
PORT="${SMOKE_PORT:-18443}"
PREFIX="twinraid-smoke-$$"
WORKDIR="$(mktemp -d)"

# Muss mit docker/run-local.sh und Konzept 5.4 übereinstimmen.
CAPS=(SETUID SETGID SETPCAP KILL DAC_READ_SEARCH)
# Die Kommas gehören zu den tmpfs-Optionen, nicht zur Array-Syntax.
# shellcheck disable=SC2054
HARDENING=(
  --read-only
  --tmpfs /run:rw,exec,nosuid,nodev,mode=0755,size=16m
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m
  --cap-drop=ALL
  --security-opt=no-new-privileges
)

failures=0
pass() { echo "ok    $*"; }
fail() { echo "FAIL  $*" >&2; failures=$((failures + 1)); }
expect_equal() {
  local label="$1" actual="$2" expected="$3"
  if [[ "${actual}" == "${expected}" ]]; then
    pass "${label}"
  else
    fail "${label}: expected '${expected}', got '${actual}'"
  fi
}
expect_contains() {
  local label="$1" haystack="$2" needle="$3"
  if [[ "${haystack}" == *"${needle}"* ]]; then
    pass "${label}"
  else
    fail "${label}: '${needle}' not found in '${haystack}'"
  fi
}

cleanup() {
  local ids
  ids="$(docker ps -aq --filter "name=${PREFIX}")"
  if [[ -n "${ids}" ]]; then
    # shellcheck disable=SC2086
    docker rm -f ${ids} >/dev/null
  fi
  rm -r "${WORKDIR}"
}
trap cleanup EXIT

# Startet einen Container. $1 = Name, $2 = wegzulassende Capability (oder ""),
# $3 = Konfigurationsdatei, Rest = zusätzliche docker-run-Argumente.
start() {
  local name="$1" skip="$2" config="$3" cap
  shift 3
  local cap_args=()
  for cap in "${CAPS[@]}"; do
    [[ "${cap}" == "${skip}" ]] || cap_args+=("--cap-add=${cap}")
  done
  docker run -d --name "${name}" "${HARDENING[@]}" "${cap_args[@]}" \
    -v "${config}:/config/twinraid.yaml:ro" "$@" "${IMAGE}" >/dev/null
}

running() { [[ "$(docker inspect -f '{{.State.Running}}' "$1")" == "true" ]]; }
exit_code() { docker inspect -f '{{.State.ExitCode}}' "$1"; }

wait_for_health() {
  for _ in $(seq 1 30); do
    if [[ "$(curl -s --max-time 2 "http://127.0.0.1:${PORT}/health" || true)" == "ok" ]]; then
      return 0
    fi
    running "$1" || return 1
    sleep 1
  done
  return 1
}

wait_for_exit() {
  for _ in $(seq 1 30); do
    running "$1" || return 0
    sleep 1
  done
  return 1
}

field() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[sys.argv[1]][sys.argv[2]])' "$@"; }

echo "== Image: ${IMAGE}"

# --- 1. Start mit minimalen Capabilities ------------------------------------
main="${PREFIX}-main"
start "${main}" "" "${CONFIG}" -e TWINRAID_MODE=mock -p "127.0.0.1:${PORT}:8443"
if wait_for_health "${main}"; then
  pass "container starts read-only with --cap-drop=ALL + ${CAPS[*]}"
else
  fail "container did not become healthy"
  docker logs "${main}" 2>&1 | tail -30
  exit 1
fi

expect_contains "/api/status answers via worker" \
  "$(curl -s "http://127.0.0.1:${PORT}/api/status")" '"source":"unknown"'
expect_equal "frontend is served" \
  "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/")" "200"
expect_contains "log warns about simulation mode" "$(docker logs "${main}" 2>&1)" "SIMULATION MODE ACTIVE"

# --- 2. Rechte der Prozesse --------------------------------------------------
procs="$(docker exec "${main}" python3 -c "${INSPECT_SCRIPT}")"
check() {
  local proc="$1" key="$2" expected="$3"
  expect_equal "${proc} ${key}=${expected}" "$(field "${proc}" "${key}" <<<"${procs}")" "${expected}"
}
check twinraid-worker Uid 0
check twinraid-worker Gid 10001
check twinraid-worker Groups ""
check twinraid-worker CapEff 0000000000000004
check twinraid-worker CapBnd 0000000000000004
check twinraid-worker CapAmb 0000000000000000
check twinraid-worker NoNewPrivs 1
check twinraid-web Uid 10001
check twinraid-web Gid 10001
check twinraid-web Groups ""
check twinraid-web CapEff 0000000000000000
check twinraid-web CapBnd 0000000000000000
check twinraid-web NoNewPrivs 1

# --- 3. Dateirechte und Dateisystem -----------------------------------------
perm() { docker exec "${main}" stat -c '%a %u:%g' "$1"; }
expect_equal "/srv/worker is 0700 root" "$(perm /srv/worker)" "700 0:0"
expect_equal "socket dir is 0750 root:10001" "$(perm /run/twinraid)" "750 0:10001"
expect_equal "socket is 0660 root:10001" "$(perm /run/twinraid/worker.sock)" "660 0:10001"
if docker exec -u 10001:10001 "${main}" ls /srv/worker >/dev/null 2>&1; then
  fail "web user can read /srv/worker"
else
  pass "web user cannot read /srv/worker"
fi
if docker exec "${main}" touch /app/probe >/dev/null 2>&1; then
  fail "root filesystem is writable"
else
  pass "root filesystem is read-only"
fi

# --- 4. Worker-Socket weist fremde UIDs ab -----------------------------------
probe='import socket;s=socket.socket(socket.AF_UNIX);s.connect("/run/twinraid/worker.sock");s.sendall(b"{\"command\":\"get_status\"}\n");print(s.recv(200).decode())'
expect_contains "worker rejects foreign UID (SO_PEERCRED)" \
  "$(docker exec -u 10002:10001 "${main}" python3 -c "${probe}" 2>&1 || true)" '"error":"forbidden"'
if docker exec -u 10002:10002 "${main}" python3 -c "${probe}" >/dev/null 2>&1; then
  fail "socket reachable without group"
else
  pass "socket not reachable without group 10001"
fi

# --- 5. Sauberes Stoppen -----------------------------------------------------
started=$(date +%s)
docker stop -t 10 "${main}" >/dev/null
duration=$(($(date +%s) - started))
code="$(exit_code "${main}")"
if [[ "${code}" == "0" && "${duration}" -lt 10 ]]; then
  pass "clean stop in ${duration}s (exit 0)"
else
  fail "stop took ${duration}s, exit ${code}"
fi

# --- 6. Kein Start ohne gültigen Modus oder mit ungültiger Konfiguration -----
bad_config="${WORKDIR}/bad.yaml"
sed 's/- medien/- system/' "${CONFIG}" >"${bad_config}"
chmod 0644 "${bad_config}"

refuse() {
  local label="$1" expected="$2" config="$3"
  shift 3
  local name="${PREFIX}-refuse-$((RANDOM))"
  start "${name}" "" "${config}" "$@"
  if wait_for_exit "${name}" && [[ "$(exit_code "${name}")" != "0" ]] \
    && docker logs "${name}" 2>&1 | grep -q "${expected}"; then
    pass "refuses to start: ${label}"
  else
    fail "did not refuse: ${label}"
    docker logs "${name}" 2>&1 | tail -10
  fi
  docker rm -f "${name}" >/dev/null
}
refuse "TWINRAID_MODE missing" "TWINRAID_MODE is not set" "${CONFIG}"
refuse "TWINRAID_MODE invalid" "invalid value" "${CONFIG}" -e TWINRAID_MODE=produktion
refuse "TWINRAID_MODE=real (not yet supported)" "not available in this version" "${CONFIG}" -e TWINRAID_MODE=real
refuse "forbidden share in config" "must never be synced" "${bad_config}" -e TWINRAID_MODE=mock
# Startfehler des Workers: Container hält an, statt den Dienst endlos neu zu starten.
refuse "worker startup error (web UID 0)" "must not be 0" "${CONFIG}" -e TWINRAID_MODE=mock -e TWINRAID_WEB_UID=0

# --- 7. Optional: Jede Capability ist nötig ----------------------------------
if [[ "${CHECK_EACH_CAP:-0}" == "1" ]]; then
  for cap in SETUID SETGID SETPCAP; do
    name="${PREFIX}-no-${cap,,}"
    start "${name}" "${cap}" "${CONFIG}" -e TWINRAID_MODE=mock
    if wait_for_exit "${name}" && [[ "$(exit_code "${name}")" != "0" ]]; then
      pass "${cap} is required (start fails without it)"
    else
      fail "${cap} seems unnecessary"
    fi
    docker rm -f "${name}" >/dev/null
  done
  name="${PREFIX}-no-kill"
  start "${name}" KILL "${CONFIG}" -e TWINRAID_MODE=mock
  sleep 5
  docker stop -t 3 "${name}" >/dev/null
  if [[ "$(exit_code "${name}")" == "137" ]]; then
    pass "KILL is required (clean stop fails without it)"
  else
    fail "KILL seems unnecessary (exit $(exit_code "${name}"))"
  fi
  docker rm -f "${name}" >/dev/null
  echo "info  DAC_READ_SEARCH is not checked: the worker reads no shares before phase 5"
fi

echo
if [[ "${failures}" -gt 0 ]]; then
  echo "${failures} check(s) failed." >&2
  exit 1
fi
echo "All checks passed."
