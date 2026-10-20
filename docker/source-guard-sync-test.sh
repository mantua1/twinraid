#!/usr/bin/env bash
# Test von host-scripts/source-guard.sh Version 2 mit echtem ZFS in Dateien und dem echten
# guard.sh 3 als Gegenseite (Konzept 3.6 „Job, Prüfen und Senden“, 3.7).
#
# Läuft als root auf einem Wegwerf-Rechner (CI-Runner) mit installiertem zfsutils-linux
# und nur mit SOURCE_GUARD_SYNC_TEST_HOST=disposable. Ein Pool "mainpool" spielt Groß
# (mainpool/twinraid-mirror mit der Delegation für UID 64000 aus der Anleitung), ein Pool
# "twinraid-klein" spielt Klein. Statt ssh startet eine kleine Hilfe guard.sh im Modus
# sync mit SSH_ORIGINAL_COMMAND, wie sshd mit der Zeile aus init-key; vorher prüft sie die
# feste Argumentliste und die Rechte des kopierten Keys.
#
# Der Wächter läuft mit Testpfaden (Ablage, Konfiguration, Key, Syslog), dem Pool
# twinraid-klein statt mainpool, ohne Mindestabstand zwischen Snapshots und mit einem Tag
# von einer Sekunde für die Aufbewahrung.
# guard.sh läuft mit denselben Testpfaden wie in docker/guard-sync-test.sh.
#
# Prüft: snapshot (atomar, nicht rekursiv, Eigenschaft), prepare beim ersten Mal (voll)
# und danach (inkrementell, Zählungen aus zfs diff), send mit neuer Basis (Hold,
# Lesezeichen), Freigabe von Löschungen, Senden vom Lesezeichen, fremder Snapshot auf
# Groß (base_missing), Ablehnungen von guard.sh (too-soon, receive-busy), close und
# Neustart während einer Übertragung, Aufbewahrung auf Klein.
#
# Aufruf aus dem Repo-Hauptordner:
#   sudo SOURCE_GUARD_SYNC_TEST_HOST=disposable docker/source-guard-sync-test.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
WORK=/var/tmp/source-guard-sync-test
GUARD="${WORK}/guard.sh"
SOURCE_GUARD="${WORK}/source-guard.sh"
KLEIN=twinraid-klein
ROOT=mainpool/twinraid-mirror
BASE="${WORK}/run/twinraid-source-guard"
REQUEST="${BASE}/in/request"
STATUS="${BASE}/state/status.json"
PERMS=acltype,atime,canmount,checksum,compression,copies,create,dedup,devices,exec,mount,mountpoint,primarycache,quota,readonly,receive:append,recordsize,refquota,refreservation,reservation,secondarycache,setuid,sharenfs,sharesmb,snapdir,sync,userprop,xattr

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
  echo "source-guard-sync-test: $*" >&2
  exit 2
}

if ((EUID != 0)); then
  die "muss als root laufen"
fi
if [[ "${SOURCE_GUARD_SYNC_TEST_HOST:-}" != disposable ]]; then
  die "legt Pools an; nur mit SOURCE_GUARD_SYNC_TEST_HOST=disposable"
fi
for pool in mainpool "${KLEIN}"; do
  if zpool list -H -o name "${pool}" >/dev/null 2>&1; then
    die "es gibt schon einen Pool ${pool}"
  fi
done

cleanup() {
  local rc=$?
  set +e
  if [[ -f "${SOURCE_GUARD}" ]]; then
    /bin/bash "${SOURCE_GUARD}" stop >/dev/null 2>&1
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
source_of() { zfs get -H -o source -- "$1" "$2"; }

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
  local key="$1" expected="$2" limit="${3:-30}" i value=""
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
  local seq="$1" text="$2" payload sum line
  payload="TRSG1 ${seq} ${text}"
  sum="$(printf '%s' "${payload}" | sha256sum)"
  printf -v line '%-255s\n' "${payload} ${sum:0:16}"
  printf '%s' "${line}"
}

# order <Auftrag> <erwartetes Ergebnis>: schreibt an Ort und Stelle wie TwinRaid.
order() {
  local seq
  seq=$(($(status_field last_seq) + 1))
  order_line "${seq}" "$1" >"${REQUEST}"
  wait_status last.seq "${seq}" 20 || true
  expect_equal "order $1" "$(status_field last.result)" "$2"
}

# Wartet, bis der Job einen dieser Zustände hat (durch | getrennt).
wait_job() {
  local wanted="$1" limit="${2:-60}" i state=""
  for ((i = 0; i < limit * 4; i++)); do
    state="$(status_field job.state)"
    if [[ "|${wanted}|" == *"|${state}|"* ]]; then
      return 0
    fi
    sleep 0.25
  done
  fail "job state: expected ${wanted}, got ${state}"
}

job() { status_field "job.$1"; }
dataset_field() { status_field "job.datasets.$1.$2"; }
own_snapshots() {
  zfs list -H -t snapshot -o name -s createtxg -- "$1" |
    while read -r name; do
      if [[ "$(value twinraid:created-by "${name}"):$(source_of twinraid:created-by "${name}")" == source-guard:local ]]; then
        echo "${name}"
      fi
    done
}
held() { zfs holds -H -- "$1" | awk -F'\t' '{print $2}' | tr '\n' ' '; }
# Der Mindestabstand von 10 min in guard.sh (too-soon): twinraid:received am neuesten
# Snapshot eines Spiegels als root zurückstellen, wie es nach 11 Minuten wäre.
age_mirrors() {
  local mirror newest
  for mirror in twinraidtest dokumente; do
    newest="$(zfs list -H -t snapshot -o name -s createtxg -- "${ROOT}/${mirror}" 2>/dev/null | tail -1)"
    if [[ -n "${newest}" ]]; then
      zfs set -- "twinraid:received=$(($(date +%s) - 660))" "${newest}"
    fi
  done
}
# Ein ganzer Lauf ohne Container: snapshot, prepare, send.
full_run() {
  age_mirrors
  # Snapshot-Namen haben Sekunden; zwei Läufe in derselben Sekunde gäbe es auf Klein nie.
  sleep 1
  order snapshot created
  order prepare preparing
  wait_job "prepared|awaiting_confirm|failed"
}

echo "== Vorbereitung"
mkdir -p "${WORK}/run" "${WORK}/var-run"
truncate -s 2G "${WORK}/mainpool.img" "${WORK}/klein.img"
zpool create -o cachefile=none -o ashift=12 -m /mnt/mainpool \
  -O compression=on -O atime=off -O xattr=sa -O acltype=posix mainpool "${WORK}/mainpool.img"
zfs create "${ROOT}"
zfs allow -u 64000 "${PERMS}" "${ROOT}"
printf 'mirror twinraidtest\nmirror dokumente\n' >"${WORK}/mirror.conf"
zpool create -o cachefile=none -o ashift=12 -m "/mnt/${KLEIN}" -O compression=on \
  "${KLEIN}" "${WORK}/klein.img"
zfs create "${KLEIN}/twinraidtest"
zfs create "${KLEIN}/twinraidtest/kind"
zfs create "${KLEIN}/dokumente"
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  printf 'Datei %s\n' "${i}" >"/mnt/${KLEIN}/twinraidtest/datei${i}.txt"
done
printf 'nur im Kind\n' >"/mnt/${KLEIN}/twinraidtest/kind/kind.txt"
head -c 2000000 /dev/urandom >"/mnt/${KLEIN}/dokumente/zufall.bin"
# Ein fremder Snapshot (ohne twinraid:created-by): der Wächter fasst ihn nie an.
zfs snapshot "${KLEIN}/twinraidtest@manuell"

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
sed -e "s|^readonly CONF=.*|readonly CONF=${WORK}/source-guard.conf|" \
  -e "s|^readonly BASE_DIR=.*|readonly BASE_DIR=${BASE}|" \
  -e "s|^readonly BASE_DIR_ALIAS=.*|readonly BASE_DIR_ALIAS=${WORK}/var-run/twinraid-source-guard|" \
  -e "s|^readonly LOGGER=.*|readonly LOGGER=${WORK}/logger-klein|" \
  -e "s|^readonly SSH=.*|readonly SSH=${WORK}/ssh|" \
  -e "s|^readonly SYNC_KEY=.*|readonly SYNC_KEY=${WORK}/ssh-sync|" \
  -e "s|^readonly KNOWN_HOSTS=.*|readonly KNOWN_HOSTS=${WORK}/target-known-hosts|" \
  -e "s|^readonly POOL=.*|readonly POOL=${KLEIN}|" \
  -e "s|^readonly SELF_DATASET=.*|readonly SELF_DATASET=${KLEIN}/appdata/twinraid|" \
  -e "s|^readonly DAY=.*|readonly DAY=1|" \
  -e "s|^readonly SNAPSHOT_GAP=.*|readonly SNAPSHOT_GAP=0|" \
  "${REPO_DIR}/host-scripts/source-guard.sh" >"${SOURCE_GUARD}"
expect_equal "source guard: only the test values differ" \
  "$(diff "${REPO_DIR}/host-scripts/source-guard.sh" "${SOURCE_GUARD}" | grep -c '^>' || true)" 11

# Statt ssh: feste Form prüfen, dann guard.sh wie sshd mit command="… guard.sh sync".
cat >"${WORK}/ssh" <<EOF
#!/bin/bash
set -euo pipefail
printf '%s\n' "\$*" >>"${WORK}/ssh-calls.log"
args=("\$@")
if ((\${#args[@]} != 29)) || [[ "\${args[0]} \${args[1]} \${args[2]} \${args[26]}" != "-F /dev/null -i --" ]]; then
  echo "unexpected ssh call" >&2
  exit 255
fi
if [[ "\$(stat -c '%a %u' -- "\${args[3]}")" != "600 0" ]]; then
  echo "Load key: bad permissions" >&2
  exit 255
fi
sleep "\$(cat "${WORK}/ssh-delay" 2>/dev/null || echo 0)"
SSH_ORIGINAL_COMMAND="\${args[28]}" exec /bin/bash "${GUARD}" sync
EOF
chmod 0755 "${WORK}/ssh"
# Key und Hostkey nur als vorhandene Dateien; die ssh-Hilfe meldet sich nicht an.
printf 'fake key\n' >"${WORK}/ssh-sync"
chmod 0644 "${WORK}/ssh-sync"
printf '192.0.2.11 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake\n' >"${WORK}/target-known-hosts"
cat >"${WORK}/source-guard.conf" <<'EOF'
dataset twinraidtest
dataset dokumente
max_deletions 5
target_host 192.0.2.11
snapshot_keep_days 1
EOF

/bin/bash "${SOURCE_GUARD}" start
wait_status zfs.state ok 30
expect_equal "guard active" "$(status_field error)" "null"
expect_equal "protocol 2" "$(status_field protocol)" "2"

echo "== Erster Lauf: vollständig"
order prepare no_job
order snapshot created
snap="$(job snapshot)"
expect_contains "snapshot name" "${snap}" "twinraid-"
expect_equal "snapshot of twinraidtest" "$(value twinraid:created-by "${KLEIN}/twinraidtest@${snap}"):$(source_of twinraid:created-by "${KLEIN}/twinraidtest@${snap}")" "source-guard:local"
expect_equal "snapshot of dokumente" "$(value twinraid:created-by "${KLEIN}/dokumente@${snap}")" "source-guard"
expect_equal "same transaction group" "$(value createtxg "${KLEIN}/twinraidtest@${snap}")" \
  "$(value createtxg "${KLEIN}/dokumente@${snap}")"
expect_equal "not recursive" "$(zfs list -H -t snapshot -o name -- "${KLEIN}/twinraidtest/kind" | wc -l)" "0"
order prepare preparing
wait_job "prepared|awaiting_confirm|failed"
expect_equal "first prepare" "$(job state):$(job send_ready):$(job blockers)" "prepared:true:[]"
expect_equal "check from guard.sh" "$(status_field target.check.result)" "ok"
expect_equal "mirror absent" "$(status_field target.check.mirrors.0.state)" "absent"
expect_equal "full stream" "$(dataset_field 0 mode):$(dataset_field 1 mode)" "full:full"
size0="$(dataset_field 0 size)"
expected_size="$(zfs send -n -v -P -L -c -e -- "${KLEIN}/twinraidtest@${snap}" | awk -F'\t' '$1 == "size" {print $2}')"
expect_equal "size from zfs send -nvP" "${size0}" "${expected_size}"
order send sending
wait_job "done|failed"
expect_equal "first send" "$(job state):$(job result)" "done:ok"
expect_equal "sent bytes" "$(dataset_field 0 sent)" "${size0}"
for name in twinraidtest dokumente; do
  expect_equal "mirror ${name} has the snapshot" "$(value guid "${ROOT}/${name}@${snap}")" \
    "$(value guid "${KLEIN}/${name}@${snap}")"
  expect_equal "mirror ${name} read-only" "$(value readonly "${ROOT}/${name}"):$(source_of readonly "${ROOT}/${name}")" "on:local"
  expect_equal "new base held on ${name}" "$(held "${KLEIN}/${name}@${snap}")" "twinraid-base "
  expect_equal "bookmark of ${name}" "$(value guid "${KLEIN}/${name}#${snap}")" \
    "$(value guid "${KLEIN}/${name}@${snap}")"
done
expect_equal "content on Groß" "$(cat /mnt/mainpool/twinraid-mirror/twinraidtest/datei1.txt)" "Datei 1"
expect_equal "child dataset not sent" "$(zfs list -H -o name -- "${ROOT}/twinraidtest/kind" 2>/dev/null || true)" ""
expect_contains "received as UID 64000" "$(zpool history -l mainpool | grep 'zfs receive' | tail -1)" "[user 64000"
first="${snap}"

echo "== Zweiter Lauf: inkrementell mit Zählungen"
cd "/mnt/${KLEIN}/twinraidtest"
rm datei1.txt datei2.txt datei3.txt
printf 'geändert\n' >>datei4.txt
printf 'geändert\n' >>datei5.txt
for i in 1 2 3 4; do printf 'neu %s\n' "${i}" >"neu${i}.txt"; done
mv datei6.txt umbenannt.txt
cd /
full_run
snap="$(job snapshot)"
expect_equal "incremental prepare" "$(job state):$(job send_ready)" "prepared:true"
expect_equal "mode and base" "$(dataset_field 0 mode):$(dataset_field 0 base_type):$(dataset_field 0 base_guid)" \
  "incremental:snapshot:$(value guid "${KLEIN}/twinraidtest@${first}")"
expect_equal "counts deleted, added, renamed" \
  "$(dataset_field 0 deleted):$(dataset_field 0 added):$(dataset_field 0 renamed)" "3:4:1"
modified="$(dataset_field 0 modified)"
if ((modified >= 2)); then pass "modified counted (${modified})"; else fail "modified: ${modified}"; fi
expect_equal "unchanged dataset" "$(dataset_field 1 deleted):$(dataset_field 1 added)" "0:0"
expect_equal "estimate for the increment" "$(dataset_field 0 size)" \
  "$(zfs send -n -v -P -L -c -e -i "@${first}" -- "${KLEIN}/twinraidtest@${snap}" | awk -F'\t' '$1 == "size" {print $2}')"
order send sending
wait_job "done|failed"
expect_equal "second send" "$(job state):$(job result)" "done:ok"
expect_equal "renamed file on Groß" "$(cat /mnt/mainpool/twinraid-mirror/twinraidtest/umbenannt.txt)" "Datei 6"
expect_equal "deleted file gone on Groß" "$(ls /mnt/mainpool/twinraid-mirror/twinraidtest/datei1.txt 2>/dev/null || true)" ""
expect_equal "old snapshot kept on Groß" "$(value guid "${ROOT}/twinraidtest@${first}")" \
  "$(value guid "${KLEIN}/twinraidtest#${first}")"
expect_equal "hold moved" "$(held "${KLEIN}/twinraidtest@${snap}")" "twinraid-base "
# Aufbewahrung (ein Tag ist im Test eine Sekunde): der alte, nicht mehr gehaltene Snapshot ist weg.
expect_equal "retention on Klein" "$(own_snapshots "${KLEIN}/twinraidtest")" "${KLEIN}/twinraidtest@${snap}"
expect_equal "foreign snapshot untouched" "$(value name "${KLEIN}/twinraidtest@manuell")" "${KLEIN}/twinraidtest@manuell"
expect_equal "bookmark of the old base kept" "$(value name "${KLEIN}/twinraidtest#${first}")" "${KLEIN}/twinraidtest#${first}"

echo "== Löschungen über max_deletions brauchen eine Freigabe"
rm /mnt/"${KLEIN}"/twinraidtest/datei{7,8,9,10,11,12}.txt
full_run
expect_equal "awaiting confirm" "$(job state)" awaiting_confirm
# Bei den kleinen Testdateien kann zusätzlich shrunk gelten.
expect_contains "deletions need a confirm" "$(job confirm_reasons)" '"deletions"'
expect_equal "deleted" "$(dataset_field 0 deleted)" "6"
order send wrong_state
order confirm confirmed
order send sending
wait_job "done|failed"
expect_equal "send after confirm" "$(job state):$(job result):$(job confirmed)" "done:ok:true"

echo "== Basis fehlt, Lesezeichen ist da"
snap="$(job snapshot)"
zfs release twinraid-base "${KLEIN}/dokumente@${snap}"
zfs destroy "${KLEIN}/dokumente@${snap}"
printf 'mehr\n' >"/mnt/${KLEIN}/dokumente/mehr.txt"
full_run
expect_equal "bookmark as base" "$(dataset_field 1 base_type):$(dataset_field 1 deleted)" "bookmark:null"
expect_equal "no_diff needs confirm" "$(job state):$(job confirm_reasons)" 'awaiting_confirm:["no_diff"]'
order confirm confirmed
order send sending
wait_job "done|failed"
expect_equal "send from the bookmark" "$(job state):$(job result)" "done:ok"
snap="$(job snapshot)"
expect_equal "mirror dokumente got it" "$(value guid "${ROOT}/dokumente@${snap}")" \
  "$(value guid "${KLEIN}/dokumente@${snap}")"

echo "== Fremder Snapshot auf Groß"
zfs snapshot "${ROOT}/twinraidtest@hand"
full_run
expect_equal "base missing" "$(dataset_field 0 problem):$(job send_ready):$(job blockers)" \
  'base_missing:false:["base_missing"]'
order send not_ready
order close closed
zfs destroy "${ROOT}/twinraidtest@hand"

echo "== Ablehnungen von guard.sh"
full_run
order send sending
wait_job "done|failed"
expect_equal "first send of this round" "$(job result)" "ok"
# Ohne age_mirrors: der nächste Empfang käme vor Ablauf von 10 min.
printf 'noch mehr\n' >"/mnt/${KLEIN}/dokumente/noch.txt"
order snapshot created
order prepare preparing
wait_job "prepared|awaiting_confirm|failed"
order send sending
wait_job "done|failed"
expect_equal "too soon on Groß" "$(job state):$(job result):$(job reason)" "failed:target_refused:too_soon"
expect_equal "dataset results" "$(dataset_field 0 result):$(dataset_field 1 result)" "target_refused:skipped"
expect_contains "refusal in the syslog of Groß" "$(cat "${WORK}/syslog-gross.log")" "too-soon"
age_mirrors
order snapshot created
order prepare preparing
wait_job "prepared|awaiting_confirm|failed"
flock "${WORK}/receive.lock" sleep 30 &
locker=$!
sleep 0.5
order send sending
wait_job "done|failed"
kill "${locker}" 2>/dev/null || true
wait "${locker}" 2>/dev/null || true
expect_equal "receive busy on Groß" "$(job result):$(job reason)" "target_refused:receive_busy"

echo "== close und Neustart während einer Übertragung"
full_run
before="$(zfs list -H -t snapshot -o name -- "${ROOT}/twinraidtest" | wc -l)"
# Die ssh-Hilfe wartet vor guard.sh; zfs send hängt so am vollen Puffer.
echo 10 >"${WORK}/ssh-delay"
order send sending
wait_job sending
sleep 1
order close closed
sleep 1
expect_equal "no zfs send left" "$(pgrep -f "zfs send -L -c -e -v -P" | wc -l)" "0"
expect_equal "no ssh helper left" "$(pgrep -f "${WORK}/ssh" | wc -l)" "0"
expect_equal "closed job" "$(job state):$(job result)" "closed:closed"
expect_equal "nothing new on Groß" "$(zfs list -H -t snapshot -o name -- "${ROOT}/twinraidtest" | wc -l)" "${before}"
rm -f "${WORK}/ssh-delay"
full_run
echo 10 >"${WORK}/ssh-delay"
order send sending
wait_job sending
sleep 1
/bin/bash "${SOURCE_GUARD}" stop
expect_equal "interrupted by stop" "$(job state):$(job result)" "failed:interrupted"
expect_equal "no zfs send left after stop" "$(pgrep -f "zfs send -L -c -e -v -P" | wc -l)" "0"
rm -f "${WORK}/ssh-delay"
/bin/bash "${SOURCE_GUARD}" start
wait_status zfs.state ok 30
expect_equal "job stays interrupted" "$(job state):$(job result)" "failed:interrupted"
expect_equal "receive lock free on Groß" "$(flock -n "${WORK}/receive.lock" true && echo free)" "free"
full_run
order send sending
wait_job "done|failed"
expect_equal "next run after the restart" "$(job state):$(job result)" "done:ok"

echo "== Feste Argumente für ssh"
bad="$(grep -v -c -- "-F /dev/null -i ${BASE}/private/ssh-sync -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${WORK}/target-known-hosts -o GlobalKnownHostsFile=/dev/null -o HostKeyAlgorithms=ssh-ed25519 -o ConnectTimeout=5 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -p 22 -l root -- 192.0.2.11 " "${WORK}/ssh-calls.log" || true)"
expect_equal "every ssh call used the fixed list" "${bad}" "0"
expect_equal "only check and receive" \
  "$(awk '{print $NF == "check" ? "check" : $(NF-1)}' "${WORK}/ssh-calls.log" | sort -u | tr '\n' ' ')" "check receive "
expect_equal "never a destroy with -r on Klein" "$(zpool history "${KLEIN}" | grep -c 'zfs destroy -r' || true)" "0"

echo
if ((failures > 0)); then
  echo "${failures} check(s) failed" >&2
  echo "--- syslog Klein" >&2
  cat "${WORK}/syslog-klein.log" >&2 || true
  echo "--- syslog Groß" >&2
  cat "${WORK}/syslog-gross.log" >&2 || true
  exit 1
fi
echo "all checks passed"
