#!/usr/bin/env bash
# Sync Ende zu Ende (Konzept 4.3 „Sync in TwinRaid“, 5.6 „Test des Syncs“, ab v0.5.2).
#
# TwinRaid läuft gehärtet im Modus real wie auf Unraid (read-only, --cap-drop=ALL plus die
# Capabilities aus Konzept 5.4) mit den beiden Einbindungen des Wächters. Der Wächter
# source-guard.sh läuft als root auf dem Runner mit echtem Docker, dem Testcontainer aus
# host-scripts/twinraid-testcontainer.xml, einem zweiten Container, der SIGTERM ignoriert,
# und einem Pool in einer Datei als Klein. guard.sh empfängt in einen zweiten Pool
# "mainpool" als Groß; statt ssh startet eine Hilfe guard.sh im Modus sync wie sshd, wie in
# docker/source-guard-sync-test.sh. Ein offener TCP-Port auf dem Runner spielt die WebGUI
# von Groß, damit TwinRaid Groß als online sieht.
#
# Gesteuert wird nur über die API von TwinRaid, wie mit den Buttons: Login, CSRF-Token,
# /api/actions/sync_start, /api/sync/<Kennung>/cancel und …/confirm-deletions.
#
# Prüft: vollständigen Sync mit angehaltenen Containern, testdata.sh change, inkrementellen
# Sync, testdata.sh verify-latest und verify-container auf Groß, Freigabe von Löschungen
# (abgelehnt und freigegeben), Abbrechen während des Sendens, den Totmannschalter in der
# Stopp-Phase (TwinRaid mit docker pause angehalten) und das gesperrte Herunterfahren
# während eines Syncs, dazu Aktionslog und Sync-Historie.
#
# Läuft nur auf einem Wegwerf-Rechner (CI-Runner) als root, mit zfsutils-linux und Docker,
# und nur mit SYNC_E2E_TEST_HOST=disposable. Aufruf aus dem Repo-Hauptordner:
#   sudo SYNC_E2E_TEST_HOST=disposable docker/sync-e2e-test.sh [IMAGE]   (Standard twinraid:ci)
set -euo pipefail

IMAGE="${1:-twinraid:ci}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TEMPLATE="${REPO_DIR}/host-scripts/twinraid-testcontainer.xml"
TESTDATA="${REPO_DIR}/host-scripts/testdata.sh"
WORK=/var/tmp/sync-e2e-test
GUARD="${WORK}/guard.sh"
SOURCE_GUARD="${WORK}/source-guard.sh"
KLEIN=twinraid-klein
DATA="/mnt/${KLEIN}/twinraidtest"
ROOT=mainpool/twinraid-mirror
MIRROR="${ROOT}/twinraidtest"
BASE="${WORK}/run/twinraid-source-guard"
REQUEST="${BASE}/in/request"
STATUS="${BASE}/state/status.json"
PERMS=acltype,atime,canmount,checksum,compression,copies,create,dedup,devices,exec,mount,mountpoint,primarycache,quota,readonly,receive:append,recordsize,refquota,refreservation,reservation,secondarycache,setuid,sharenfs,sharesmb,snapdir,sync,userprop,xattr
APP=e2e-twinraid
TEST=twinraid-testcontainer
SLOW=e2e-slow
PORT=18543
API="https://127.0.0.1:${PORT}"
ORIGIN="Origin: ${API}"
WEBGUI_PORT=18080
PASSWORD="sync-e2e-test-password-42"
# Wie docker/smoke-test.sh und Konzept 5.4.
CAPS=(SETUID SETGID SETPCAP KILL DAC_READ_SEARCH)
# shellcheck disable=SC2054 # Die Kommas gehören zu den tmpfs-Optionen.
HARDENING=(
  --read-only
  --tmpfs /run:rw,exec,nosuid,nodev,mode=0755,size=16m
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m
  --cap-drop=ALL
  --security-opt=no-new-privileges
)

failures=0
pass() { echo "ok    $*"; }
fail() {
  echo "FAIL  $*" >&2
  failures=$((failures + 1))
}
expect_equal() {
  if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1: expected '$3', got '$2'"; fi
}
expect_contains() {
  if [[ "$2" == *"$3"* ]]; then pass "$1"; else fail "$1: '$3' not found in '${2:0:800}'"; fi
}
die() {
  echo "sync-e2e-test: $*" >&2
  exit 2
}

if ((EUID != 0)); then
  die "muss als root laufen"
fi
if [[ "${SYNC_E2E_TEST_HOST:-}" != disposable ]]; then
  die "legt Pools und Container an; nur mit SYNC_E2E_TEST_HOST=disposable"
fi
for pool in mainpool "${KLEIN}"; do
  if zpool list -H -o name "${pool}" >/dev/null 2>&1; then
    die "es gibt schon einen Pool ${pool}"
  fi
done

webgui_pid=""
cleanup() {
  local rc=$?
  set +e
  if ((rc != 0 || failures > 0)); then
    echo "--- log of TwinRaid" >&2
    docker logs "${APP}" 2>&1 | tail -60 >&2
    echo "--- syslog Klein" >&2
    tail -40 "${WORK}/syslog-klein.log" >&2
    echo "--- syslog Groß" >&2
    tail -40 "${WORK}/syslog-gross.log" >&2
  fi
  docker unpause "${APP}" >/dev/null 2>&1
  docker rm -f "${APP}" "${TEST}" "${SLOW}" >/dev/null 2>&1
  docker volume rm "${APP}-data" >/dev/null 2>&1
  if [[ -f "${SOURCE_GUARD}" ]]; then
    /bin/bash "${SOURCE_GUARD}" stop >/dev/null 2>&1
  fi
  if [[ -n "${webgui_pid}" ]]; then
    kill "${webgui_pid}" 2>/dev/null
  fi
  pkill -f "${WORK}/" 2>/dev/null
  umount "${BASE}/in" 2>/dev/null
  zpool destroy -f mainpool 2>/dev/null
  zpool destroy -f "${KLEIN}" 2>/dev/null
  rm -rf "${WORK}"
  exit "${rc}"
}
trap cleanup EXIT

# --- Hilfen ------------------------------------------------------------------------------

value() { zfs get -H -o value -- "$1" "$2"; }
snapshot_count() { zfs list -H -t snapshot -d 1 -o name -- "$1" 2>/dev/null | grep -c '@' || true; }
own_snapshot_count() {
  zfs list -H -t snapshot -d 1 -o name,twinraid:created-by -- "$1" | awk -F'\t' '$2 == "source-guard"' | grep -c '' || true
}
running_state() { docker inspect --type container --format '{{.State.Running}}' -- "$1" 2>/dev/null || echo missing; }

# Ein Feld aus einer JSON-Datei, Schlüssel mit Punkten getrennt; null für fehlende Werte.
json_field() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
try:
    value = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    print("unreadable")
    raise SystemExit(0)
for key in sys.argv[2].split("."):
    if value is None:
        break
    if isinstance(value, list):
        value = value[int(key)] if int(key) < len(value) else None
    else:
        value = value.get(key)
if value is None:
    print("null")
elif isinstance(value, bool):
    print(str(value).lower())
elif isinstance(value, (dict, list)):
    print(json.dumps(value, sort_keys=True))
else:
    print(value)
PY
}
guard_field() { json_field "${STATUS}" "$1"; }

# Status von TwinRaid über die API (wie das Dashboard); danach liest field daraus.
refresh() {
  curl -s --cacert "${WORK}/ca.pem" -b "${WORK}/cookies.txt" "${API}/api/status" >"${WORK}/api-status.json"
}
field() { json_field "${WORK}/api-status.json" "$1"; }
post() {
  curl -s -o /dev/null -w '%{http_code}' --cacert "${WORK}/ca.pem" -b "${WORK}/cookies.txt" \
    -H "${ORIGIN}" -H "X-CSRF-Token: ${csrf}" -X POST "${API}$1"
}

# Wartet, bis ein Feld im Status von TwinRaid einen Wert hat.
wait_field() {
  local key="$1" expected="$2" limit="${3:-60}" i got=""
  for ((i = 0; i < limit * 2; i++)); do
    refresh
    got="$(field "${key}")"
    if [[ "${got}" == "${expected}" ]]; then
      return 0
    fi
    sleep 0.5
  done
  fail "wait for ${key}: expected '${expected}', got '${got}'"
  return 1
}

# Vor jedem Lauf: Pause des Wächters aufheben (nur root auf Klein kann das) und den
# Mindestabstand von 10 min auf Groß zurückstellen, wie es nach 11 Minuten wäre.
next_run() {
  local newest
  /bin/bash "${SOURCE_GUARD}" reset-run-interval >/dev/null
  newest="$(zfs list -H -t snapshot -d 1 -o name -s createtxg -- "${MIRROR}" 2>/dev/null | tail -1)"
  if [[ -n "${newest}" ]]; then
    zfs set -- "twinraid:received=$(($(date +%s) - 660))" "${newest}"
  fi
  # Snapshot-Namen haben Sekunden; zwei Läufe in derselben Sekunde gäbe es auf Klein nie.
  sleep 1
  wait_field actions.availability.sync_start null 60 || true
}

# Startet den Sync wie der Button und merkt sich die Kennung des Laufs in JOB_ID.
start_sync() {
  expect_equal "$1: sync_start accepted" "$(post /api/actions/sync_start)" "202"
  refresh
  JOB_ID="$(field sync.running.job_id)"
}

# Wartet auf das Ende des Laufs; SHUTDOWN_SEEN hält fest, wie die Verfügbarkeit von
# „Groß herunterfahren“ während des Laufs lautete, STEPS die gesehenen Schritte.
wait_done() {
  local limit="${1:-240}" i step
  STEPS=" "
  SHUTDOWN_SEEN=" "
  for ((i = 0; i < limit * 2; i++)); do
    refresh
    step="$(field sync.running.step)"
    if [[ "${step}" == null ]]; then
      return 0
    fi
    [[ "${STEPS}" == *" ${step} "* ]] || STEPS+="${step} "
    local shutdown
    shutdown="$(field actions.availability.shutdown_target)"
    [[ "${SHUTDOWN_SEEN}" == *" ${shutdown} "* ]] || SHUTDOWN_SEEN+="${shutdown} "
    sleep 0.5
  done
  fail "sync did not end within ${limit} s (step ${step})"
}
outcome() { echo "$(field sync.last.kind):$(field sync.last.outcome):$(field sync.last.reason)"; }

echo "== Vorbereitung"
docker image inspect "${IMAGE}" >/dev/null || die "Image ${IMAGE} fehlt"
mkdir -p "${WORK}/run" "${WORK}/var-run"
truncate -s 4G "${WORK}/mainpool.img" "${WORK}/klein.img"
zpool create -o cachefile=none -o ashift=12 -m /mnt/mainpool \
  -O compression=on -O atime=off -O xattr=sa -O acltype=posix mainpool "${WORK}/mainpool.img"
zfs create "${ROOT}"
zfs allow -u 64000 "${PERMS}" "${ROOT}"
printf 'mirror twinraidtest\n' >"${WORK}/mirror.conf"
zpool create -o cachefile=none -o ashift=12 -m "/mnt/${KLEIN}" -O compression=on \
  -O xattr=sa -O acltype=posix "${KLEIN}" "${WORK}/klein.img"
zfs create "${KLEIN}/twinraidtest"
bash "${TESTDATA}" create "${DATA}/testdata" >/dev/null
# Mehr Dateien, als max_deletions erlaubt; gelöscht werden sie für die Freigabe.
mkdir "${DATA}/viele"
for i in $(seq 1 70); do printf 'Datei %s\n' "${i}" >"${DATA}/viele/datei${i}.txt"; done
mkdir "${DATA}/testcontainer"

cp /etc/passwd "${WORK}/passwd"
cp /etc/group "${WORK}/group"
# shellcheck disable=SC2016 # $4 gehört in das erzeugte Skript (logger -t … -- Meldung).
printf '#!/bin/sh\nprintf "%%s\\n" "$4" >>%s\n' "${WORK}/syslog-gross.log" >"${WORK}/logger-gross"
# shellcheck disable=SC2016 # wie oben
printf '#!/bin/sh\nprintf "%%s\\n" "$4" >>%s\n' "${WORK}/syslog-klein.log" >"${WORK}/logger-klein"
chmod 0755 "${WORK}/logger-gross" "${WORK}/logger-klein"
sed -e "s|^readonly LOGGER=.*|readonly LOGGER=${WORK}/logger-gross|" \
  -e "s|^readonly MIRROR_CONF=.*|readonly MIRROR_CONF=${WORK}/mirror.conf|" \
  -e "s|^readonly PASSWD=.*|readonly PASSWD=${WORK}/passwd|" \
  -e "s|^readonly GROUP=.*|readonly GROUP=${WORK}/group|" \
  -e "s|^readonly RECEIVE_LOCK=.*|readonly RECEIVE_LOCK=${WORK}/receive.lock|" \
  "${REPO_DIR}/host-scripts/guard.sh" >"${GUARD}"
expect_equal "guard.sh: only the test values differ" \
  "$(diff "${REPO_DIR}/host-scripts/guard.sh" "${GUARD}" | grep -c '^>' || true)" 5
sed -e "s|^readonly CONF=.*|readonly CONF=${WORK}/source-guard.conf|" \
  -e "s|^readonly BASE_DIR=.*|readonly BASE_DIR=${BASE}|" \
  -e "s|^readonly BASE_DIR_ALIAS=.*|readonly BASE_DIR_ALIAS=${WORK}/var-run/twinraid-source-guard|" \
  -e "s|^readonly LOGGER=.*|readonly LOGGER=${WORK}/logger-klein|" \
  -e "s|^readonly SSH=.*|readonly SSH=${WORK}/ssh|" \
  -e "s|^readonly SYNC_KEY=.*|readonly SYNC_KEY=${WORK}/ssh-sync|" \
  -e "s|^readonly KNOWN_HOSTS=.*|readonly KNOWN_HOSTS=${WORK}/target-known-hosts|" \
  -e "s|^readonly POOL=.*|readonly POOL=${KLEIN}|" \
  -e "s|^readonly SELF_DATASET=.*|readonly SELF_DATASET=${KLEIN}/appdata/twinraid|" \
  -e "s|^readonly SNAPSHOT_GAP=.*|readonly SNAPSHOT_GAP=0|" \
  "${REPO_DIR}/host-scripts/source-guard.sh" >"${SOURCE_GUARD}"
expect_equal "source guard: only the test values differ" \
  "$(diff "${REPO_DIR}/host-scripts/source-guard.sh" "${SOURCE_GUARD}" | grep -c '^>' || true)" 10

# Statt ssh: feste Form prüfen, dann guard.sh wie sshd mit command="… guard.sh sync". Eine
# Verzögerung in receive-delay hält nur den Empfang auf, nicht check (für das Abbrechen).
cat >"${WORK}/ssh" <<EOF
#!/bin/bash
set -euo pipefail
args=("\$@")
if ((\${#args[@]} != 29)) || [[ "\${args[0]} \${args[1]} \${args[2]} \${args[26]}" != "-F /dev/null -i --" ]]; then
  echo "unexpected ssh call" >&2
  exit 255
fi
if [[ "\${args[28]}" == "receive "* ]]; then
  sleep "\$(cat "${WORK}/receive-delay" 2>/dev/null || echo 0)"
fi
SSH_ORIGINAL_COMMAND="\${args[28]}" exec /bin/bash "${GUARD}" sync
EOF
chmod 0755 "${WORK}/ssh"
printf 'fake key\n' >"${WORK}/ssh-sync"
printf '192.0.2.11 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake\n' >"${WORK}/target-known-hosts"
cat >"${WORK}/source-guard.conf" <<EOF
allow ${TEST}
allow ${SLOW}
max_stop_minutes 5
heartbeat_timeout_seconds 30
stop_timeout_seconds 10
min_run_interval_minutes 10
dataset twinraidtest
max_deletions 60
target_host 192.0.2.11
snapshot_keep_days 7
EOF

# Testcontainer aus dem Template (eine Quelle für Unraid und Test) und ein Container, der
# SIGTERM ignoriert: docker stop dauert bei ihm stop_timeout_seconds.
template_value() {
  python3 - "${TEMPLATE}" "$1" <<'PY'
import sys
import xml.etree.ElementTree as ET
value = ET.parse(sys.argv[1]).getroot().findtext(sys.argv[2]) or ""
if sys.argv[2] == "PostArgs":
    value = value.removeprefix("sh -c '").removesuffix("'")
print(value)
PY
}
BUSYBOX="$(template_value Repository)"
[[ "${BUSYBOX}" =~ ^busybox:[0-9.]+@sha256:[0-9a-f]{64}$ ]] || die "Image im Template ist nicht gepinnt"
docker run -d --name "${TEST}" --network none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges -v "${DATA}/testcontainer:/data" \
  "${BUSYBOX}" sh -c "$(template_value PostArgs)" >/dev/null
docker run -d --name "${SLOW}" --network none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges "${BUSYBOX}" sh -c 'trap "" TERM; while true; do sleep 1; done' >/dev/null

/bin/bash "${SOURCE_GUARD}" start
for _ in $(seq 1 120); do
  [[ "$(guard_field zfs.state):$(guard_field allowed.1.state)" == ok:running ]] && break
  sleep 0.5
done
expect_equal "source guard active with both containers" \
  "$(guard_field error):$(guard_field zfs.state):$(guard_field allowed.0.state):$(guard_field allowed.1.state)" \
  "null:ok:running:running"

# WebGUI von Groß: nur ein offener Port auf dem Runner, erreichbar über das Gateway der Bridge.
GATEWAY="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
python3 - "${GATEWAY}" "${WEBGUI_PORT}" <<'PY' &
import socket
import sys

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((sys.argv[1], int(sys.argv[2])))
server.listen(16)
while True:
    connection, _ = server.accept()
    connection.close()
PY
webgui_pid=$!

cat >"${WORK}/twinraid.yaml" <<EOF
web:
  allowed_networks:
    # Docker-Bridge: Anfragen vom Runner über die Portweiterleitung kommen von dort.
    - 172.16.0.0/12
  session_idle_minutes: 60
  default_language: de
  tls:
    ip_addresses:
      - 127.0.0.1
servers:
  source:
    name: "Unraid Klein"
    host: 192.0.2.10
    webgui:
      scheme: http
  target:
    name: "Unraid Groß"
    host: ${GATEWAY}
    webgui:
      scheme: http
      port: ${WEBGUI_PORT}
    mac: "02:00:00:00:00:01"
    boot_timeout_minutes: 5
gaming_vm:
  display_name: "Gaming-VM"
  shutdown_timeout_minutes: 3
actions:
  shutdown: true
  sync: true
sync:
  shares:
    - twinraidtest
  stop_containers: true
  never_stop: []
  copy_templates: false
  schedule:
    enabled: false
    timezone: "Europe/Berlin"
    days: [sun]
    time: "03:00"
    wake_target: false
    shutdown_after: false
EOF
chmod 0644 "${WORK}/twinraid.yaml"
cap_args=()
for cap in "${CAPS[@]}"; do cap_args+=("--cap-add=${cap}"); done
docker run -d --name "${APP}" "${HARDENING[@]}" "${cap_args[@]}" \
  -v "${WORK}/twinraid.yaml:/config/twinraid.yaml:ro" \
  --mount "type=volume,src=${APP}-data,dst=/data" \
  -v "${REQUEST}:/srv/worker/source-guard/request:rw" \
  -v "${BASE}/state:/srv/worker/source-guard/state:ro" \
  -e TWINRAID_MODE=real -p "127.0.0.1:${PORT}:8443" "${IMAGE}" >/dev/null
for _ in $(seq 1 60); do
  [[ "$(curl -sk --max-time 2 "${API}/health" || true)" == ok ]] && break
  [[ "$(running_state "${APP}")" == true ]] || die "TwinRaid hat sich beendet (Log unten)"
  sleep 1
done
[[ "$(curl -sk --max-time 2 "${API}/health" || true)" == ok ]] || die "TwinRaid antwortet nicht"
docker exec "${APP}" cat /run/twinraid-tls/ca.pem >"${WORK}/ca.pem"
docker exec -i "${APP}" twinraid-admin set-password --stdin <<<"${PASSWORD}" >/dev/null
csrf="$(curl -s -c "${WORK}/cookies.txt" --cacert "${WORK}/ca.pem" -H "${ORIGIN}" \
  -H "Content-Type: application/json" -d "{\"password\": \"${PASSWORD}\"}" "${API}/api/auth/login" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])')"
procs="$(docker exec "${APP}" /app/.venv/bin/python -c "$(cat "${REPO_DIR}/docker/smoke/inspect_processes.py")")"
expect_equal "worker without capabilities (Konzept 5.4)" \
  "$(python3 -c 'import json,sys; w=json.load(sys.stdin)["twinraid-worker"]; print(w["CapEff"], w["CapBnd"])' <<<"${procs}")" \
  "0000000000000000 0000000000000000"
wait_field source_guard.state active 60 && pass "TwinRaid reads the source guard as active"
wait_field target.state online 60 && pass "Groß is online (open WebGUI port)"
wait_field actions.availability.sync_start null 60 && pass "sync is available"

echo "== Vollständiger Sync mit angehaltenen Containern"
start_sync "first sync"
wait_done 300
expect_equal "first sync" "$(outcome)" "sync:success:null"
expect_contains "containers were stopped during the sync" "${STEPS}" " stopping_containers "
expect_contains "shutting down Groß is locked during the sync" "${SHUTDOWN_SEEN}" " sync_running "
expect_equal "history: kind, result, stopped containers" \
  "$(field sync.history.0.kind):$(field sync.history.0.result):$(field sync.history.0.containers_stopped)" \
  "sync:success:2"
expect_equal "history: full stream, sent" \
  "$(field sync.history.0.shares.0.mode):$(field sync.history.0.shares.0.result)" "full:ok"
snap="$(field sync.report.snapshot)"
expect_equal "mirror on Groß has the snapshot" "$(value guid "${MIRROR}@${snap}")" \
  "$(value guid "${KLEIN}/twinraidtest@${snap}")"
expect_contains "test data arrive unchanged" "$(bash "${TESTDATA}" verify-latest "${MIRROR}" 2>&1 || true)" \
  "Keine Abweichung (Lauf 0)."
expect_contains "test container was stopped cleanly for the snapshot" \
  "$(bash "${TESTDATA}" verify-container "${MIRROR}" 2>&1 || true)" "sauber angehalten"
expect_equal "containers run again" "$(running_state "${TEST}"):$(running_state "${SLOW}")" "true:true"
expect_equal "TwinRaid was never stopped" "$(running_state "${APP}")" "true"
action_log="$(docker exec "${APP}" twinraid-admin show-action-log 2>&1)"
expect_contains "action log: containers stopped" "${action_log}" "source_guard_stop"
expect_contains "action log: two containers" "${action_log}" "2 containers"
expect_contains "action log: containers started again" "${action_log}" "source_guard_finish"

echo "== testdata.sh change, inkrementeller Sync"
bash "${TESTDATA}" change "${DATA}/testdata" >/dev/null
next_run
start_sync "incremental sync"
wait_done 300
expect_equal "incremental sync" "$(outcome)" "sync:success:null"
expect_equal "incremental stream" "$(field sync.report.shares.0.mode):$(field sync.report.shares.0.base_type)" \
  "incremental:snapshot"
deleted="$(field sync.report.shares.0.deleted)"
if [[ "${deleted}" =~ ^[0-9]+$ ]] && ((deleted >= 50 && deleted <= 60)); then
  pass "deletions of testdata.sh change counted (${deleted})"
else
  fail "deletions counted: ${deleted}"
fi
expect_contains "changed test data in the newest snapshot on Groß" \
  "$(bash "${TESTDATA}" verify-latest "${MIRROR}" 2>&1 || true)" "Keine Abweichung (Lauf 1)."

echo "== Löschungen über max_deletions: abgelehnt"
rm -r "${DATA}/viele"
next_run
before="$(snapshot_count "${MIRROR}")"
start_sync "sync with many deletions"
wait_field sync.running.step awaiting_confirm 180 && pass "sync waits for the confirmation"
expect_contains "confirmation names the deletions" "$(field sync.running.confirmation.reasons)" '"deletions"'
expect_equal "deletions and limit" \
  "$(field sync.running.confirmation.deletions):$(field sync.running.confirmation.max_deletions)" "71:60"
expect_equal "deadline from the job" "$(field sync.running.confirmation.deadline | grep -c '^20')" "1"
expect_equal "job running while waiting" "$(field job_running)" "true"
expect_equal "confirm with a foreign id is refused" \
  "$(post /api/sync/0123456789abcdef0123456789abcdef/confirm-deletions)" "409"
expect_equal "decline" "$(post "/api/sync/${JOB_ID}/cancel")" "202"
wait_done 60
expect_equal "declined" "$(outcome)" "sync:deletions_declined:null"
expect_equal "nothing new on Groß" "$(snapshot_count "${MIRROR}")" "${before}"
expect_equal "job closed on Klein" "$(guard_field job.state):$(guard_field job.result)" "closed:closed"
expect_equal "deleted files still on Groß" "$(find /mnt/mainpool/twinraid-mirror/twinraidtest/viele -type f | wc -l)" "70"

echo "== Löschungen über max_deletions: freigegeben"
next_run
start_sync "sync with confirmed deletions"
wait_field sync.running.step awaiting_confirm 180 && pass "sync waits for the confirmation again"
expect_equal "confirm" "$(post "/api/sync/${JOB_ID}/confirm-deletions")" "202"
wait_done 300
expect_equal "confirmed sync" "$(outcome)" "sync:success:null"
expect_equal "deleted files gone on Groß" "$(find /mnt/mainpool/twinraid-mirror/twinraidtest -maxdepth 1 -name viele | wc -l)" "0"
expect_equal "older snapshot keeps them" \
  "$(find "/mnt/mainpool/twinraid-mirror/twinraidtest/.zfs/snapshot/${snap}/viele" -type f | wc -l)" "70"

echo "== Abbrechen während des Sendens"
printf 'noch eine Änderung\n' >"${DATA}/neu.txt"
echo 600 >"${WORK}/receive-delay"
next_run
before="$(snapshot_count "${MIRROR}")"
start_sync "sync to cancel"
wait_field sync.running.step sending 180 && pass "sync is sending"
expect_equal "shutting down Groß is locked while sending" "$(field actions.availability.shutdown_target)" "sync_running"
expect_equal "dry run is busy while sending" "$(field actions.availability.sync_dry_run)" "busy"
total="$(field sync.running.progress.total)"
if [[ "${total}" =~ ^[0-9]+$ ]] && ((total > 0)); then pass "progress has a total (${total})"; else fail "progress total: ${total}"; fi
expect_equal "cancel" "$(post "/api/sync/${JOB_ID}/cancel")" "202"
wait_done 60
expect_equal "cancelled" "$(outcome)" "sync:cancelled:null"
expect_equal "job closed on Klein" "$(guard_field job.state):$(guard_field job.result)" "closed:closed"
sleep 1
expect_equal "no zfs send left" "$(pgrep -f "zfs send -L -c -e -v -P" | wc -l)" "0"
expect_equal "nothing new on Groß" "$(snapshot_count "${MIRROR}")" "${before}"
expect_equal "receive lock free on Groß" "$(flock -n "${WORK}/receive.lock" true && echo free)" "free"
rm -f "${WORK}/receive-delay"

echo "== Totmannschalter in der Stopp-Phase"
# Der Wächter liest die Zustände der Container nur alle 15 s neu; ohne laufende Kandidaten
# wäre die Stopp-Phase vorbei, bevor TwinRaid angehalten ist.
for _ in $(seq 1 60); do
  [[ "$(guard_field allowed.0.state):$(guard_field allowed.1.state)" == running:running ]] && break
  sleep 0.5
done
next_run
own_before="$(own_snapshot_count "${KLEIN}/twinraidtest")"
start_sync "sync with a hanging TwinRaid"
for _ in $(seq 1 120); do
  [[ "$(guard_field run)" != null ]] && break
  sleep 0.25
done
expect_equal "run open on Klein" "$([[ "$(guard_field run)" != null ]] && echo open)" "open"
# TwinRaid hängt: kein ping mehr. Der Wächter beendet den Lauf nach heartbeat_timeout_seconds.
docker pause "${APP}" >/dev/null
for _ in $(seq 1 120); do
  [[ "$(guard_field last_run.reason)" == deadman_heartbeat ]] && break
  sleep 1
done
expect_equal "deadman of the source guard" "$(guard_field last_run.reason)" "deadman_heartbeat"
docker unpause "${APP}" >/dev/null
wait_done 120
expect_equal "sync ends before the snapshot" "$(outcome)" "sync:source_guard_refused:no_run"
expect_equal "no snapshot on Klein" "$(own_snapshot_count "${KLEIN}/twinraidtest")" "${own_before}"
for _ in $(seq 1 30); do
  [[ "$(running_state "${TEST}"):$(running_state "${SLOW}")" == true:true ]] && break
  sleep 1
done
expect_equal "deadman started the containers again" "$(running_state "${TEST}"):$(running_state "${SLOW}")" "true:true"

echo "== Historie und Aktionslog"
refresh
expect_equal "history, newest first" \
  "$(python3 -c 'import json,sys; print(" ".join(e["result"] for e in json.load(open(sys.argv[1]))["sync"]["history"]))' "${WORK}/api-status.json")" \
  "source_guard_refused cancelled success deletions_declined success success"
sync_log="$(docker exec "${APP}" twinraid-admin show-sync-log 2>&1)"
expect_contains "show-sync-log lists the runs" "${sync_log}" "deletions_declined"
expect_contains "show-sync-log names stopped containers" "${sync_log}" "2 containers stopped"
expect_equal "never a destroy with -r" "$(zpool history "${KLEIN}" mainpool | grep -c 'zfs destroy -r' || true)" "0"
expect_equal "never a receive with -F" "$(zpool history mainpool | grep -c 'receive -F' || true)" "0"

echo
if ((failures > 0)); then
  echo "${failures} check(s) failed" >&2
  exit 1
fi
echo "all checks passed"
