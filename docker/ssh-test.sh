#!/usr/bin/env bash
# SSH- und Aktionstest gegen ein nachgebautes Unraid Groß (Konzept 3.5, 4.2 und 5.6).
#
# Es wird kein echter Server angesprochen: docker/fake-target ist ein Wegwerf-
# Container mit openssh-server in derselben gepinnten Version wie der Client,
# guard.sh aus dem Repo, einem nachgebauten virsh und einem Platzhalter für
# poweroff. TwinRaid läuft gehärtet wie auf Unraid im Modus real.
#
# Prüft: die authorized_keys-Zeilen der Keys „status“ und „power“ aus init-ssh-keys
# (restrict, from=, command=), trust-host-key mit Verbindungstest beider Keys, dass ein
# bestandener Test mit dem Key „status“ die Pause nach einem abgelehnten Key beendet, die
# Sitzung vm-watch (alle Zustände, weitere VMs, VM-Dienst aus oder hängend,
# power.conf fehlt, Lebenszeichen, höchstens zwei Schleifen, Neuverbindung, keine
# übrig bleibenden Prozesse), die Merkmale von Unraid Connect, dass Modus status nichts
# verändert und keine Anfrage Parameter annimmt, die Prüfung vor poweroff in guard.sh,
# dass Abläufe sich nur für den Befehl selbst anmelden, und alle Abläufe aus Phase 4:
# VM starten, herunterfahren und erzwingen, Groß herunterfahren (VM zuerst) und wecken,
# „Erzwingen und danach Server herunterfahren“ und der Schutz vor weiteren VMs.
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
OTHER_UUID="7c6b5a49-3827-4161-9504-f3e2d1c0b9a8"
VM_NAME="Test VM"
MAC="02:42:0A:0B:0C:0D"
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
fake_read() { docker exec "${TARGET}" cat "$1" 2>/dev/null || true; }
state() { fake_read "/state/$1"; }
# Schreibt stdin in eine Datei auf dem nachgebauten Groß.
fake_write() { docker exec -i "${TARGET}" tee "$1" >/dev/null; }
admin() { docker exec -i "${APP}" twinraid-admin "$@" 2>&1; }

# Zustand einer VM im nachgebauten virsh, z. B. set_vm <UUID> running.
VM_SCRIPT='import json,sys
path="/state/vms.json"
vms=json.load(open(path))
for vm in vms:
    if vm["uuid"]==sys.argv[1]:
        vm["state"]=sys.argv[2]
        if len(sys.argv)>3: vm["name"]=sys.argv[3]
json.dump(vms,open(path,"w"))'
set_vm() { fake python3 -c "${VM_SCRIPT}" "$@"; }
set_flag() { fake touch "/state/$1"; }
clear_flag() { fake rm -f "/state/$1"; }
virsh_actions() { state virsh.log | grep -E '^(start|shutdown|destroy) ' || true; }

# Prozesse von guard.sh im Modus status (vm-watch) auf dem nachgebauten Groß.
PROC_SCRIPT='import os,signal,sys
found=[]
for pid in os.listdir("/proc"):
    if not pid.isdigit(): continue
    try: cmd=open(f"/proc/{pid}/cmdline","rb").read()
    except OSError: continue
    if b"guard.sh\x00status" in cmd: found.append(int(pid))
if sys.argv[1]=="kill":
    for pid in found: os.kill(pid,signal.SIGTERM)
print(len(found))'
watch_loops() { fake python3 -c "${PROC_SCRIPT}" count; }
# Beendet die Schleife auf Groß, als bräche die Verbindung ab.
kill_watch() { fake python3 -c "${PROC_SCRIPT}" kill >/dev/null; }
wait_for_loops() {
  local label="$1" expected="$2" count=""
  for _ in $(seq 1 40); do
    count="$(watch_loops)"
    if [[ "${count}" == "${expected}" ]]; then
      pass "${label}"
      return 0
    fi
    sleep 1
  done
  fail "${label}: ${count} vm-watch loops instead of ${expected}"
}
# Meldungen von guard.sh im Syslog des nachgebauten Groß.
syslog_count() {
  local log
  log="$(state syslog.log)"
  grep -F "twinraid-guard" <<<"${log}" | grep -c -F "$1" || true
}
# Anmeldungen mit einem Key (Fingerabdruck im Log von sshd).
logins() {
  local log
  log="$(docker logs "${TARGET}" 2>&1)"
  grep "Accepted publickey for root" <<<"${log}" | grep -c -F "$1" || true
}

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
  local label="$1" needle="$2" seconds="${3:-60}" status=""
  for _ in $(seq 1 "${seconds}"); do
    status="$(worker get_status)"
    if [[ "${status}" == *"${needle}"* ]]; then
      pass "${label}"
      return 0
    fi
    sleep 1
  done
  fail "${label}: '${needle}' not in '${status:0:600}'"
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

# SSH vom TwinRaid-Container mit einem der Keys, z. B. ssh_with status root@x check.
ssh_with() {
  local key="$1"
  shift
  docker exec "${APP}" ssh -F /dev/null -i "/data/secrets/ssh-${key}" -o BatchMode=yes \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/data/secrets/target-ssh-known-hosts \
    -o GlobalKnownHostsFile=/dev/null -o ConnectTimeout=5 "$@"
}
# vm-watch von Hand mit dem Key „status“; timeout beendet ssh im Container.
watch_by_hand() {
  local seconds="$1"
  docker exec "${APP}" timeout "${seconds}" ssh -F /dev/null -i /data/secrets/ssh-status \
    -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=/data/secrets/target-ssh-known-hosts -o GlobalKnownHostsFile=/dev/null \
    -o ConnectTimeout=5 "root@${target_ip}" vm-watch
}
ON='"vm":{"state":"online","reason":null}'
OFF='"vm":{"state":"offline","reason":null}'

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
expect_not_contains "no warning about an unencrypted API" "${logs}" "unencrypted"
wait_for_status "without keys the VM state names the missing key" \
  '"vm":{"state":"unknown","reason":"ssh_key_missing"}'
expect_not_contains "TwinRaid stores no API key" "$(docker exec "${APP}" ls /data/secrets)" "api"

# --- 2. Keys „status“ und „power“, Hostkey (Konzept 5.5 und 5.6) -----------------------
init_out="$(admin init-ssh-keys)"
status_line="$(grep '^restrict,.*guard.sh status' <<<"${init_out}" || true)"
power_line="$(grep '^restrict,.*guard.sh power' <<<"${init_out}" || true)"
expect_contains "status line has from= with the address of TwinRaid" "${status_line}" "restrict,from=\"${app_ip}\","
expect_contains "status line forces guard.sh status" "${status_line}" 'command="/bin/bash /boot/config/twinraid/guard.sh status" ssh-ed25519 '
expect_contains "status line names the key" "${status_line}" " twinraid-status"
expect_contains "power line forces guard.sh power" "${power_line}" 'command="/bin/bash /boot/config/twinraid/guard.sh power" ssh-ed25519 '
expect_not_contains "the private keys are never printed" "${init_out}" "PRIVATE KEY"
wait_for_status "without a confirmed host key nothing is sent" \
  '"vm":{"state":"unknown","reason":"ssh_host_untrusted"}'
host_fp="$(fake ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}')"
expect_contains "trust-host-key refuses a wrong fingerprint" \
  "$(admin trust-host-key target --fingerprint SHA256:falsch || true)" "does not match"
# Der Fall aus dem Test von v0.4.3: Hostkey bestätigt, die Zeilen fehlen noch auf Groß.
early_out="$(admin trust-host-key target --fingerprint "${host_fp}" || true)"
expect_contains "trust-host-key shows the fingerprint" "${early_out}" "ED25519 ${host_fp}"
expect_contains "without the lines on Groß the key status is rejected" "${early_out}" \
  "The server rejects the key 'status'"
expect_not_contains "a failed test does not end the wait" "${early_out}" "right away"
wait_for_status "the status connection pauses after the rejected key" \
  '"vm":{"state":"unknown","reason":"ssh_auth_failed"}'
expect_contains "the status names the next attempt of the pause" "$(worker get_status)" \
  '"paused":true}'
printf '%s\n%s\n' "${status_line}" "${power_line}" | docker exec -i "${TARGET}" tee -a /boot/config/ssh/root/authorized_keys >/dev/null
trust_out="$(admin trust-host-key target --fingerprint "${host_fp}" || true)"
expect_contains "the host key stays confirmed" "${trust_out}" "This host key was already confirmed."
expect_contains "connection test with the key status" "${trust_out}" "OK: guard.sh answers (twinraid-guard 2 status ok)"
expect_contains "connection test with the key power" "${trust_out}" "OK: guard.sh answers (twinraid-guard 2 power ok)"
expect_contains "trust-host-key ends the wait (v0.4.4)" "${trust_out}" \
  "TwinRaid connects with the key 'status' right away."
# Ohne das Signal bliebe die Pause 30 Minuten; ohne power.conf meldet guard.sh nun den Stand.
wait_for_status "the status connection is up within seconds" \
  '"vm":{"state":"unknown","reason":"vm_not_configured"}' 15
expect_contains "the worker got the signal" "$(docker logs "${APP}" 2>&1)" \
  "Retry of the status connection requested"
expect_contains "with a running session there is no next attempt" "$(worker get_status)" \
  '"watch_retry":null'
status_fp="$(docker exec "${APP}" ssh-keygen -lf /data/secrets/ssh-status.pub | awk '{print $2}')"
power_fp="$(docker exec "${APP}" ssh-keygen -lf /data/secrets/ssh-power.pub | awk '{print $2}')"

# --- 3. power.conf: ohne Eintrag keine VM ----------------------------------------------------
wait_for_status "without power.conf the VM is not set up" \
  '"vm":{"state":"unknown","reason":"vm_not_configured"}'
expect_equal "guard.sh reports the missing power.conf once" \
  "$(syslog_count "power.conf is missing or invalid")" "1"
refused="$(ssh_with power "root@${target_ip}" vm-start 2>&1 || true)"
expect_contains "VM commands are refused without power.conf" "${refused}" "twinraid-guard: refused vm-not-configured"
printf 'vm %s\n' "${VM_UUID}" | fake_write /boot/config/twinraid/power.conf
wait_for_status "with power.conf the VM is reported as off" "${OFF}"
expect_equal "guard.sh noted one vm-watch session" "$(syslog_count "vm-watch started")" "1"
expect_equal "nothing was changed so far" "$(virsh_actions)$(state poweroff.log)" ""

# --- 4. Modus status verändert nichts, keine Anfrage nimmt Parameter ---------------------------
for request in vm-start vm-shutdown vm-force-stop poweroff "vm-shutdown ${VM_UUID}" check2; do
  denied="$(ssh_with status "root@${target_ip}" "${request}" 2>&1 || true)"
  expect_contains "key status cannot run '${request}'" "${denied}" "twinraid-guard: denied"
done
for request in "vm-start ${VM_UUID}" "vm-shutdown ${VM_UUID}" "vm-force-stop ${VM_UUID}" vm-watch; do
  denied="$(ssh_with power "root@${target_ip}" "${request}" 2>&1 || true)"
  expect_contains "key power rejects '${request}'" "${denied}" "twinraid-guard: denied"
done
expect_equal "the rejected requests changed nothing" "$(virsh_actions)$(state poweroff.log)" ""

# --- 5. vm-watch: alle Zustände, weitere VMs, VM-Dienst (Konzept 3.5 und 4.1) ---------------
declare -A EXPECTED=(
  ["running"]="${ON}"
  ["idle"]="${ON}"
  ["paused"]='"vm":{"state":"online","reason":"vm_paused"}'
  ["pmsuspended"]='"vm":{"state":"online","reason":"vm_suspended"}'
  ["in shutdown"]='"vm":{"state":"stopping","reason":null}'
  ["crashed"]='"vm":{"state":"offline","reason":"vm_crashed"}'
  ["no state"]='"vm":{"state":"unknown","reason":null}'
  ["shut off"]="${OFF}"
)
for vm_state in running idle paused pmsuspended "in shutdown" crashed "no state" "shut off"; do
  set_vm "${VM_UUID}" "${vm_state}"
  wait_for_status "vm-watch reports '${vm_state}'" "${EXPECTED[${vm_state}]}" 20
done
set_vm "${OTHER_UUID}" paused $'Tab\tName'
wait_for_status "other VMs come cleaned from vm-watch" '"other_vms_running":["Tab?Name"]' 20
set_vm "${OTHER_UUID}" "shut off" "Second VM"
wait_for_status "an other VM that is off is not listed" '"other_vms_running":[]' 20
set_flag libvirt-down
wait_for_status "a stopped VM service is reported" \
  '"vm":{"state":"unknown","reason":"vm_service_unavailable"}' 20
expect_contains "and the list of other VMs is unknown" "$(worker get_status)" '"other_vms_running":null'
clear_flag libvirt-down
wait_for_status "the VM service is back" "${OFF}" 20
set_flag virsh-hang
wait_for_status "a hanging virsh ends after its time limit" \
  '"vm":{"state":"unknown","reason":"vm_service_unavailable"}' 40
clear_flag virsh-hang
wait_for_status "the same loop goes on after virsh hung" "${OFF}" 40
printf 'vm %s\n' "00000000-0000-4000-8000-000000000000" | fake_write /boot/config/twinraid/power.conf
wait_for_status "an unknown UUID in power.conf is reported" \
  '"vm":{"state":"unknown","reason":"vm_not_found"}' 30
printf 'vm %s\n' "${VM_UUID}" | fake_write /boot/config/twinraid/power.conf
wait_for_status "the VM from power.conf is back" "${OFF}" 30
expect_equal "all of this ran in one session" "$(syslog_count "vm-watch started")" "1"
expect_not_contains "VM names never appear in the container log" "$(docker logs "${APP}" 2>&1)" "Tab"

# --- 6. Lebenszeichen, höchstens zwei Schleifen, keine übrigen Prozesse ----------------------
# TwinRaid hält eine Schleife, diese Sitzung von Hand die zweite (20 s lang).
watch_by_hand 20 >"${WORKDIR}/watch.out" 2>&1 &
by_hand=$!
sleep 3
busy="$(watch_by_hand 10 2>&1 || true)"
expect_contains "a third vm-watch loop is refused" "${busy}" "twinraid-guard: refused watch-busy"
wait "${by_hand}" || true
watch_out="$(cat "${WORKDIR}/watch.out")"
vm_hex="$(printf '%s' "${VM_NAME}" | od -An -v -tx1 | tr -d ' \n')"
expect_contains "vm-watch starts with a full line" "${watch_out}" \
  "watch 2 vm=SHUTOFF name=${vm_hex} others=0 names=- connect=absent flash_backup=inactive"
expect_equal "without changes a keepalive follows after 15 s" \
  "$(grep -c '^watch 2 ' <<<"${watch_out}" || true)" "2"
wait_for_loops "the loop ends when its connection closes" 1

# --- 7. Neuverbindung und Merkmale von Unraid Connect ---------------------------------------
fake touch /boot/config/plugins/dynamix.unraid.net.plg
printf 'activated="true"\n' | fake_write /var/local/emhttp/flashbackup.ini
before="$(logins "${status_fp}")"
kill_watch
wait_for_status "an interrupted session is rebuilt and reports Unraid Connect" \
  '"notices":["target_connect_plugin","target_flash_backup"]' 40
expect_equal "the new session is exactly one new login" "$(logins "${status_fp}")" "$((before + 1))"
fake rm /boot/config/plugins/dynamix.unraid.net.plg /var/local/emhttp/flashbackup.ini
before="$(logins "${status_fp}")"
kill_watch
# Erst wenn die neue Sitzung die VM wieder meldet, ist sie aufgebaut.
wait_for_status "the session is rebuilt once more" '"vm":{"state":"unknown","reason":"watch_interrupted"}' 10
wait_for_status "and reports the VM again" "${OFF}" 40
expect_contains "without the markers there is no notice" "$(worker get_status)" '"notices":[]'
expect_equal "again exactly one new login" "$(logins "${status_fp}")" "$((before + 1))"

# --- 8. Abläufe aus Phase 4; Anmeldung nur für den Befehl selbst --------------------------------
status_logins="$(logins "${status_fp}")"
power_logins="$(logins "${power_fp}")"
run_action vm_start success
expect_contains "guard.sh started the VM from power.conf" "$(virsh_actions)" "start --domain ${VM_UUID}"
wait_for_status "the VM runs" "${ON}" 20
run_action vm_stop success
expect_contains "guard.sh sent only the ACPI signal" "$(virsh_actions)" "shutdown --domain ${VM_UUID}"
expect_equal "the sequences used no further status login" "$(logins "${status_fp}")" "${status_logins}"
expect_equal "one login with the key power per command" "$(logins "${power_fp}")" "$((power_logins + 2))"

# Erzwingen erst nach shutdown_timeout_minutes.
run_action vm_start success
set_flag vm-hang
run_action vm_stop vm_stop_timeout 100
expect_contains "the VM ignored the shutdown signal" "$(worker get_status)" "${ON}"
run_action vm_force_stop success
expect_contains "force stop went through guard.sh" "$(virsh_actions)" "destroy --domain ${VM_UUID}"
wait_for_status "the VM is off after forcing" "${OFF}" 20
clear_flag vm-hang

# Groß herunterfahren: VM zuerst, dann poweroff.
run_action vm_start success
run_action shutdown_target success
expect_contains "the VM was shut down first" "$(admin show-action-log)" \
  "vm_stop  automatic (part of shutdown_target): success"
expect_equal "guard.sh ran poweroff" "$(state poweroff.log)" "poweroff"
wait_for_status "target is reported as offline" '"target":{"state":"offline","reason":"unreachable"}'

# Groß wecken: Magic Packet an die abgeleitete Broadcast-Adresse, danach neue Sitzung.
run_action wake_target success
expect_contains "the magic packet reached the target with its MAC" "$(state wol.log)" "${MAC,,}"
wait_for_status "target is online again" '"target":{"state":"online","reason":null}'
wait_for_status "the VM is known again after the boot" "${OFF}"

# Erzwingen und danach Server herunterfahren (Konzept 4.2, Ergänzung zu E10).
expect_contains "force-and-shutdown is not offered without an aborted shutdown" \
  "$(worker shutdown_target_force_vm)" '"blocked_by":"not_offered"'
run_action vm_start success
set_flag vm-hang
run_action shutdown_target vm_stop_timeout 100
expect_equal "the aborted shutdown left the target on" "$(state poweroff.log)" "poweroff"
expect_contains "status offers force-and-shutdown" "$(worker get_status)" '"forced_shutdown_until":"'
run_action shutdown_target_force_vm success
clear_flag vm-hang
force_log="$(admin show-action-log --lines 20)"
expect_contains "the VM was forced as part of it" "${force_log}" \
  "vm_force_stop  automatic (part of shutdown_target_force_vm): success"
expect_contains "then the target was shut down" "${force_log}" \
  "shutdown_target  automatic (part of shutdown_target_force_vm): success"
expect_equal "both forced stops went through guard.sh" "$(virsh_actions | grep -c '^destroy ')" "2"
expect_equal "guard.sh ran poweroff once more" "$(state poweroff.log | wc -l | tr -d ' ')" "2"
wait_for_status "target is reported as offline after force-and-shutdown" \
  '"target":{"state":"offline","reason":"unreachable"}'
run_action wake_target success
wait_for_status "the VM is known again" "${OFF}"

# Weitere VMs auf Groß: kein Ausschalten (Konzept 4.2, ab v0.4.2).
set_vm "${OTHER_UUID}" running
wait_for_status "status lists the other running VM" '"other_vms_running":["Second VM"]' 20
expect_contains "shutdown is locked while another VM runs" \
  "$(worker shutdown_target)" '"blocked_by":"other_vms_running"'
set_vm "${OTHER_UUID}" "shut off"
wait_for_status "the other VM is off again" '"other_vms_running":[]' 20
# Die zweite VM startet, während die Gaming-VM herunterfährt: Prüfung vor poweroff.
run_action vm_start success
set_flag other-vm-on-shutdown
run_action shutdown_target other_vms_running
expect_equal "Groß was not switched off" "$(state poweroff.log | wc -l | tr -d ' ')" "2"
expect_contains "the gaming VM was shut down first" "$(worker get_status)" "${OFF}"
clear_flag other-vm-on-shutdown
set_vm "${OTHER_UUID}" "shut off"
wait_for_status "no other VM runs any more" '"other_vms_running":[]' 20
expect_not_contains "VM names never appear in the container log" "$(docker logs "${APP}" 2>&1)" "Second VM"

# --- 9. guard.sh prüft vor poweroff selbst (Konzept 3.5) --------------------------------------
set_vm "${VM_UUID}" running
refused="$(ssh_with power "root@${target_ip}" poweroff 2>&1 || true)"
expect_contains "poweroff is refused while the gaming VM runs" "${refused}" "twinraid-guard: refused vm-running"
set_vm "${VM_UUID}" "shut off"
set_vm "${OTHER_UUID}" crashed
refused="$(ssh_with power "root@${target_ip}" poweroff 2>&1 || true)"
expect_contains "poweroff is refused while another VM is not off" "${refused}" "twinraid-guard: refused other-vms-running"
set_vm "${OTHER_UUID}" "shut off"
set_flag libvirt-down
refused="$(ssh_with power "root@${target_ip}" poweroff 2>&1 || true)"
expect_contains "poweroff is refused when the VM list is unreadable" "${refused}" "twinraid-guard: refused vm-state-unknown"
clear_flag libvirt-down
expect_equal "guard.sh never switched off with running VMs" "$(state poweroff.log | wc -l | tr -d ' ')" "2"
expect_equal "the refusals are in the syslog" "$(syslog_count "refused: vm-running")" "1"
wait_for_status "the VM is reported as off" "${OFF}" 20

# --- 10. Einschränkungen der authorized_keys-Zeilen -------------------------------------------
for key in status power; do
  denied="$(ssh_with "${key}" "root@${target_ip}" id 2>&1 || true)"
  expect_contains "key ${key}: guard.sh rejects other commands" "${denied}" "twinraid-guard: denied"
  expect_not_contains "key ${key}: no shell command ran" "${denied}" "uid=0"
  pty="$(ssh_with "${key}" -tt "root@${target_ip}" check 2>&1 || true)"
  expect_contains "key ${key}: restrict, no terminal" "${pty}" "PTY allocation request failed"
  forward="$(ssh_with "${key}" -W 127.0.0.1:80 "root@${target_ip}" </dev/null 2>&1 || true)"
  expect_contains "key ${key}: restrict, no forwarding" "${forward}" "administratively prohibited"
done
docker run -d --name "${OUTSIDER}" --network "${NET}" --entrypoint sleep "${IMAGE}" 120 >/dev/null
for key in status power; do
  docker exec "${APP}" cat "/data/secrets/ssh-${key}" >"${WORKDIR}/stolen-key"
  docker cp "${WORKDIR}/stolen-key" "${OUTSIDER}:/tmp/key"
  docker exec "${OUTSIDER}" chmod 600 /tmp/key
  outsider="$(docker exec "${OUTSIDER}" ssh -F /dev/null -i /tmp/key -o BatchMode=yes \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "root@${target_ip}" check 2>&1 || true)"
  expect_contains "from=: the key ${key} does not work from another address" "${outsider}" "Permission denied"
done

# --- 11. Strikte Hostkey-Prüfung ------------------------------------------------------------------
fake sh -c 'rm -f /etc/ssh/ssh_host_* && ssh-keygen -A >/dev/null && kill -HUP 1'
sleep 2
run_action shutdown_target ssh_host_key_mismatch
expect_equal "no poweroff with a changed host key" "$(state poweroff.log | wc -l | tr -d ' ')" "2"
kill_watch
wait_for_status "the status session refuses the changed host key" \
  '"vm":{"state":"unknown","reason":"ssh_host_key_mismatch"}' 40

# --- 12. Nach dem Ende von TwinRaid bleibt auf Groß nichts übrig --------------------------------
docker stop "${APP}" >/dev/null
wait_for_loops "no vm-watch loop is left on the target" 0

echo
if [[ "${failures}" -gt 0 ]]; then
  echo "${failures} check(s) failed." >&2
  docker logs "${APP}" 2>&1 | tail -30 >&2
  exit 1
fi
echo "All checks passed."
