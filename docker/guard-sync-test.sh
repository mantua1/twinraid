#!/usr/bin/env bash
# Test von host-scripts/guard.sh, Modus sync, mit echtem ZFS in Dateien (Konzept 3.7, E26, E27).
#
# Läuft als root auf einem Wegwerf-Rechner (CI-Runner) mit installiertem zfsutils-linux
# und nur mit GUARD_SYNC_TEST_HOST=disposable. Ein Pool "mainpool" in einer Datei spielt
# Groß nach dem Schritt "Groß prüfen" (mit mainpool/twinraid-mirror und der Delegation für
# UID 64000 aus der Anleitung), ein zweiter Pool spielt die Datasets auf Klein. Gesendet
# wird wie vom Wächter (zfs send -L -c -e), dazu absichtlich böse Ströme (-p, -R).
#
# guard.sh läuft unverändert bis auf vier Pfade: Syslog, mirror.conf, /etc/passwd und
# /etc/group zeigen in einen Testordner, die Sperre des Empfangs ebenso. zfs, setpriv und
# findmnt sind echt; zfs receive läuft wirklich als UID 64000.
#
# Aufruf aus dem Repo-Hauptordner:
#   sudo GUARD_SYNC_TEST_HOST=disposable docker/guard-sync-test.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
WORK=/var/tmp/guard-sync-test
GUARD="${WORK}/guard.sh"
SRC=twinraid-sync-src
ROOT=mainpool/twinraid-mirror
MIRROR="${ROOT}/twinraidtest"
MOUNT=/mnt/mainpool/twinraid-mirror/twinraidtest
# Die 28 Rechte aus Z1b, wie in der Anleitung (Konzept 3.7).
PERMS=acltype,atime,canmount,checksum,compression,copies,create,dedup,devices,exec,mount,mountpoint,primarycache,quota,readonly,receive:append,recordsize,refquota,refreservation,reservation,secondarycache,setuid,sharenfs,sharesmb,snapdir,sync,userprop,xattr
FIXED=(
  readonly=on exec=off setuid=off devices=off primarycache=metadata secondarycache=all
  compression=lz4 atime=off xattr=sa acltype=posix recordsize=128K snapdir=hidden
  sync=standard dedup=off copies=1 checksum=on quota=none refquota=none reservation=none
  refreservation=none canmount=on sharesmb=off sharenfs=off twinraid:mirror=1
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
expect_not_contains() {
  if [[ "$2" != *"$3"* ]]; then pass "$1"; else fail "$1: '$3' must not appear"; fi
}
die() {
  echo "guard-sync-test: $*" >&2
  exit 2
}

if ((EUID != 0)); then
  die "muss als root laufen"
fi
if [[ "${GUARD_SYNC_TEST_HOST:-}" != disposable ]]; then
  die "legt Pools an; nur mit GUARD_SYNC_TEST_HOST=disposable"
fi
for pool in mainpool "${SRC}"; do
  if zpool list -H -o name "${pool}" >/dev/null 2>&1; then
    die "es gibt schon einen Pool ${pool}"
  fi
done

cleanup() {
  local rc=$?
  set +e
  zpool destroy -f mainpool 2>/dev/null
  zpool destroy -f "${SRC}" 2>/dev/null
  rm -rf "${WORK}"
  exit "${rc}"
}
trap cleanup EXIT

# --- Hilfen ------------------------------------------------------------------------------

GUARD_OUT=""
GUARD_ERR=""
GUARD_RC=0
# guard.sh im Modus sync; stdin geht an zfs receive.
run_guard() {
  GUARD_RC=0
  GUARD_OUT="$(SSH_ORIGINAL_COMMAND="$1" /bin/bash "${GUARD}" sync 2>"${WORK}/stderr")" ||
    GUARD_RC=$?
  GUARD_ERR="$(cat "${WORK}/stderr")"
}
# receive <Name> <Argumente für zfs send>: sendet wie der Wächter (-L -c -e).
receive() {
  local name="$1"
  shift
  run_guard "receive ${name}" < <(zfs send -L -c -e "$@" 2>/dev/null)
}
# Wie receive, aber mit eigenen Optionen für zfs send (böse Ströme).
receive_raw() {
  local name="$1"
  shift
  run_guard "receive ${name}" < <(zfs send "$@" 2>/dev/null)
}
expect_refused() {
  expect_equal "$1" "${GUARD_RC}:${GUARD_ERR}" "3:twinraid-guard: refused $2"
}
expect_received() {
  expect_equal "$1" "${GUARD_RC}:${GUARD_ERR}:${GUARD_OUT}" "0::"
}
check() {
  run_guard check
  echo "${GUARD_OUT}"
}
value() { zfs get -H -o value -- "$1" "$2"; }
source_of() { zfs get -H -o source -- "$1" "$2"; }
where() { findmnt -n -o TARGET -S "$1" 2>/dev/null || true; }
syslog() { cat "${WORK}/syslog.log" 2>/dev/null || true; }
# Der Mindestabstand von 10 min (too-soon): twinraid:received am neuesten Snapshot als root
# zurückstellen, wie es nach 11 Minuten wäre.
age_newest() {
  local newest
  newest="$(zfs list -H -t snapshot -o name -s createtxg -- "$1" | tail -1)"
  zfs set -- "twinraid:received=$(($(date +%s) - ${2:-660}))" "${newest}"
}
locked() {
  [[ "$(value canmount "$1"):$(source_of canmount "$1"):$(value mounted "$1")" == "noauto:local:no" ]]
}
expect_locked() {
  if locked "$2"; then pass "$1"; else
    fail "$1: canmount=$(value canmount "$2") mounted=$(value mounted "$2")"
  fi
}
# Alle festen Werte mit Quelle local und der feste Ort.
expect_fixed() {
  local dataset="$1" mount="$2" pair bad=""
  for pair in "${FIXED[@]}"; do
    if [[ "$(value "${pair%%=*}" "${dataset}")" != "${pair#*=}" ||
      "$(source_of "${pair%%=*}" "${dataset}")" != local ]]; then
      bad+=" ${pair%%=*}"
    fi
  done
  if [[ "$(value mountpoint "${dataset}")" != "${mount}" ||
    "$(source_of mountpoint "${dataset}")" != local ]]; then
    bad+=" mountpoint"
  fi
  expect_equal "fixed values with source local on ${dataset}" "${bad}" ""
}

echo "== Vorbereitung"
mkdir -p "${WORK}"
truncate -s 2G "${WORK}/mainpool.img" "${WORK}/source.img"
zpool create -o cachefile=none -o ashift=12 -m /mnt/mainpool \
  -O compression=on -O atime=off -O xattr=sa -O acltype=posix mainpool "${WORK}/mainpool.img"
zfs create "${ROOT}"
zpool create -o cachefile=none -o ashift=12 -m "/mnt/${SRC}" -O compression=on \
  "${SRC}" "${WORK}/source.img"
zfs create "${SRC}/twinraidtest"
printf 'eins\n' >"/mnt/${SRC}/twinraidtest/eins.txt"
zfs snapshot "${SRC}/twinraidtest@a"
# Kopie von guard.sh mit Testpfaden für Syslog, mirror.conf, Benutzer und Sperre.
cp /etc/passwd "${WORK}/passwd"
cp /etc/group "${WORK}/group"
# shellcheck disable=SC2016 # $4 gehört in das erzeugte Skript (logger -t … -- Meldung).
printf '#!/bin/sh\nprintf "%%s\\n" "$4" >>%s\n' "${WORK}/syslog.log" >"${WORK}/logger"
chmod 0755 "${WORK}/logger"
sed -e "s|^readonly LOGGER=.*|readonly LOGGER=${WORK}/logger|" \
  -e "s|^readonly MIRROR_CONF=.*|readonly MIRROR_CONF=${WORK}/mirror.conf|" \
  -e "s|^readonly PASSWD=.*|readonly PASSWD=${WORK}/passwd|" \
  -e "s|^readonly GROUP=.*|readonly GROUP=${WORK}/group|" \
  -e "s|^readonly RECEIVE_LOCK=.*|readonly RECEIVE_LOCK=${WORK}/receive.lock|" \
  "${REPO_DIR}/host-scripts/guard.sh" >"${GUARD}"
expect_equal "only the four test paths and the lock differ" \
  "$(diff "${REPO_DIR}/host-scripts/guard.sh" "${GUARD}" | grep -c '^>' || true)" 5

echo "== check vor der Einrichtung"
out="$(check)"
expect_contains "check without mirror.conf" "${out}" "summary root=ok conf=missing deleg=mismatch receive=idle"

echo "== Delegation wie in der Anleitung (Konzept 3.7, E27)"
zfs allow -u 64000 "${PERMS}" "${ROOT}"
expected="$(printf -- '---- Permissions on %s ' "${ROOT}")"
while ((${#expected} < 70)); do expected+="-"; done
expected+=$'\nLocal+Descendent permissions:\n\tuser (unknown: 64000) '"${PERMS}"
expect_equal "zfs allow prints exactly what guard.sh expects" "$(zfs allow "${ROOT}")" "${expected}"
printf 'mirror twinraidtest  # Test-Share\nmirror isos\nmirror fremd\nmirror kinder\n' >"${WORK}/mirror.conf"
out="$(check)"
expect_contains "check: version line" "${out}" "twinraid-guard 3 sync ok"
expect_contains "check: absent mirror" "${out}" "mirror twinraidtest state=absent props=- count=0 "
expect_contains "check: summary" "${out}" "summary root=ok conf=ok deleg=ok receive=idle clones=0 available="
expect_contains "check: defaults" "${out}" " reserve_percent=15 keep_days=30 min_interval_minutes=10"

echo "== Erster Empfang"
before="$(zpool history mainpool | wc -l)"
check >/dev/null
expect_equal "check changes nothing in the pool" "$(zpool history mainpool | wc -l)" "${before}"
receive twinraidtest "${SRC}/twinraidtest@a"
expect_received "first receive"
expect_fixed "${MIRROR}" "${MOUNT}"
expect_equal "mounted at the fixed place" "$(where "${MIRROR}")" "${MOUNT}"
expect_equal "content arrived" "$(cat "${MOUNT}/eins.txt")" "eins"
expect_equal "same GUID as on Klein" "$(value guid "${MIRROR}@a")" "$(value guid "${SRC}/twinraidtest@a")"
expect_equal "creation taken over from Klein" "$(zfs get -H -p -o value creation "${MIRROR}@a")" \
  "$(zfs get -H -p -o value creation "${SRC}/twinraidtest@a")"
expect_equal "twinraid:received with source local" "$(source_of twinraid:received "${MIRROR}@a")" local
expect_equal "no property with source received" \
  "$(zfs get -H -r -s received -o name,property all "${MIRROR}")" ""
expect_contains "zfs receive ran as UID 64000" "$(zpool history -l mainpool | grep 'zfs receive' | tail -1)" \
  "[user 64000"
out="$(check)"
expect_contains "check reports the mirror" "${out}" \
  "mirror twinraidtest state=mirror props=ok count=1 newest_guid=$(value guid "${MIRROR}@a") "

echo "== Inkrementeller Empfang und Mindestabstand"
printf 'zwei\n' >"/mnt/${SRC}/twinraidtest/zwei.txt"
zfs snapshot "${SRC}/twinraidtest@b"
receive twinraidtest -i @a "${SRC}/twinraidtest@b"
expect_refused "second receive within 10 minutes" too-soon
age_newest "${MIRROR}"
receive twinraidtest -i @a "${SRC}/twinraidtest@b"
expect_received "incremental receive"
expect_equal "new file visible" "$(cat "${MOUNT}/zwei.txt")" "zwei"
expect_fixed "${MIRROR}" "${MOUNT}"
expect_equal "still mounted at the fixed place" "$(where "${MIRROR}")" "${MOUNT}"

echo "== Kein Rückfall auf root: Delegation genau prüfen"
age_newest "${MIRROR}"
printf 'drei\n' >"/mnt/${SRC}/twinraidtest/drei.txt"
zfs snapshot "${SRC}/twinraidtest@c"
zfs unallow -u 64000 exec "${ROOT}"
receive twinraidtest -i @b "${SRC}/twinraidtest@c"
expect_refused "one right missing" delegation-mismatch
expect_contains "check: deleg=mismatch" "$(check)" " deleg=mismatch "
zfs allow -u 64000 exec,destroy "${ROOT}"
receive twinraidtest -i @b "${SRC}/twinraidtest@c"
expect_refused "one right too many" delegation-mismatch
zfs unallow -u 64000 destroy "${ROOT}"
zfs allow -g 64000 mount "${ROOT}"
receive twinraidtest -i @b "${SRC}/twinraidtest@c"
expect_refused "a group as well" delegation-mismatch
zfs unallow -g 64000 mount "${ROOT}"
zfs allow -u 64000 -l snapshot "${ROOT}"
receive twinraidtest -i @b "${SRC}/twinraidtest@c"
expect_refused "local permissions as well" delegation-mismatch
zfs unallow -u 64000 -l snapshot "${ROOT}"
zfs allow -u 64000 mount mainpool
receive twinraidtest -i @b "${SRC}/twinraidtest@c"
expect_refused "rights on the pool" delegation-mismatch
zfs unallow -u 64000 mount mainpool
zfs allow -u 64000 mount "${MIRROR}"
receive twinraidtest -i @b "${SRC}/twinraidtest@c"
expect_refused "rights on the mirror" delegation-mismatch
zfs unallow -u 64000 mount "${MIRROR}"
zfs allow -c destroy "${ROOT}"
receive twinraidtest -i @b "${SRC}/twinraidtest@c"
expect_refused "create time permissions" delegation-mismatch
zfs unallow -c destroy "${ROOT}"
expect_equal "delegation as in the guide again" "$(zfs allow "${ROOT}")" "${expected}"
printf 'twinraid:x:64000:64000::/:/bin/false\n' >>"${WORK}/passwd"
receive twinraidtest -i @b "${SRC}/twinraidtest@c"
expect_refused "UID 64000 in /etc/passwd" receiver-id-in-use
expect_contains "check: deleg=id-in-use" "$(check)" " deleg=id-in-use "
cp /etc/passwd "${WORK}/passwd"
receive twinraidtest -i @b "${SRC}/twinraidtest@c"
expect_received "receive with the delegation restored"

echo "== Feste Werte vor jedem Empfang (das Recht readonly erlaubt auch readonly=off)"
age_newest "${MIRROR}"
zfs snapshot "${SRC}/twinraidtest@d"
zfs set readonly=off "${MIRROR}"
receive twinraidtest -i @c "${SRC}/twinraidtest@d"
expect_refused "readonly=off before the receive" props-not-applied
expect_locked "locked after props-not-applied" "${MIRROR}"
receive twinraidtest -i @c "${SRC}/twinraidtest@d"
expect_refused "locked mirror refuses" props-mismatch
expect_contains "check: props=mismatch" "$(check)" "mirror twinraidtest state=mirror props=mismatch "
# Zurücksetzen von Hand nach props-not-applied (Konzept 3.7): Werte setzen, Sperre aufheben.
zfs set readonly=on "${MIRROR}"
zfs set canmount=on "${MIRROR}"
expect_contains "check: props=ok after the reset" "$(check)" "mirror twinraidtest state=mirror props=ok "
receive twinraidtest -i @c "${SRC}/twinraidtest@d"
expect_received "receive after the reset"
expect_equal "mounted again after the reset" "$(where "${MIRROR}")" "${MOUNT}"

echo "== Strom mit Eigenschaften (zfs send -p) sperrt"
age_newest "${MIRROR}"
zfs set quota=1G "${SRC}/twinraidtest"
zfs set mountpoint=/mnt/anderswo "${SRC}/twinraidtest"
zfs set twinraid:received=1 "${SRC}/twinraidtest"
zfs snapshot "${SRC}/twinraidtest@e"
receive_raw twinraidtest -p -L -c -e -i @d "${SRC}/twinraidtest@e"
zfs inherit mountpoint "${SRC}/twinraidtest"
expect_refused "stream with properties" received-properties
expect_locked "locked after received-properties" "${MIRROR}"
expect_equal "quota stays fixed" "$(value quota "${MIRROR}")" none
expect_equal "nothing mounted elsewhere" "$(where "${MIRROR}")" ""
expect_not_contains "the suspicious snapshot is not marked" \
  "$(source_of twinraid:received "${MIRROR}@e")" local
expect_contains "the refusal is in the syslog" "$(syslog)" "refused: received-properties"
# Zurücksetzen von Hand (Konzept 3.7): verdächtigen Snapshot löschen, Rollback auf den
# letzten guten, nur gemeldete Eigenschaften zurücksetzen, Sperre aufheben.
zfs destroy "${MIRROR}@e"
zfs rollback "${MIRROR}@d"
# Nur genau die gemeldeten Eigenschaften, nie eine feste der Empfangszeile.
while IFS=$'\t' read -r name property; do
  zfs inherit -- "${property}" "${name}"
done < <(zfs get -H -r -s received -o name,property all "${MIRROR}")
expect_equal "no received property after the reset" \
  "$(zfs get -H -r -s received -o name,property all "${MIRROR}")" ""
zfs set canmount=on "${MIRROR}"
zfs set quota=none "${SRC}/twinraidtest"
zfs inherit twinraid:received "${SRC}/twinraidtest"
zfs snapshot "${SRC}/twinraidtest@f"
receive twinraidtest -i @d "${SRC}/twinraidtest@f"
expect_received "incremental from the base after the reset"
expect_fixed "${MIRROR}" "${MOUNT}"

echo "== Verändertes Ziel (target-modified)"
age_newest "${MIRROR}"
zfs set readonly=off "${MIRROR}"
printf 'fremd\n' >"${MOUNT}/fremd.txt"
zfs set readonly=on "${MIRROR}"
zfs snapshot "${SRC}/twinraidtest@g"
receive twinraidtest -i @f "${SRC}/twinraidtest@g"
expect_refused "modified target" target-modified
expect_contains "first line of zfs in the syslog" "$(syslog)" "zfs receive: cannot receive incremental stream"
expect_not_contains "the message of zfs does not go to Klein" "${GUARD_ERR}" "cannot receive"
rm "${MOUNT}/fremd.txt" 2>/dev/null || true
zfs rollback "${MIRROR}@f"

echo "== Falsche Basis: Exit-Code 1 ohne Text"
receive twinraidtest -i @a "${SRC}/twinraidtest@g"
expect_equal "wrong base" "${GUARD_RC}:${GUARD_ERR}" "1:"
expect_equal "mirror unchanged" "$(zfs list -H -t snapshot -o name -s createtxg "${MIRROR}" | tail -1)" \
  "${MIRROR}@f"

echo "== Fehlendes Recht: zfs receive übergeht -o, guard.sh sperrt (props-not-applied)"
# Die Prüfung der Delegation fände das fehlende Recht vorher. Eine Kopie, die nur 27 Rechte
# erwartet, zeigt, dass auch die Prüfung nach dem Empfang allein sperrt (Konzept 3.7).
sed -e 's|^readonly RECV_PERMS=.*|readonly RECV_PERMS='"${PERMS/exec,/}"'|' "${GUARD}" >"${WORK}/guard-27.sh"
zfs unallow -u 64000 exec "${ROOT}"
zfs create "${SRC}/isos"
zfs snapshot "${SRC}/isos@a"
rc=0
err="$(SSH_ORIGINAL_COMMAND="receive isos" /bin/bash "${WORK}/guard-27.sh" sync \
  < <(zfs send -L -c -e "${SRC}/isos@a") 2>&1 >/dev/null)" || rc=$?
expect_equal "missing right" "${rc}:${err}" "3:twinraid-guard: refused props-not-applied"
expect_locked "locked although never mounted" "${ROOT}/isos"
expect_equal "exec keeps its default" "$(source_of exec "${ROOT}/isos")" default
expect_contains "warning of zfs in the syslog" "$(syslog)" "zfs receive: cannot receive exec property"
zfs allow -u 64000 exec "${ROOT}"

echo "== Strom mit Kindern (zfs send -R) sperrt Spiegel und Kinder"
zfs create "${SRC}/kinder"
zfs create -o mountpoint=/mnt/etc-kind "${SRC}/kinder/etc"
zfs snapshot -r "${SRC}/kinder@a"
receive_raw kinder -R -L -c -e "${SRC}/kinder@a"
expect_refused "stream with children" received-properties
expect_locked "mirror locked" "${ROOT}/kinder"
expect_locked "child locked" "${ROOT}/kinder/etc"
expect_equal "child not mounted anywhere" "$(findmnt -n -o TARGET /mnt/etc-kind 2>/dev/null || true)" ""
expect_contains "check: props=mismatch with children" "$(check)" "mirror kinder state=mirror props=mismatch "

echo "== Fremdes Dataset an der Stelle eines Spiegels (E26)"
zfs create "${ROOT}/fremd"
zfs create "${SRC}/fremd"
zfs snapshot "${SRC}/fremd@a"
receive fremd "${SRC}/fremd@a"
expect_refused "foreign dataset" target-exists
expect_equal "foreign dataset unchanged" "$(zfs list -H -t snapshot -o name -r "${ROOT}/fremd")" ""
expect_contains "check: foreign" "$(check)" "mirror fremd state=foreign props=- count=- "

echo "== Klon eines Spiegel-Snapshots sperrt jeden Empfang"
age_newest "${MIRROR}"
zfs clone -o readonly=off -o canmount=noauto "${MIRROR}@f" mainpool/klon
zfs snapshot "${SRC}/twinraidtest@h"
receive twinraidtest -i @f "${SRC}/twinraidtest@h"
expect_refused "clone of a mirror snapshot" clone-active
expect_contains "check counts the clone" "$(check)" " clones=1 "
zfs destroy mainpool/klon

echo "== Platzreserve"
zfs set quota=20M "${ROOT}"
receive twinraidtest -i @f "${SRC}/twinraidtest@h"
expect_refused "space below the reserve" space-low
zfs set quota=none "${ROOT}"

echo "== Aufbewahrung"
receive twinraidtest -i @f "${SRC}/twinraidtest@h"
expect_received "receive before the retention test"
for snap in i j; do
  age_newest "${MIRROR}"
  zfs snapshot "${SRC}/twinraidtest@${snap}"
  previous="$(zfs list -H -t snapshot -o name -s createtxg "${SRC}/twinraidtest" | tail -2 | head -1)"
  receive twinraidtest -i "@${previous#*@}" "${SRC}/twinraidtest@${snap}"
  expect_received "receive ${snap}"
done
old=$(($(date +%s) - 40 * 86400))
for snap in a b c d f; do
  zfs set "twinraid:received=${old}" "${MIRROR}@${snap}"
done
zfs inherit twinraid:received "${MIRROR}@b"
zfs hold twinraid-test "${MIRROR}@c"
zfs set "twinraid:received=${old}" "${MIRROR}@h"
age_newest "${MIRROR}" 700
zfs snapshot "${SRC}/twinraidtest@k"
receive twinraidtest -i @j "${SRC}/twinraidtest@k"
expect_received "receive triggers the retention"
# a, d, f und h sind alt; b trägt kein twinraid:received mit Quelle local, c hat einen
# Hold; i, j und k sind die drei neuesten.
expect_equal "retention kept the right snapshots" \
  "$(zfs list -H -t snapshot -o name -s createtxg "${MIRROR}" | sed 's/.*@//' | tr '\n' ' ')" \
  "b c i j k "
expect_contains "retention in the syslog" "$(syslog)" "retention of mirror twinraidtest: 4 snapshots destroyed, 1 failed"
zfs release twinraid-test "${MIRROR}@c"
expect_equal "locked mirror untouched by retention" \
  "$(zfs list -H -t snapshot -o name "${ROOT}/kinder" | wc -l)" 1

echo "== Übrige Anfragen"
run_guard "receive system"
expect_refused "system is never a mirror" name-denied
run_guard "receive ../twinraidtest"
expect_equal "malformed name" "${GUARD_RC}:${GUARD_ERR}" "2:twinraid-guard: denied"
printf 'mirror twinraidtest\nmirror domains\n' >"${WORK}/mirror.conf"
run_guard "receive twinraidtest"
expect_refused "invalid mirror.conf" name-denied
expect_contains "invalid line named" "$(syslog)" "mirror.conf is invalid (line 2)"

if ((failures > 0)); then
  echo "${failures} check(s) failed" >&2
  echo "--- syslog of guard.sh" >&2
  syslog >&2
  exit 1
fi
echo "All guard.sh sync checks passed."
