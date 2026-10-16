#!/usr/bin/env bash
# SSH- und Aktionstest gegen ein nachgebautes Unraid Groß (Konzept 4.2 und 5.6).
#
# Es wird kein echter Server angesprochen: docker/fake-target ist ein Wegwerf-
# Container mit openssh-server in derselben gepinnten Version wie der Client,
# guard.sh aus dem Repo, einer nachgebauten Unraid-API und Platzhaltern für
# poweroff und virsh. TwinRaid läuft gehärtet wie auf Unraid im Modus real.
#
# Prüft: die authorized_keys-Zeile aus init-ssh-keys (restrict, from=, command=),
# trust-host-key mit Verbindungstest, guard.sh lehnt fremde Befehle ab, kein Terminal,
# keine Weiterleitung, der Key gilt nur von der Adresse von TwinRaid, strikte
# Hostkey-Prüfung; dazu die Abläufe: Rechtetest mit Lese-Key, VM starten, herunterfahren
# und erzwingen, Groß herunterfahren (VM zuerst) und wecken, sowie „Erzwingen und danach
# Server herunterfahren“ nach einem an der VM gescheiterten Herunterfahren.
#
# Aufruf aus dem Repo-Hauptordner:   docker/ssh-test.sh [IMAGE]   (Standard: twinraid:dev)
set -euo pipefail

IMAGE="${1:-twinraid:dev}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="twinraid-sshtest-$$"
FAKE_IMAGE="twinraid-fake-target:test"
NET="${PREFIX}-net"
TARGET="${PREFIX}-target"
APP="${PREFIX}-app"
OUTSIDER="${PREFIX}-outsider"
WORKDIR="$(mktemp -d)"
PY=/app/.venv/bin/python
VM_UUID="3f2a9c4e-5b6d-4e7f-8a9b-0c1d2e3f4a5b"
VM_NAME="Test VM"
MAC="02:42:0A:0B:0C:0D"
API_KEY="SshTestKey_0123456789"
# Muss mit docker/smoke-test.sh und Konzept 5.4 übereinstimmen.
# shellcheck disable=SC2054
HARDENING=(
  --read-only
  --tmpfs /run:rw,exec,nosuid,nodev,mode=0755,size=16m
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m
  --cap-drop=ALL
  --cap-add=SETUID --cap-add=SETGID --cap-add=SETPCAP --cap-add=KILL --cap-add=DAC_READ_SEARCH
  --security-opt=no-new-privileges
)

failures=0
pass() { echo "ok    $*"; }
fail() { echo "FAIL  $*" >&2; failures=$((failures + 1)); }
expect_equal() {
  if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1: expected '$3', got '$2'"; fi
}
expect_contains() {
  if [[ "$2" == *"$3"* ]]; then pass "$1"; else fail "$1: '$3' not found in '${2:0:400}'"; fi
}
expect_not_contains() {
  if [[ "$2" != *"$3"* ]]; then pass "$1"; else fail "$1: '$3' must not appear"; fi
}

cleanup() {
  docker rm -fv "${APP}" "${TARGET}" "${OUTSIDER}" >/dev/null 2>&1 || true
  docker network rm "${NET}" >/dev/null 2>&1 || true
  rm -r "${WORKDIR}"
}
trap cleanup EXIT

ip_of() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$1"; }
fake() { docker exec "${TARGET}" "$@"; }
fake_read() { docker exec "${TARGET}" cat "/state/$1" 2>/dev/null || true; }
fake_write() { docker exec -i "${TARGET}" tee "/state/$1" >/dev/null; }
admin() { docker exec -i "${APP}" twinraid-admin "$@" 2>&1; }

# Befehl an den Worker-Socket, als Web-Benutzer wie der Web-Prozess.
WORKER_SCRIPT='import json,socket,sys
s=socket.socket(socket.AF_UNIX);s.connect("/run/twinraid/worker.sock")
s.sendall((json.dumps({"command":sys.argv[1]})+"\n").encode())
data=b""
while not data.endswith(b"\n"):
    chunk=s.recv(65536)
    if not chunk: break
    data+=chunk
print(data.decode().strip())'
worker() { docker exec -u 10001:10001 "${APP}" "${PY}" -c "${WORKER_SCRIPT}" "$1"; }

wait_for_status() {
  local label="$1" needle="$2" status=""
  for _ in $(seq 1 60); do
    status="$(worker get_status)"
    if [[ "${status}" == *"${needle}"* ]]; then
      pass "${label}"
      return 0
    fi
    sleep 1
  done
  fail "${label}: '${needle}' not in '${status:0:400}'"
}

# Ergebnisse einer Aktion im Aktionslog des Workers (ohne die Startzeilen).
finished() {
  admin show-action-log --lines 500 | grep "  $1  dashboard: " | grep -v ': started$' || true
}

# Fordert eine Aktion an und wartet auf ihr Ergebnis im Aktionslog.
run_action() {
  local action="$1" expected="$2" seconds="${3:-60}" before response result=""
  before="$(finished "${action}" | wc -l)"
  response="$(worker "${action}")"
  if [[ "${response}" != *'"ok":true'* ]]; then
    fail "${action} was not accepted: ${response}"
    return 0
  fi
  for _ in $(seq 1 "${seconds}"); do
    if [[ "$(finished "${action}" | wc -l)" -gt "${before}" ]]; then
      result="$(finished "${action}" | tail -1 | sed 's/.*: //')"
      break
    fi
    sleep 1
  done
  expect_equal "${action} ends with ${expected}" "${result}" "${expected}"
}

ssh_from_app() {
  docker exec "${APP}" ssh -F /dev/null -i /data/secrets/ssh-power -o BatchMode=yes \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/data/secrets/target-ssh-known-hosts \
    -o GlobalKnownHostsFile=/dev/null -o ConnectTimeout=5 "$@"
}

echo "== Image: ${IMAGE}"

# --- 1. Nachgebautes Groß und TwinRaid starten ----------------------------------------
# Basis-Image (per Digest gepinnt) und openssh-Version kommen aus docker/Dockerfile.
openssh_version="$(sed -n 's/^ARG OPENSSH_VERSION=//p' "${REPO_DIR}/docker/Dockerfile")"
base_image="$(sed -n 's/^FROM \(python:[^ ]*@sha256:[0-9a-f]\{64\}\).*/\1/p' "${REPO_DIR}/docker/Dockerfile" | head -1)"
if [[ -z "${openssh_version}" || -z "${base_image}" ]]; then
  echo "Cannot read OPENSSH_VERSION or the pinned python image from docker/Dockerfile." >&2
  exit 1
fi
docker build -q --build-arg "BASE_IMAGE=${base_image}" --build-arg "OPENSSH_VERSION=${openssh_version}" \
  -t "${FAKE_IMAGE}" "${REPO_DIR}/docker/fake-target" >/dev/null
docker network create "${NET}" >/dev/null
docker run -d --name "${TARGET}" --network "${NET}" --tmpfs /state \
  -e FAKE_VM_UUID="${VM_UUID}" -e FAKE_VM_NAME="${VM_NAME}" -e FAKE_MAC="${MAC}" \
  -v "${REPO_DIR}/host-scripts/guard.sh:/boot/config/twinraid/guard.sh:ro" \
  "${FAKE_IMAGE}" >/dev/null
target_ip="$(ip_of "${TARGET}")"
# Der Lese-Key: nur VMS:READ_ANY.
printf '{"%s": ["READ_ANY"]}\n' "${API_KEY}" | fake_write api-keys.json

config="${WORKDIR}/twinraid.yaml"
python3 - "${REPO_DIR}/config.example.yaml" "${config}" "${target_ip}" "${VM_NAME}" "${MAC}" <<'PY'
import sys

source, destination, target_ip, vm_name, mac = sys.argv[1:]
text = open(source, encoding="utf-8").read()
replacements = {
    "host: 192.168.10.11": f"host: {target_ip}",
    'mac: "AA:BB:CC:DD:EE:FF"': f'mac: "{mac}"',
    'unraid_vm_name: "Windows 11 Gaming"': f'unraid_vm_name: "{vm_name}"',
    "shutdown_timeout_minutes: 3": "shutdown_timeout_minutes: 1",
    "  wake: false": "  wake: true",
    "  vm: false": "  vm: true",
    "  shutdown: false": "  shutdown: true",
}
for old, new in replacements.items():
    assert old in text, old
    text = text.replace(old, new)
open(destination, "w", encoding="utf-8").write(text)
PY
chmod 0644 "${config}"
docker run -d --name "${APP}" --network "${NET}" "${HARDENING[@]}" -e TWINRAID_MODE=real \
  -v "${config}:/config/twinraid.yaml:ro" --mount type=volume,dst=/data "${IMAGE}" >/dev/null
for _ in $(seq 1 40); do
  [[ "$(docker logs "${APP}" 2>&1)" == *"worker listening"* ]] && break
  sleep 1
done
app_ip="$(ip_of "${APP}")"
logs="$(docker logs "${APP}" 2>&1)"
expect_contains "worker runs in real mode with actions" "${logs}" \
  "these actions are enabled in twinraid.yaml: wake, vm, shutdown"
expect_contains "Wake-on-LAN broadcast is derived from the own network" "${logs}" \
  "Wake-on-LAN broadcast address:"

# --- 2. Rechtetest mit dem Lese-Key (Konzept 5.6, unraid/api #1723) -------------------------
expect_contains "set-api-key works with the read-only key" \
  "$(admin set-api-key target --stdin <<<"${API_KEY}")" "OK: the API key works. VM 'Test VM' is SHUTOFF."
wait_for_status "VM is reported as off" '"vm":{"state":"offline","reason":null}'
run_action vm_start api_forbidden
expect_equal "the read-only key changed nothing" "$(fake_read mutations.log)" ""
expect_equal "the VM is still off" "$(fake_read vm-state)" "SHUTOFF"

# --- 3. Key erweitern, VM starten -----------------------------------------------------------
printf '{"%s": ["READ_ANY", "UPDATE_ANY"]}\n' "${API_KEY}" | fake_write api-keys.json
run_action vm_start success
expect_equal "the VM runs" "$(fake_read vm-state)" "RUNNING"

# --- 4. SSH einrichten: init-ssh-keys und trust-host-key -------------------------------------
run_action vm_stop ssh_key_missing
init_out="$(admin init-ssh-keys)"
line="$(grep '^restrict,' <<<"${init_out}" || true)"
expect_contains "authorized_keys line has from= with the address of TwinRaid" "${line}" "restrict,from=\"${app_ip}\","
expect_contains "authorized_keys line forces guard.sh" "${line}" 'command="/bin/bash /boot/config/twinraid/guard.sh power" ssh-ed25519 '
expect_not_contains "the private key is never printed" "${init_out}" "PRIVATE KEY"
printf '%s\n' "${line}" | docker exec -i "${TARGET}" tee -a /boot/config/ssh/root/authorized_keys >/dev/null
run_action vm_stop ssh_host_untrusted
host_fp="$(fake ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}')"
expect_contains "trust-host-key refuses a wrong fingerprint" \
  "$(admin trust-host-key target --fingerprint SHA256:falsch || true)" "does not match"
trust_out="$(admin trust-host-key target --fingerprint "${host_fp}" || true)"
expect_contains "trust-host-key shows the fingerprint" "${trust_out}" "ED25519 ${host_fp}"
expect_contains "connection test reaches guard.sh" "${trust_out}" "OK: guard.sh answers (twinraid-guard 1 power ok)"
expect_equal "the connection test changed nothing" "$(fake_read virsh.log)$(fake_read poweroff.log)" ""

# --- 5. VM herunterfahren über guard.sh (nur ACPI) ---------------------------------------------
run_action vm_stop success
expect_equal "guard.sh called virsh shutdown with the UUID" "$(fake_read virsh.log)" "shutdown --domain ${VM_UUID}"
expect_equal "the VM is off" "$(fake_read vm-state)" "SHUTOFF"

# --- 6. Erzwingen erst nach shutdown_timeout_minutes ---------------------------------------------
run_action vm_start success
fake touch /state/vm-hang
run_action vm_stop vm_stop_timeout 100
expect_equal "the VM ignored the shutdown signal" "$(fake_read vm-state)" "RUNNING"
run_action vm_force_stop success
expect_contains "force stop went through the API" "$(fake_read mutations.log)" "forceStop ${VM_UUID}"
expect_equal "the VM is off after forcing" "$(fake_read vm-state)" "SHUTOFF"
fake rm /state/vm-hang

# --- 7. Groß herunterfahren: VM zuerst, dann poweroff --------------------------------------------
run_action vm_start success
run_action shutdown_target success
expect_contains "the VM was shut down first" "$(admin show-action-log)" \
  "vm_stop  automatic (part of shutdown_target): success"
expect_equal "guard.sh ran poweroff" "$(fake_read poweroff.log)" "poweroff"
wait_for_status "target is reported as offline" '"target":{"state":"offline","reason":"unreachable"}'

# --- 8. Groß wecken: Magic Packet an die abgeleitete Broadcast-Adresse ------------------------------
run_action wake_target success
expect_contains "the magic packet reached the target with its MAC" "$(fake_read wol.log)" "${MAC,,}"
wait_for_status "target is online again" '"target":{"state":"online","reason":null}'

# --- 8a. Erzwingen und danach Server herunterfahren (Konzept 4.2, Ergänzung zu E10) ---------------
expect_contains "force-and-shutdown is not offered without an aborted shutdown" \
  "$(worker shutdown_target_force_vm)" '"blocked_by":"not_offered"'
run_action vm_start success
fake touch /state/vm-hang
run_action shutdown_target vm_stop_timeout 100
expect_equal "the aborted shutdown left the target on" "$(fake_read poweroff.log)" "poweroff"
expect_contains "status offers force-and-shutdown" "$(worker get_status)" '"forced_shutdown_until":"'
run_action shutdown_target_force_vm success
fake rm /state/vm-hang
force_log="$(admin show-action-log --lines 20)"
expect_contains "the VM was forced as part of it" "${force_log}" \
  "vm_force_stop  automatic (part of shutdown_target_force_vm): success"
expect_contains "then the target was shut down" "${force_log}" \
  "shutdown_target  automatic (part of shutdown_target_force_vm): success"
expect_equal "force went through the API" "$(fake_read mutations.log | grep -c forceStop)" "2"
expect_equal "guard.sh ran poweroff once more" "$(fake_read poweroff.log | wc -l | tr -d ' ')" "2"
wait_for_status "target is reported as offline after force-and-shutdown" \
  '"target":{"state":"offline","reason":"unreachable"}'
run_action wake_target success

# --- 9. Einschränkungen der authorized_keys-Zeile ------------------------------------------------
denied="$(ssh_from_app "root@${target_ip}" id 2>&1 || true)"
expect_contains "guard.sh rejects other commands" "${denied}" "twinraid-guard: denied"
expect_not_contains "no shell command ran" "${denied}" "uid=0"
pty="$(ssh_from_app -tt "root@${target_ip}" check 2>&1 || true)"
expect_contains "restrict: no terminal" "${pty}" "PTY allocation request failed"
forward="$(ssh_from_app -W 127.0.0.1:80 "root@${target_ip}" </dev/null 2>&1 || true)"
expect_contains "restrict: no forwarding" "${forward}" "administratively prohibited"
docker exec "${APP}" cat /data/secrets/ssh-power >"${WORKDIR}/stolen-key"
docker run -d --name "${OUTSIDER}" --network "${NET}" --entrypoint sleep "${IMAGE}" 120 >/dev/null
docker cp "${WORKDIR}/stolen-key" "${OUTSIDER}:/tmp/key"
docker exec "${OUTSIDER}" chmod 600 /tmp/key
outsider="$(docker exec "${OUTSIDER}" ssh -F /dev/null -i /tmp/key -o BatchMode=yes \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "root@${target_ip}" check 2>&1 || true)"
expect_contains "from=: the key does not work from another address" "${outsider}" "Permission denied"

# --- 10. Strikte Hostkey-Prüfung ------------------------------------------------------------------
fake sh -c 'rm -f /etc/ssh/ssh_host_* && ssh-keygen -A >/dev/null && kill -HUP 1'
sleep 2
run_action shutdown_target ssh_host_key_mismatch
expect_equal "no poweroff with a changed host key" "$(fake_read poweroff.log | wc -l | tr -d ' ')" "2"
expect_not_contains "the API key never appears in the log" "$(docker logs "${APP}" 2>&1)" "${API_KEY}"

echo
if [[ "${failures}" -gt 0 ]]; then
  echo "${failures} check(s) failed." >&2
  docker logs "${APP}" 2>&1 | tail -30 >&2
  exit 1
fi
echo "All checks passed."
