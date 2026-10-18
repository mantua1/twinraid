#!/usr/bin/env bash
# Test des Wächters host-scripts/source-guard.sh mit echtem Docker, echtem tmpfs und
# echten Bind-Mounts (Konzept 3.6 und 5.6).
#
# Der Wächter läuft wie auf Unraid Klein als root auf dem Host, mit seinen festen
# Pfaden /boot/config/twinraid und /run/twinraid-source-guard. Deshalb läuft dieser
# Test nur auf einem Wegwerf-Rechner, etwa dem CI-Runner, und nur mit
# SOURCE_GUARD_TEST_HOST=disposable. Er hält zwischendurch den Docker-Dienst an.
# Ein nachgebautes TwinRaid (busybox mit denselben Einbindungen und ohne
# Capabilities) schreibt die Aufträge von innen.
#
# Prüft: tmpfs und Rechte der Ablage, Start ohne Docker (docker_unavailable, ohne
# Abbruch), einen Lauf mit Anhalten und Wiederstarten des Testcontainers aus
# host-scripts/twinraid-testcontainer.xml, Ablehnungen (nicht freigegeben, nicht
# laufend, nicht vorhanden, ID-Präfix eines anderen Containers, TwinRaid nach Image,
# Label und Einbindung), Angriffe aus dem Container (löschen, umbenennen, Symlink,
# chown, Schreiben in state/, übergroße Datei, falsche Prüfsumme, Shell-Zeichen, alte
# Nummer) und dass ihr Inhalt nie in Syslog oder status.json landet, Hardlink und
# fremden Besitzer auf dem Host, den Totmannschalter, selftest, Neustarts und
# Auftragsnummern, den verlorenen Zustand, das Verzeichnis, das Docker anlegt, wenn
# TwinRaid vor dem Wächter startet, und die Merkmale von Unraid Connect.
#
# Aufruf aus dem Repo-Hauptordner:
#   sudo SOURCE_GUARD_TEST_HOST=disposable docker/source-guard-test.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
GUARD="${REPO_DIR}/host-scripts/source-guard.sh"
TEMPLATE="${REPO_DIR}/host-scripts/twinraid-testcontainer.xml"
BASE=/run/twinraid-source-guard
REQUEST="${BASE}/in/request"
STATUS="${BASE}/state/status.json"
CONF_DIR=/boot/config/twinraid
CONF="${CONF_DIR}/source-guard.conf"
PLUGIN=/boot/config/plugins/dynamix.unraid.net.plg
FLASH_INI=/var/local/emhttp/flashbackup.ini
DATA="$(mktemp -d)"
TEST=twinraid-testcontainer
APP=sg-twinraid
OTHER=sg-other
STOPPED=sg-stopped
SELF_IMAGE_TAG=ghcr.io/mantua1/twinraid:sg-test

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
  if [[ "$2" == *"$3"* ]]; then pass "$1"; else fail "$1: '$3' not found in '${2:0:400}'"; fi
}
expect_not_contains() {
  if [[ "$2" != *"$3"* ]]; then pass "$1"; else fail "$1: '$3' must not appear"; fi
}
die() {
  echo "source-guard-test: $*" >&2
  exit 2
}

if ((EUID != 0)); then
  die "muss als root laufen"
fi
if [[ "${SOURCE_GUARD_TEST_HOST:-}" != disposable ]]; then
  die "schreibt nach /boot/config und /run und hält Docker an; nur mit SOURCE_GUARD_TEST_HOST=disposable"
fi
if [[ -e "${CONF}" || -e "${BASE}" ]]; then
  die "${CONF} oder ${BASE} gibt es schon; dieser Rechner ist kein Wegwerf-Rechner"
fi

# Image und Befehl des Testcontainers stehen nur im Template (eine Quelle für Unraid und Test).
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
TEST_SCRIPT="$(template_value PostArgs)"
[[ "${BUSYBOX}" =~ ^busybox:[0-9.]+@sha256:[0-9a-f]{64}$ ]] || die "Image im Template ist nicht gepinnt"

guard() { /bin/bash "${GUARD}" "$@"; }

# Ein Feld aus status.json, Schlüssel mit Punkten getrennt; null für fehlende Werte.
status_field() {
  python3 - "${STATUS}" "$1" <<'PY'
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
    value = value[int(key)] if isinstance(value, list) else value.get(key)
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

wait_status() {
  local key="$1" expected="$2" limit="${3:-15}" i value=""
  for ((i = 0; i < limit * 4; i++)); do
    value="$(status_field "${key}")"
    if [[ "${value}" == "${expected}" ]]; then
      return 0
    fi
    sleep 0.25
  done
  echo "wait_status ${key}: expected '${expected}', got '${value}'" >&2
  return 1
}

order_line() {
  local seq="$1" text="$2" sum="${3:-}" payload line
  payload="TRSG1 ${seq} ${text}"
  if [[ -z "${sum}" ]]; then
    sum="$(printf '%s' "${payload}" | sha256sum)"
    sum="${sum:0:16}"
  fi
  printf -v line '%-255s\n' "${payload} ${sum}"
  printf '%s' "${line}"
}

# Schreibt wie TwinRaid von innen: Datei kürzen und in einem Zug schreiben.
from_app() { docker exec -i "${APP}" sh -c 'cat > /srv/worker/source-guard/request'; }

next_seq() { echo $(($(status_field last_seq) + 1)); }

# Auftrag aus dem nachgebauten TwinRaid; setzt RESULT.
send() {
  local seq
  seq="$(next_seq)"
  order_line "${seq}" "$1" | from_app
  if ! wait_status last.seq "${seq}" "${2:-30}"; then
    RESULT=timeout
    return 0
  fi
  RESULT="$(status_field last.result)"
}

# Prüft, dass wait_status den erwarteten Wert erreicht.
expect_wait() {
  local label="$1" key="$2" expected="$3" limit="${4:-15}"
  if wait_status "${key}" "${expected}" "${limit}"; then
    pass "${label}"
  else
    fail "${label}"
  fi
}

# Schreibt den Inhalt einer Datei wie TwinRaid und erwartet eine Ablehnung.
expect_rejected() {
  local label="$1" file="$2" before seq
  before="$(status_field rejected.count)"
  seq="$(status_field last_seq)"
  from_app <"${file}"
  expect_wait "${label}: rejected" rejected.count $((before + 1))
  expect_equal "${label}: number unchanged" "$(status_field last_seq)" "${seq}"
}

running_state() { docker inspect --type container --format '{{.State.Running}}' -- "$1" 2>/dev/null || echo missing; }
syslog() { journalctl -t twinraid-source-guard --no-pager -o cat 2>/dev/null || true; }

start_app() {
  docker run -d --name "${APP}" --network none --cap-drop=ALL --security-opt=no-new-privileges \
    -v "${REQUEST}:/srv/worker/source-guard/request" \
    -v "${BASE}/state:/srv/worker/source-guard/state:ro" \
    "${BUSYBOX}" sleep 86400 >/dev/null
}

remove_layout() {
  docker rm -f "${APP}" >/dev/null 2>&1 || true
  if findmnt -n -M "${BASE}/in" >/dev/null 2>&1; then
    umount "${BASE}/in"
  fi
  rm -rf "${BASE}"
}

cleanup() {
  guard stop >/dev/null 2>&1 || true
  pkill -f "${GUARD} (supervise|run)" 2>/dev/null || true
  systemctl start docker.socket docker.service >/dev/null 2>&1 || true
  docker rm -f "${TEST}" "${APP}" "${OTHER}" "${STOPPED}" sg-twin-image sg-twin-label sg-twin-mount \
    >/dev/null 2>&1 || true
  docker rmi "${SELF_IMAGE_TAG}" >/dev/null 2>&1 || true
  if findmnt -n -M "${BASE}/in" >/dev/null 2>&1; then
    umount "${BASE}/in" || true
  fi
  rm -rf "${BASE}" "${CONF_DIR}" "${PLUGIN}" "${FLASH_INI}" "${DATA}"
}
trap cleanup EXIT

write_conf() { mkdir -p "${CONF_DIR}" && printf '%s' "$1" >"${CONF}"; }

echo "== Vorbereitung"
docker pull -q "${BUSYBOX}" >/dev/null
docker tag "${BUSYBOX}" "${SELF_IMAGE_TAG}"
write_conf "allow ${TEST}
heartbeat_timeout_seconds 30
stop_timeout_seconds 10
"

echo "== Start ohne Docker"
systemctl stop docker.socket docker.service
guard start
expect_wait "docker_unavailable while docker is down" error docker_unavailable 30
first="$(status_field heartbeat_at)"
sleep 6
second="$(status_field heartbeat_at)"
if ((second > first)); then pass "heartbeat continues without docker"; else fail "heartbeat stopped"; fi
expect_contains "second start is refused" "$(guard start)" "läuft bereits"
expect_equal "exactly one supervisor" "$(pgrep -c -f "${GUARD} supervise")" 1
systemctl start docker.socket docker.service
expect_wait "error clears once docker is up" error null 60
expect_contains "syslog: docker available" "$(syslog)" "docker available"

echo "== Ablage"
expect_equal "in/ is tmpfs" "$(findmnt -n -o FSTYPE -M "${BASE}/in")" tmpfs
options="$(findmnt -n -o OPTIONS -M "${BASE}/in")"
for option in nosuid nodev noexec size=64k mode=700; do
  expect_contains "tmpfs option ${option}" ",${options}," ",${option},"
done
expect_equal "request: regular empty file of root, 0600" "$(stat -c '%F %U %a' "${REQUEST}")" \
  "regular empty file root 600"
expect_equal "state/: root, 0755" "$(stat -c '%U %a' "${BASE}/state")" "root 755"
expect_equal "private/: root, 0700" "$(stat -c '%U %a' "${BASE}/private")" "root 700"
expect_equal "status.json: 0644" "$(stat -c '%a' "${STATUS}")" 644
expect_equal "protocol 1" "$(status_field protocol)" 1
expect_equal "zfs unavailable on the runner, guard still active" "$(status_field zfs.state)" unavailable

echo "== Container und Konfiguration"
mkdir -p "${DATA}/testcontainer"
docker run -d --name "${TEST}" --network none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges -v "${DATA}/testcontainer:/data" "${BUSYBOX}" \
  sh -c "${TEST_SCRIPT}" >/dev/null
docker run -d --name "${OTHER}" --network none "${BUSYBOX}" sleep 86400 >/dev/null
docker create --name "${STOPPED}" --network none "${BUSYBOX}" sleep 86400 >/dev/null
docker run -d --name sg-twin-image --network none "${SELF_IMAGE_TAG}" sleep 86400 >/dev/null
docker run -d --name sg-twin-label --network none \
  --label org.opencontainers.image.source=https://github.com/mantua1/twinraid \
  "${BUSYBOX}" sleep 86400 >/dev/null
docker run -d --name sg-twin-mount --network none -v "${BASE}/state:/x:ro" \
  "${BUSYBOX}" sleep 86400 >/dev/null
other_id="$(docker inspect --type container --format '{{.Id}}' -- "${OTHER}")"
prefix="${other_id:0:12}"

for marker in sg-twin-image sg-twin-label sg-twin-mount; do
  write_conf "allow ${TEST}
allow ${marker}
"
  out="$(guard check-config 2>&1 || true)"
  expect_contains "check-config refuses TwinRaid (${marker})" "${out}" "allow enthält TwinRaid"
done
write_conf "allow ${TEST}
allow ${STOPPED}
allow sg-missing
allow ${prefix}
allow sg-twin-label
heartbeat_timeout_seconds 30
stop_timeout_seconds 10
dataset twinraidtest
"
guard restart >/dev/null
start_app
expect_wait "running guard: allow with TwinRaid is invalid" error config_invalid 30
expect_equal "error detail" "$(status_field error_detail)" allow_contains_twinraid
send begin
expect_equal "orders refused while config is invalid" "${RESULT}" config_invalid
write_conf "allow ${TEST}
allow ${STOPPED}
allow sg-missing
allow ${prefix}
heartbeat_timeout_seconds 30
stop_timeout_seconds 10
dataset twinraidtest
"
instance="$(status_field instance)"
guard restart >/dev/null
expect_wait "valid config" error null 30
expect_equal "restart keeps the instance" "$(status_field instance)" "${instance}"

echo "== Lauf mit dem Testcontainer"
send begin
expect_equal "begin" "${RESULT}" opened
send "stop ${TEST}" 40
expect_equal "stop test container" "${RESULT}" stopped
expect_equal "test container is stopped" "$(running_state "${TEST}")" false
expect_contains "test container ended cleanly" "$(tail -n 1 "${DATA}/testcontainer/log")" "sauber beendet"
send ping
expect_equal "ping" "${RESULT}" extended
send finish
expect_equal "finish" "${RESULT}" finished
expect_equal "test container runs again" "$(running_state "${TEST}")" true
expect_equal "last run: started" "$(status_field last_run.containers.0.result)" started
# Nach dem Neustart zählt der Testcontainer wieder ab 1.
sleep 1
counter="$(cat "${DATA}/testcontainer/counter")"
sleep 1
if (($(cat "${DATA}/testcontainer/counter") > counter)); then pass "test container counts again"; else
  fail "test container does not count"
fi
expect_contains "log goes on after the clean end" "$(tail -n 1 "${DATA}/testcontainer/log")" zaehler
expect_contains "syslog: stop with name" "$(syslog)" "stop ${TEST}: stopped"

echo "== Ablehnungen"
send begin
send "stop ${OTHER}"
expect_equal "not allowed" "${RESULT}" not_allowed
send "stop ${STOPPED}"
expect_equal "not running" "${RESULT}" not_running
send "stop sg-missing"
expect_equal "not found" "${RESULT}" not_found
send "stop ${prefix}"
expect_equal "id prefix of another container is not found" "${RESULT}" not_found
send "stop ${APP}"
expect_equal "TwinRaid itself: not allowed" "${RESULT}" not_allowed
send finish
for name in "${OTHER}" sg-twin-image sg-twin-label sg-twin-mount "${APP}"; do
  expect_equal "${name} still runs" "$(running_state "${name}")" true
done
send snapshot
expect_equal "snapshot not supported in version 1" "${RESULT}" not_supported

echo "== Angriffe aus dem Container"
for attempt in "rm -f /srv/worker/source-guard/request" \
  "mv /srv/worker/source-guard/request /srv/worker/source-guard/r2" \
  "ln -sf /etc/passwd /srv/worker/source-guard/request" \
  "chown 1000 /srv/worker/source-guard/request" \
  "touch /srv/worker/source-guard/state/x" \
  "rm -f /srv/worker/source-guard/state/status.json"; do
  if docker exec "${APP}" sh -c "${attempt}" >/dev/null 2>&1; then
    fail "container could: ${attempt}"
  else
    pass "container cannot: ${attempt}"
  fi
done
expect_equal "request is still a regular file of root" "$(stat -c '%F %U %h' "${REQUEST}")" \
  "regular file root 1"
seq="$(next_seq)"
head -c 300 /dev/zero | tr '\0' x >"${DATA}/oversized"
order_line "${seq}" begin 0000000000000000 >"${DATA}/checksum"
order_line "${seq}" "stop a;reboot" >"${DATA}/shell"
order_line "${seq}" "stop leakmarker4711" 1111111111111111 >"${DATA}/marker"
expect_rejected "oversized file" "${DATA}/oversized"
expect_rejected "wrong checksum" "${DATA}/checksum"
expect_rejected "shell characters" "${DATA}/shell"
expect_rejected "secret marker" "${DATA}/marker"
order_line 1 begin | from_app
sleep 2
expect_equal "old number is ignored" "$(status_field run)" null
expect_not_contains "marker not in status.json" "$(cat "${STATUS}")" leakmarker4711
expect_not_contains "marker not in syslog" "$(syslog)" leakmarker4711
send begin
expect_equal "valid order after the attacks" "${RESULT}" opened
send finish

echo "== Auftragsdatei auf dem Host verändert"
ln "${REQUEST}" "${BASE}/in/second"
expect_wait "hardlink detected" error_detail request_hardlink
rm -f "${BASE}/in/second"
expect_wait "recovers after hardlink" error null
chown 65534 "${REQUEST}"
expect_wait "foreign owner detected" error_detail request_bad_owner
chown 0 "${REQUEST}"
expect_wait "recovers after owner change" error null

echo "== Totmannschalter (30 s)"
send begin
send "stop ${TEST}" 40
expect_equal "stopped for deadman test" "${RESULT}" stopped
expect_wait "deadman fired" last_run.reason deadman_heartbeat 60
expect_equal "test container runs after deadman" "$(running_state "${TEST}")" true
expect_contains "syslog: deadman" "$(syslog)" "deadman: no heartbeat"

echo "== selftest"
docker stop -t 1 "${APP}" >/dev/null
out="$(guard selftest "${TEST}" 2>&1)" || true
expect_contains "selftest with the test container" "${out}" "OK: angehalten und wieder gestartet."
out="$(guard selftest "${OTHER}" 2>&1)" || true
expect_contains "selftest with a refused container" "${out}" "OK: abgelehnt (not_allowed)"
docker start "${APP}" >/dev/null

echo "== Neustarts"
send begin
send "stop ${TEST}" 40
started="$(status_field started_at)"
seq="$(status_field last_seq)"
sleep 1
pkill -9 -f "${GUARD} run"
for ((i = 0; i < 60; i++)); do
  if (($(status_field started_at) > started)); then break; fi
  sleep 0.5
done
if (($(status_field started_at) > started)); then pass "supervisor restarted the worker"; else
  fail "worker not restarted"
fi
expect_equal "number kept after restart" "$(status_field last_seq)" "${seq}"
expect_equal "open run kept after restart" "$(status_field run.stopped.0.name)" "${TEST}"
guard stop >/dev/null
expect_equal "stop finishes the open run" "$(running_state "${TEST}")" true
expect_equal "reason stop_command" "$(status_field last_run.reason)" stop_command

echo "== Verlorener Zustand"
order_line 50 begin >"${REQUEST}"
rm -rf "${BASE}/private"
guard start >/dev/null
expect_wait "state_lost reported" error state_lost 30
expect_equal "found order is discarded" "$(status_field run)" null
expect_equal "number taken over" "$(status_field last_seq)" 50
expect_contains "syslog: discarded_at_start" "$(syslog)" discarded_at_start
guard restart >/dev/null
expect_wait "restart clears state_lost" error null 30

echo "== Neustart von Klein (leeres /run)"
guard stop >/dev/null
remove_layout
guard start >/dev/null
expect_wait "fresh start" error null 30
expect_equal "new numbers start at 0" "$(status_field last_seq)" 0
start_app
send begin
expect_equal "first order after reboot" "${RESULT}" opened
send finish

echo "== TwinRaid startet vor dem Wächter (Docker legt ein Verzeichnis an)"
guard stop >/dev/null
remove_layout
start_app
if [[ -d "${REQUEST}" ]]; then pass "docker created a directory"; else fail "no directory created"; fi
if docker exec "${APP}" test -d /srv/worker/source-guard/request; then
  pass "TwinRaid sees a directory"
else
  fail "TwinRaid does not see a directory"
fi
guard start >/dev/null
expect_wait "guard reports request_not_a_file" error request_not_a_file 30
expect_equal "detail request_is_directory" "$(status_field error_detail)" request_is_directory
if findmnt -n -M "${BASE}/in" >/dev/null 2>&1; then fail "tmpfs covers the directory"; else
  pass "no tmpfs over the directory"
fi
expect_contains "syslog: hint" "$(syslog)" "restart the guard, then start TwinRaid"
docker rm -f "${APP}" >/dev/null
rmdir "${REQUEST}"
guard restart >/dev/null
expect_wait "fixed by hand" error null 30
start_app
send begin
expect_equal "orders work after the fix" "${RESULT}" opened
send finish

echo "== Unraid Connect"
mkdir -p "$(dirname "${PLUGIN}")" "$(dirname "${FLASH_INI}")"
touch "${PLUGIN}"
printf 'activated="yes"\n' >"${FLASH_INI}"
guard restart >/dev/null
expect_wait "connect marker" host.connect present 30
expect_equal "flash backup active" "$(status_field host.flash_backup)" active

echo "== Aufräumen"
guard stop >/dev/null
expect_equal "no guard process left" "$(pgrep -c -f "${GUARD} (supervise|run)" || true)" 0

if ((failures > 0)); then
  echo "${failures} check(s) failed" >&2
  exit 1
fi
echo "All source guard checks passed."
