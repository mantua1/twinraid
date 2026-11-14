#!/usr/bin/env bash
# Test der Anleitung docs/WIEDERHERSTELLUNG.md (Konzept 4.6 und 5.6, ab v0.5.3): Notbetrieb
# auf Groß und Rückweg nach Klein mit echtem ZFS in Dateien.
#
# Läuft als root auf einem Wegwerf-Rechner (CI-Runner) mit installiertem zfsutils-linux
# und nur mit RECOVERY_TEST_HOST=disposable; sonst bricht es ab, bevor es einen Pool, eine
# Datei oder einen zfs-Befehl anfasst. Es arbeitet nur mit zwei Pools in Dateien, die es
# selbst anlegt, und prüft vor jedem Abschnitt der Anleitung, dass es genau diese sind.
# Das Skript kommt nicht ins Image (docker/Dockerfile.dockerignore).
#
# Aufbau wie docker/source-guard-sync-test.sh: Ein Pool "mainpool" spielt Groß
# (mainpool/twinraid-mirror mit der Delegation für UID 64000 und dem echten guard.sh), ein
# Pool "twinraid-klein" spielt Klein (der echte source-guard.sh, Pool per sed ersetzt).
# Zwei Syncs legen den Spiegel an. Danach liest das Skript die Befehlskästen der Abschnitte
# „Variablen und Verbindung zu Klein“, „Letzten Stand finden“, „Notbetrieb starten“,
# „Rückweg nach Klein“ und „Aufräumen auf Groß“ aus der Anleitung und führt sie aus.
#
# Ersetzt wird nur ssh root@<IP von Klein> in der Zeile klein() { … }, durch eine lokale
# Hilfe: Sie führt den Befehl wie sshd über eine Shell aus, auf dem Pool, der Klein spielt.
# Auf einem Rechner kann es nur einen Pool "mainpool" geben; die Hilfe übersetzt deshalb
# den Namen des Pools in den Argumenten nach twinraid-klein und in der Ausgabe zurück, so
# dass die Anleitung Klein sieht, als hieße sein Pool mainpool wie auf dem Server.
#
# Der Rückweg enthält zfs receive -F (CLAUDE.md, Regel 7: nur hier in der CI und nur von
# Hand nach der Anleitung). Das Skript prüft am Ende, dass -F nur auf dem Pool von Klein und
# nur über die Hilfe lief.
#
# Prüft: Klon mit allen Eigenschaften aus Konzept 4.6 und beschreibbar; ein leeres Dataset
# von Unraid wird entfernt, ein volles nicht; guard.sh lehnt jeden Empfang mit clone-active
# ab, der Probelauf meldet clone_active; die Prüfungen vor dem Rückweg halten bei einem Hold
# und bei einer falschen Basis an; der Rückweg setzt Klein auf den Stand des Klons, neuere
# eigene Snapshots auf Klein sind weg, Basis, Hold und Lesezeichen bleiben; nach dem
# Aufräumen ist ein Sync inkrementell von derselben Basis erfolgreich, verify-latest findet
# die Änderung aus dem Notbetrieb, und Spiegel und seine Snapshots sind unverändert (GUIDs).
#
# Aufruf aus dem Repo-Hauptordner:
#   sudo RECOVERY_TEST_HOST=disposable docker/recovery-test.sh
set -euo pipefail

die() {
  echo "recovery-test: $*" >&2
  exit 2
}

# Vor allem anderen: ohne den Schalter und ohne root nichts anlegen und kein zfs aufrufen.
if [[ "${RECOVERY_TEST_HOST:-}" != disposable ]]; then
  die "führt zfs receive -F aus und legt Pools an; nur mit RECOVERY_TEST_HOST=disposable"
fi
if ((EUID != 0)); then
  die "muss als root laufen"
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DOC="${REPO_DIR}/docs/WIEDERHERSTELLUNG.md"
WORK=/var/tmp/recovery-test
GUARD="${WORK}/guard.sh"
SOURCE_GUARD="${WORK}/source-guard.sh"
KLEIN=twinraid-klein
ROOT=mainpool/twinraid-mirror
NAME=twinraidtest
MIRROR="${ROOT}/${NAME}"
BASE="${WORK}/run/twinraid-source-guard"
REQUEST="${BASE}/in/request"
STATUS="${BASE}/state/status.json"
PERMS=acltype,atime,canmount,checksum,compression,copies,create,dedup,devices,exec,mount,mountpoint,primarycache,quota,readonly,receive:append,recordsize,refquota,refreservation,reservation,secondarycache,setuid,sharenfs,sharesmb,snapdir,sync,userprop,xattr
PLACEHOLDER='ssh root@<IP von Klein>'

for pool in mainpool "${KLEIN}"; do
  if zpool list -H -o name "${pool}" >/dev/null 2>&1; then
    die "es gibt schon einen Pool ${pool}"
  fi
done
if [[ -e "${WORK}" ]]; then
  die "${WORK} gibt es schon"
fi

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
  if [[ "$2" == *"$3"* ]]; then pass "$1"; else fail "$1: '$3' not found in '${2:0:1500}'"; fi
}
expect_not_contains() {
  if [[ "$2" != *"$3"* ]]; then pass "$1"; else fail "$1: '$3' found in '${2:0:1500}'"; fi
}

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

order() {
  local seq
  seq=$(($(status_field last_seq) + 1))
  order_line "${seq}" "$1" >"${REQUEST}"
  wait_status last.seq "${seq}" 20 || true
  expect_equal "order $1" "$(status_field last.result)" "$2"
}

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
dataset_field() { status_field "job.datasets.0.$1"; }
held() { zfs holds -H -- "$1" | awk -F'\t' '{print $2}' | tr '\n' ' '; }
own_snapshots() {
  zfs list -H -t snapshot -o name -s createtxg -- "$1" |
    while read -r snapshot; do
      if [[ "$(value twinraid:created-by "${snapshot}"):$(source_of twinraid:created-by "${snapshot}")" == source-guard:local ]]; then
        echo "${snapshot}"
      fi
    done
}
# Der Mindestabstand von 10 min in guard.sh (too-soon): twinraid:received am neuesten
# Snapshot des Spiegels als root zurückstellen, wie es nach 11 Minuten wäre.
age_mirror() {
  local newest
  # Vor dem ersten Sync gibt es den Spiegel noch nicht.
  newest="$(zfs list -H -t snapshot -o name -s createtxg -- "${MIRROR}" 2>/dev/null | tail -1)" || newest=""
  if [[ -n "${newest}" ]]; then
    zfs set -- "twinraid:received=$(($(date +%s) - 660))" "${newest}"
  fi
}
# Snapshot und Prüfung über den Wächter, wie ein Probelauf oder der Anfang eines Syncs.
prepare_run() {
  age_mirror
  sleep 1
  order snapshot created
  order prepare preparing
  wait_job "prepared|awaiting_confirm|failed"
}
send_run() {
  if [[ "$(job state)" == awaiting_confirm ]]; then
    order confirm confirmed
  fi
  order send sending
  wait_job "done|failed"
}
# Snapshots des Spiegels mit GUID, ältester zuerst.
mirror_snapshots() { zfs list -H -t snapshot -o name,guid -s createtxg -d 1 -- "${MIRROR}"; }

# --- Kästen aus der Anleitung ----------------------------------------------------------

# Die Kästen eines Abschnitts (Überschrift genau so, bis zur nächsten gleich hohen oder
# höheren), je Kasten eine Datei blocks/<Kennung>-<n>.sh; gibt die Anzahl aus.
extract_blocks() {
  local title="$1" tag="$2"
  python3 - "${DOC}" "${title}" "${WORK}/blocks/${tag}" <<'PY'
import re
import sys
import textwrap

text = open(sys.argv[1], encoding="utf-8").read()
title, prefix = sys.argv[2], sys.argv[3]
match = re.search(rf"^(#+) {re.escape(title)}\n", text, flags=re.M)
if match is None:
    sys.exit(f"Abschnitt {title!r} fehlt in der Anleitung")
level = len(match[1])
end = re.compile(rf"^#{{1,{level}}} ", flags=re.M).search(text, match.end())
section = text[match.end() : end.start() if end else len(text)]
blocks = [
    textwrap.dedent(block)
    for _, block in re.findall(r"^( *)```bash\n(.*?)^\1```", section, flags=re.S | re.M)
]
for number, block in enumerate(blocks, start=1):
    with open(f"{prefix}-{number}.sh", "w", encoding="utf-8") as handle:
        handle.write(block)
print(len(blocks))
PY
}

# Nummer des ersten Kastens eines Abschnitts, der den Text enthält.
block_with() {
  local tag="$1" text="$2" file
  for file in "${WORK}/blocks/${tag}"-*.sh; do
    if grep -qF -- "${text}" "${file}"; then
      file="${file##*-}"
      echo "${file%.sh}"
      return 0
    fi
  done
  die "kein Kasten in ${tag} mit ${text}"
}

# Vor jedem Abschnitt: Es gibt genau die beiden eigenen Pools, beide aus Dateien in WORK.
check_pools() {
  local pools vdevs
  pools="$(zpool list -H -o name | sort | tr '\n' ' ')"
  [[ "${pools}" == "mainpool ${KLEIN} " ]] || die "unerwartete Pools: ${pools}"
  vdevs="$(zpool list -H -v -P mainpool "${KLEIN}" | awk -F'\t' '$2 ~ /^\// {print $2}' | sort | tr '\n' ' ')"
  [[ "${vdevs}" == "${WORK}/klein.img ${WORK}/mainpool.img " ]] || die "unerwartete Geräte: ${vdevs}"
  [[ "$(cat "${WORK}/pool-marker")" == "$(zpool get -H -o value guid mainpool) $(zpool get -H -o value guid "${KLEIN}")" ]] ||
    die "die Pools sind nicht die vom Anfang des Tests"
}

# run <Kennung> <Kasten>…: Kasten „Variablen und Verbindung zu Klein“ und die genannten
# Kästen des Abschnitts in einer Shell, wie in einem Terminal nacheinander eingefügt.
# Die Ausgabe steht danach in OUT, der Exit-Code in RC.
run() {
  local tag="$1" script="${WORK}/run.sh" number line
  shift
  check_pools
  cp "${WORK}/vars.sh" "${script}"
  for number in "$@"; do
    if [[ "${number}" == B=* ]]; then
      printf '%s\n' "${number}" >>"${script}"
    else
      cat "${WORK}/blocks/${tag}-${number}.sh" >>"${script}"
    fi
  done
  set +e
  OUT="$(cd "${WORK}" && bash -eu "${script}" 2>&1)"
  RC=$?
  set -e
  while IFS= read -r line; do
    echo "      | ${line}"
  done <<<"${OUT}"
}

run_all() {
  local tag="$1" count i numbers=()
  count="$(find "${WORK}/blocks" -name "${tag}-*.sh" | wc -l)"
  for ((i = 1; i <= count; i++)); do
    numbers+=("${i}")
  done
  run "${tag}" "${numbers[@]}"
  expect_equal "section ${tag}: all ${count} blocks ran" "${RC}" "0"
}

echo "== Vorbereitung"
mkdir -p "${WORK}/run" "${WORK}/var-run" "${WORK}/blocks"
truncate -s 2G "${WORK}/mainpool.img" "${WORK}/klein.img"
zpool create -o cachefile=none -o ashift=12 -m /mnt/mainpool \
  -O compression=on -O atime=off -O xattr=sa -O acltype=posix mainpool "${WORK}/mainpool.img"
zfs create "${ROOT}"
zfs allow -u 64000 "${PERMS}" "${ROOT}"
printf 'mirror %s\n' "${NAME}" >"${WORK}/mirror.conf"
zpool create -o cachefile=none -o ashift=12 -m "/mnt/${KLEIN}" -O compression=on \
  -O xattr=sa -O acltype=posix "${KLEIN}" "${WORK}/klein.img"
printf '%s %s\n' "$(zpool get -H -o value guid mainpool)" "$(zpool get -H -o value guid "${KLEIN}")" \
  >"${WORK}/pool-marker"
zfs create "${KLEIN}/${NAME}"
bash "${REPO_DIR}/host-scripts/testdata.sh" create "/mnt/${KLEIN}/${NAME}/testdata" >/dev/null
printf 'vor dem Ausfall\n' >"/mnt/${KLEIN}/${NAME}/klein.txt"

cp /etc/passwd "${WORK}/passwd"
cp /etc/group "${WORK}/group"
for side in gross klein; do
  cat >"${WORK}/logger-${side}" <<EOF
#!/bin/sh
printf 'Nov  8 10:00:00 Server %s: %s\\n' "\$2" "\$4" >>"${WORK}/syslog-${side}.log"
EOF
done
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
  -e "s|^readonly SNAPSHOT_MINUTE=.*|readonly SNAPSHOT_MINUTE=0|" \
  "${REPO_DIR}/host-scripts/source-guard.sh" >"${SOURCE_GUARD}"
expect_equal "source guard: only the test values differ" \
  "$(diff "${REPO_DIR}/host-scripts/source-guard.sh" "${SOURCE_GUARD}" | grep -c '^>' || true)" 10
# Statt des ssh des Wächters: guard.sh im Modus sync wie sshd mit der Zeile aus init-key.
cat >"${WORK}/ssh" <<EOF
#!/bin/bash
set -euo pipefail
args=("\$@")
if ((\${#args[@]} != 29)) || [[ "\${args[0]} \${args[1]} \${args[2]} \${args[26]}" != "-F /dev/null -i --" ]]; then
  echo "unexpected ssh call" >&2
  exit 255
fi
SSH_ORIGINAL_COMMAND="\${args[28]}" exec /bin/bash "${GUARD}" sync
EOF
chmod 0755 "${WORK}/ssh"
printf 'fake key\n' >"${WORK}/ssh-sync"
printf '192.0.2.11 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake\n' >"${WORK}/target-known-hosts"
cat >"${WORK}/source-guard.conf" <<EOF
dataset ${NAME}
max_deletions 1000
target_host 192.0.2.11
EOF

# Statt ssh root@<IP von Klein>: der Befehl wie bei sshd über eine Shell, auf dem Pool von
# Klein; Ausgabe und Fehler mit dem Namen mainpool wie auf dem Server. Jeder Aufruf steht
# im Log, damit am Ende feststeht, dass -F nur hier lief.
cat >"${WORK}/klein-ssh" <<EOF
#!/bin/bash
set -uo pipefail
printf '%s\n' "\$*" >>"${WORK}/klein-calls.log"
command="\$*"
command="\${command//mainpool/${KLEIN}}"
/bin/bash -c "\${command}" 2> >(sed -e 's/${KLEIN}/mainpool/g' >&2) | sed -e 's/${KLEIN}/mainpool/g'
EOF
chmod 0755 "${WORK}/klein-ssh"

/bin/bash "${SOURCE_GUARD}" start
wait_status zfs.state ok 30
expect_equal "guard active" "$(status_field error)" "null"

echo "== Kästen aus der Anleitung lesen"
expect_equal "placeholder for Klein exactly once in the guide" "$(grep -cF -- "${PLACEHOLDER}" "${DOC}")" 1
expect_equal "variables: one block" "$(extract_blocks "Variablen und Verbindung zu Klein" vars)" 1
vars="$(cat "${WORK}/blocks/vars-1.sh")"
expect_contains "variables: N is the test share" "${vars}" "N=${NAME}"
expect_contains "variables: klein uses ssh to Klein" "${vars}" "klein() { ${PLACEHOLDER} \"\$@\"; }"
printf '%s\n' "${vars//"${PLACEHOLDER}"/${WORK}/klein-ssh}" >"${WORK}/vars.sh"
for section in "latest:Letzten Stand finden" "emergency:Notbetrieb starten" \
  "back:Rückweg nach Klein" "cleanup:Aufräumen auf Groß"; do
  count="$(extract_blocks "${section#*:}" "${section%%:*}")"
  if ((count > 0)); then pass "section ${section#*:}: ${count} blocks"; else fail "section ${section#*:}: no blocks"; fi
done
# Außer der Zeile für klein darf in den ausgeführten Kästen kein Platzhalter stehen.
expect_equal "no placeholders in the blocks" \
  "$(cat "${WORK}"/blocks/{latest,emergency,back,cleanup}-*.sh | grep -c '<[A-Za-zÄÖÜäöü]' || true)" 0
# Wörtlicher Text aus der Anleitung; $N bleibt dort unaufgelöst.
# shellcheck disable=SC2016
expect_equal "zfs receive -F only once, piped to Klein" \
  "$(cat "${WORK}"/blocks/*.sh | grep -o 'zfs receive.*' | tr '\n' '|')" \
  'zfs receive -F "mainpool/$N" && echo "Rückweg gesendet"|'
expect_contains "the receive goes through klein" "$(cat "${WORK}"/blocks/back-*.sh)" '| klein zfs receive -F'

echo "== Zwei Syncs legen den Spiegel an"
prepare_run
expect_equal "first run is a full stream" "$(dataset_field mode)" "full"
send_run
expect_equal "first sync" "$(job state):$(job result)" "done:ok"
first="$(job snapshot)"
bash "${REPO_DIR}/host-scripts/testdata.sh" change "/mnt/${KLEIN}/${NAME}/testdata" >/dev/null
prepare_run
expect_equal "second run is incremental" "$(dataset_field mode):$(dataset_field base_type)" "incremental:snapshot"
send_run
expect_equal "second sync" "$(job state):$(job result)" "done:ok"
base="$(job snapshot)"
base_guid="$(value guid "${KLEIN}/${NAME}@${base}")"
expect_equal "base held on Klein" "$(held "${KLEIN}/${NAME}@${base}")" "twinraid-base "
expect_equal "mirror has both" "$(mirror_snapshots | cut -f1 | tr '\n' ' ')" "${MIRROR}@${first} ${MIRROR}@${base} "
mirror_before="$(mirror_snapshots)"
# Auf Klein entsteht nach der Basis noch etwas, das der Rückweg verwerfen muss.
printf 'nach der Basis\n' >"/mnt/${KLEIN}/${NAME}/nach-der-basis.txt"

echo "== Letzten Stand finden"
run latest 1
expect_equal "latest: runs" "${RC}" "0"
expect_contains "latest: lists the newest snapshot" "${OUT}" "${MIRROR}@${base}"
expect_contains "latest: time of the receive" "${OUT}" "${MIRROR}@${base}  empfangen "
expect_contains "latest: mirror mounted" "${OUT}" "$(printf 'mounted\tyes')"
expect_not_contains "latest: every snapshot has twinraid:received" "${OUT}" "ohne gültiges"

echo "== Notbetrieb: Dataset von Unraid an der Stelle des Klons"
destroy_block="$(block_with emergency 'hier aufhören')"
zfs create "mainpool/${NAME}"
printf 'fremd\n' >"/mnt/mainpool/${NAME}/fremd.txt"
run emergency "${destroy_block}"
expect_contains "a dataset with data is not removed" "${OUT}" "nichts gelöscht, hier aufhören"
expect_equal "dataset with data still there" "$(cat "/mnt/mainpool/${NAME}/fremd.txt")" "fremd"
rm "/mnt/mainpool/${NAME}/fremd.txt"
zfs snapshot "mainpool/${NAME}@hand"
run emergency "${destroy_block}"
expect_contains "a dataset with a snapshot is not removed" "${OUT}" "nichts gelöscht, hier aufhören"
zfs destroy "mainpool/${NAME}@hand"
# Jetzt leer wie ein Dataset, das Unraid für eine Share-Konfiguration anlegt.

echo "== Notbetrieb starten"
run_all emergency
expect_contains "the empty dataset of Unraid is removed" "${OUT}" "leeres Dataset mainpool/${NAME} entfernt"
expect_contains "clone of the newest snapshot" "${OUT}" "Neuester Stand: ${base}"
expect_contains "clone writable" "${OUT}" "beschreibbar"
expect_equal "clone origin" "$(value origin "mainpool/${NAME}")" "${MIRROR}@${base}"
for pair in readonly=off exec=on setuid=off devices=off primarycache=all compression=lz4 \
  atime=off xattr=sa acltype=posix; do
  property="${pair%%=*}"
  expect_equal "clone ${property}" "$(value "${property}" "mainpool/${NAME}"):$(source_of "${property}" "mainpool/${NAME}")" \
    "${pair#*=}:local"
done
expect_equal "clone mounted where Klein has the share" \
  "$(findmnt -n -o TARGET,SOURCE,FSTYPE "/mnt/mainpool/${NAME}")" "/mnt/mainpool/${NAME} mainpool/${NAME} zfs"
expect_equal "clone is no mirror" "$(source_of twinraid:mirror "mainpool/${NAME}")" "-"
expect_equal "mirror stays read-only" "$(value readonly "${MIRROR}"):$(value mounted "${MIRROR}")" "on:yes"
expect_contains "clone holds the data of the base" \
  "$(bash "${REPO_DIR}/host-scripts/testdata.sh" verify "/mnt/mainpool/${NAME}/testdata" 2>&1 || true)" \
  "Keine Abweichung (Lauf 1)."
expect_equal "clone without the file written after the base" \
  "$(ls "/mnt/mainpool/${NAME}/nach-der-basis.txt" 2>/dev/null || true)" ""

echo "== Im Notbetrieb: Container schreiben in den Klon, Groß lehnt Empfänge ab"
bash "${REPO_DIR}/host-scripts/testdata.sh" change "/mnt/mainpool/${NAME}/testdata" >/dev/null
printf 'aus dem Notbetrieb\n' >"/mnt/mainpool/${NAME}/notbetrieb.txt"
refusal="$(SSH_ORIGINAL_COMMAND="receive ${NAME}" /bin/bash "${GUARD}" sync </dev/null 2>&1)" && rc=0 || rc=$?
expect_equal "guard.sh refuses a receive while a clone exists" "${rc}:${refusal}" \
  "3:twinraid-guard: refused clone-active"
prepare_run
expect_contains "dry run blocked by the clone" "$(job blockers)" '"clone_active"'
expect_equal "check counts the clone" "$(status_field target.check.summary.clones)" "1"
order send not_ready
order close closed
newer="$(job snapshot)"
expect_equal "an own snapshot newer than the base on Klein" \
  "$(own_snapshots "${KLEIN}/${NAME}" | tail -1)" "${KLEIN}/${NAME}@${newer}"
expect_equal "mirror unchanged during the emergency" "$(mirror_snapshots)" "${mirror_before}"

echo "== Klein fällt aus und startet wieder"
/bin/bash "${SOURCE_GUARD}" stop >/dev/null
/bin/bash "${SOURCE_GUARD}" start
wait_status zfs.state ok 30

echo "== Rückweg: die Prüfungen halten an"
# Wörtlicher Text aus der Anleitung, nicht aufgelöst.
# shellcheck disable=SC2016
snapshot_block="$(block_with back 'zfs snapshot "mainpool/$N@notbetrieb-')"
# shellcheck disable=SC2016
values_block="$(block_with back 'R=$(')"
check_block="$(block_with back 'Prüfung in Ordnung')"
send_block="$(block_with back 'zfs receive -F')"
zfs hold test-hold "${KLEIN}/${NAME}@${newer}"
run back "${values_block}" "${check_block}"
expect_contains "a hold on a newer snapshot stops the way back" "${OUT}" \
  "Hold oder Klon an einem neueren Snapshot: nicht weitermachen"
zfs release test-hold "${KLEIN}/${NAME}@${newer}"
klein_before="$(zfs list -H -t snapshot -o name,guid -s createtxg -d 1 "${KLEIN}/${NAME}")"
run back "${snapshot_block}" "${values_block}" "B=gibt-es-nicht" "${send_block}"
expect_contains "a wrong base sends nothing" "${OUT}" "Basis fehlt auf Klein oder hat eine andere GUID; nichts gesendet"
expect_equal "Klein unchanged after the refusal" \
  "$(zfs list -H -t snapshot -o name,guid -s createtxg -d 1 "${KLEIN}/${NAME}")" "${klein_before}"
expect_equal "the file after the base still on Klein" "$(cat "/mnt/${KLEIN}/${NAME}/nach-der-basis.txt")" "nach der Basis"
expect_equal "no zfs receive -F so far" "$(grep -c 'zfs receive' "${WORK}/klein-calls.log" || true)" 0
# Der Snapshot dieses Durchgangs bleibt; der Rückweg nimmt den neuesten.
sleep 1

echo "== Rückweg nach Klein"
run_all back
notbetrieb="$(zfs list -H -t snapshot -o name -s createtxg -d 1 "mainpool/${NAME}" | tail -1)"
notbetrieb="${notbetrieb#*@}"
expect_contains "values for the way back" "${OUT}" "Basis: ${base}, Stand aus dem Notbetrieb: ${notbetrieb}"
expect_contains "Klein reachable through klein" "${OUT}" "mainpool/${NAME}"
expect_contains "base found on Klein with the same GUID" "${OUT}" "${base_guid}
${base_guid}"
expect_contains "the check passes" "${OUT}" "Prüfung in Ordnung"
expect_contains "the newer snapshot is named" "${OUT}" "wird gelöscht: mainpool/${NAME}@${newer}"
expect_contains "changes on Klein after the base are listed" "${OUT}" "/mnt/mainpool/${NAME}/nach-der-basis.txt"
expect_contains "sent" "${OUT}" "Rückweg gesendet"
expect_equal "Klein has the state of the emergency" \
  "$(value guid "${KLEIN}/${NAME}@${notbetrieb}")" "$(value guid "mainpool/${NAME}@${notbetrieb}")"
expect_equal "file from the emergency on Klein" "$(cat "/mnt/${KLEIN}/${NAME}/notbetrieb.txt")" "aus dem Notbetrieb"
expect_equal "file after the base is gone" "$(ls "/mnt/${KLEIN}/${NAME}/nach-der-basis.txt" 2>/dev/null || true)" ""
expect_equal "the file from before the failure stays" "$(cat "/mnt/${KLEIN}/${NAME}/klein.txt")" "vor dem Ausfall"
# Geprüft im Snapshot: Im eingehängten Dataset kann der Kernel nach einem Empfang noch eine
# schon gelesene ACL zeigen (Konzept 3.7); die Ansicht dort meldet der Test nur.
expect_contains "test data of the emergency on Klein" \
  "$(bash "${REPO_DIR}/host-scripts/testdata.sh" verify "/mnt/${KLEIN}/${NAME}/.zfs/snapshot/${notbetrieb}/testdata" 2>&1 || true)" \
  "Keine Abweichung (Lauf 2)."
echo "info  verify in the mounted dataset on Klein: $(bash "${REPO_DIR}/host-scripts/testdata.sh" verify "/mnt/${KLEIN}/${NAME}/testdata" 2>&1 | tail -1 || true)"
expect_equal "newer own snapshot gone on Klein" "$(value name "${KLEIN}/${NAME}@${newer}" 2>/dev/null || true)" ""
expect_equal "own snapshots on Klein: first and base" \
  "$(own_snapshots "${KLEIN}/${NAME}" | tr '\n' ' ')" "${KLEIN}/${NAME}@${first} ${KLEIN}/${NAME}@${base} "
expect_equal "base keeps its hold" "$(held "${KLEIN}/${NAME}@${base}")" "twinraid-base "
expect_equal "base keeps its GUID" "$(value guid "${KLEIN}/${NAME}@${base}")" "${base_guid}"
expect_equal "bookmarks stay" "$(zfs list -H -t bookmark -o name -d 1 "${KLEIN}/${NAME}" | tr '\n' ' ')" \
  "${KLEIN}/${NAME}#${first} ${KLEIN}/${NAME}#${base} "
expect_equal "bookmark of the base in the clone" "$(value guid "mainpool/${NAME}#${base}")" "${base_guid}"
expect_equal "the mirror has no bookmark" "$(zfs list -H -t bookmark -o name -d 1 "${MIRROR}" | wc -l)" 0
expect_equal "the received snapshot is no own snapshot" \
  "$(source_of twinraid:created-by "${KLEIN}/${NAME}@${notbetrieb}")" "-"
expect_equal "mirror unchanged after the way back" "$(mirror_snapshots)" "${mirror_before}"

echo "== Aufräumen auf Groß"
run_all cleanup
expect_contains "clone removed" "${OUT}" "Klon mainpool/${NAME} gelöscht"
expect_contains "no dataset in the place of the clone" "${OUT}" "kein Dataset mainpool/${NAME}"
expect_contains "no share configuration" "${OUT}" "keine Share-Konfiguration"
expect_equal "clone gone" "$(zfs list -H -o name "mainpool/${NAME}" 2>/dev/null || true)" ""
expect_equal "bookmark of the clone gone" "$(zfs list -H -t bookmark -o name -r mainpool | grep -c "^mainpool/${NAME}#" || true)" 0
expect_equal "mirror and its snapshots unchanged" "$(mirror_snapshots)" "${mirror_before}"
expect_equal "no clones of the mirror" "$(zfs list -H -t snapshot -o clones -d 1 "${MIRROR}" | sort -u)" "-"
expect_contains "check: clones=0" "$(SSH_ORIGINAL_COMMAND=check /bin/bash "${GUARD}" sync)" "clones=0"
run cleanup "$(block_with cleanup 'ist kein Klon eines Spiegels')"
expect_contains "a second run deletes nothing" "${OUT}" "mainpool/${NAME} ist kein Klon eines Spiegels; nichts gelöscht"
expect_equal "mirror still there" "$(mirror_snapshots)" "${mirror_before}"

echo "== Danach: Sync inkrementell von derselben Basis"
prepare_run
expect_equal "incremental from the base" \
  "$(dataset_field mode):$(dataset_field base_type):$(dataset_field base_guid)" "incremental:snapshot:${base_guid}"
expect_equal "ready to send" "$(job send_ready):$(job blockers)" "true:[]"
send_run
expect_equal "sync after the emergency" "$(job state):$(job result)" "done:ok"
after="$(job snapshot)"
expect_equal "mirror kept its snapshots and got one more" "$(mirror_snapshots | head -n 2)" "${mirror_before}"
expect_equal "mirror has the new snapshot" "$(value guid "${MIRROR}@${after}")" "$(value guid "${KLEIN}/${NAME}@${after}")"
report="$(bash "${REPO_DIR}/host-scripts/testdata.sh" verify-latest "${MIRROR}" 2>&1 || true)"
expect_contains "verify-latest finds the change of the emergency" "${report}" "Keine Abweichung (Lauf 2)."
expect_equal "file of the emergency in the mirror" \
  "$(cat "/mnt/mainpool/twinraid-mirror/${NAME}/.zfs/snapshot/${after}/notbetrieb.txt")" "aus dem Notbetrieb"
expect_equal "new base held" "$(held "${KLEIN}/${NAME}@${after}")" "twinraid-base "

echo "== -F nur auf Klein und nur über klein"
expect_equal "one receive through klein" "$(grep -c 'zfs receive' "${WORK}/klein-calls.log" || true)" 1
expect_contains "with -F into the dataset of Klein" "$(grep 'zfs receive' "${WORK}/klein-calls.log")" \
  "zfs receive -F mainpool/${NAME}"
expect_equal "never zfs receive -F on Groß" "$(zpool history mainpool | grep -c 'zfs receive -F' || true)" 0
expect_equal "zfs receive -F once on Klein" "$(zpool history "${KLEIN}" | grep -c 'zfs receive -F' || true)" 1
expect_equal "never a recursive destroy" \
  "$(zpool history mainpool "${KLEIN}" | grep -c -E 'zfs destroy -[a-zA-Z]*[rR]' || true)" 0
expect_equal "never a rollback on Groß" "$(zpool history mainpool | grep -c 'zfs rollback' || true)" 0

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
