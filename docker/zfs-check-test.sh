#!/usr/bin/env bash
# Test von host-scripts/zfs-check.sh mit ZFS-Pools in Dateien (Konzept 3.7 und 5.6).
#
# Läuft als root auf einem Wegwerf-Rechner (CI-Runner) mit installiertem zfsutils-linux
# und nur mit ZFS_CHECK_TEST_HOST=disposable. Ein Pool "mainpool" in einer Datei spielt
# Groß nach dem Schritt "Groß prüfen" (mit mainpool/twinraid-mirror und einer weiteren
# Share). Geprüft wird, dass zfs-check.sh in beiden Arten durchläuft, danach nichts
# übrig bleibt, keine Rechte auf twinraid-mirror stehen, fremde Daten unberührt bleiben,
# Voraussetzungen und Reste früherer Läufe erkannt werden, ein Abbruch (SIGTERM) aufräumt
# und "cleanup" die Reste nach kill -9 entfernt, aber nichts Fremdes.
#
# Aufruf aus dem Repo-Hauptordner:
#   sudo ZFS_CHECK_TEST_HOST=disposable docker/zfs-check-test.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
CHECK="${REPO_DIR}/host-scripts/zfs-check.sh"
WORK=/var/tmp/zfs-check-test
POOL_FILE="${WORK}/mainpool.img"

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
  if [[ "$2" == *"$3"* ]]; then pass "$1"; else fail "$1: '$3' not found in '${2:0:600}'"; fi
}
expect_not_contains() {
  if [[ "$2" != *"$3"* ]]; then pass "$1"; else fail "$1: '$3' must not appear"; fi
}
die() {
  echo "zfs-check-test: $*" >&2
  exit 2
}

if ((EUID != 0)); then
  die "muss als root laufen"
fi
if [[ "${ZFS_CHECK_TEST_HOST:-}" != disposable ]]; then
  die "legt Pools an; nur mit ZFS_CHECK_TEST_HOST=disposable"
fi
if zpool list -H -o name mainpool >/dev/null 2>&1; then
  die "es gibt schon einen Pool mainpool"
fi

check() { /bin/bash "${CHECK}" "$@"; }
state() {
  zfs list -H -t all -o name -r mainpool | sort
  zfs allow mainpool/twinraid-mirror
  zpool list -H -o name | sort
}
own_files() { find /tmp -maxdepth 1 -name 'twinraid-check-*' | sort; }

cleanup() {
  pkill -9 -f "${CHECK}" 2>/dev/null || true
  sleep 1
  zpool list -H -o name 2>/dev/null | grep -E '^twinraid-check-' | while IFS= read -r pool; do
    zpool destroy -f "${pool}" || true
  done
  zpool destroy -f mainpool 2>/dev/null || true
  rm -rf "${WORK}" /tmp/twinraid-check-*
}
trap cleanup EXIT

echo "== Vorbereitung"
mkdir -p "${WORK}"
truncate -s 4G "${POOL_FILE}"
zpool create -o cachefile=none -o ashift=12 -m /mnt/mainpool \
  -O compression=on -O atime=off -O xattr=sa -O acltype=posix mainpool "${POOL_FILE}"
zfs create mainpool/isos
printf 'fremde Daten\n' >/mnt/mainpool/isos/bleibt.txt
zfs snapshot mainpool/isos@fremd

echo "== Voraussetzung: twinraid-mirror fehlt"
rc=0
out="$(check target 2>&1)" || rc=$?
expect_equal "exit code without twinraid-mirror" "${rc}" 2
expect_contains "hint to check Groß first" "${out}" "Groß prüfen"
zfs create mainpool/twinraid-mirror
before="$(state)"

echo "== Lauf auf Groß (target)"
rc=0
out="$(check target 2>&1)" || rc=$?
echo "${out}"
expect_equal "target run exit code" "${rc}" 0
expect_contains "target run: all checks passed" "${out}" "Alle Prüfungen in Ordnung."
expect_not_contains "target run: no FEHLER" "${out}" "FEHLER"
expect_contains "memory measurement ran" "${out}" "Messung: ARC"
expect_contains "receive:append examined" "${out}" "receive:append"
expect_equal "nothing left, no permissions on twinraid-mirror" "$(state)" "${before}"
expect_equal "foreign data untouched" "$(cat /mnt/mainpool/isos/bleibt.txt)" "fremde Daten"
expect_equal "foreign snapshot untouched" "$(zfs list -H -o name -t snapshot -r mainpool/isos)" \
  "mainpool/isos@fremd"

echo "== Rest eines früheren Laufs"
zfs create -o twinraid:zfs-check=deadbeef mainpool/twinraid-mirror/zfs-check
zfs snapshot mainpool/twinraid-mirror/zfs-check@rest
rc=0
out="$(check target 2>&1)" || rc=$?
expect_equal "exit code with a leftover" "${rc}" 2
expect_contains "leftover reported" "${out}" "cleanup target"
out="$(check cleanup target 2>&1)" || true
expect_contains "cleanup target" "${out}" "Aufgeräumt."
expect_equal "leftover removed" "$(state)" "${before}"

echo "== Fremdes Dataset an derselben Stelle"
zfs create mainpool/twinraid-mirror/zfs-check
rc=0
out="$(check cleanup target 2>&1)" || rc=$?
expect_equal "cleanup refuses a foreign dataset" "${rc}" 1
expect_contains "reason named" "${out}" "trägt twinraid:zfs-check nicht"
if zfs list -H -o name mainpool/twinraid-mirror/zfs-check >/dev/null 2>&1; then
  pass "foreign dataset kept"
else
  fail "foreign dataset removed"
fi
zfs destroy mainpool/twinraid-mirror/zfs-check

echo "== Abbruch mit SIGTERM räumt auf"
check target >"${WORK}/term.log" 2>&1 &
pid=$!
sleep 2
kill -TERM "${pid}"
rc=0
wait "${pid}" || rc=$?
if ((rc != 0)); then pass "terminated run ends with an error code (${rc})"; else fail "terminated run returned 0"; fi
expect_contains "abort reported" "$(cat "${WORK}/term.log")" "abgebrochen"
expect_equal "nothing left after SIGTERM" "$(state)" "${before}"

echo "== kill -9 lässt Reste; cleanup entfernt nur sie"
# Eigene Prozessgruppe, damit kill -9 auch Kindprozesse beendet.
setsid /bin/bash "${CHECK}" target >"${WORK}/kill.log" 2>&1 &
pid=$!
sleep 2
kill -9 -- "-${pid}"
wait "${pid}" 2>/dev/null || true
sleep 1
if zfs list -H -o name mainpool/twinraid-mirror/zfs-check >/dev/null 2>&1; then
  pass "kill -9 leaves a leftover"
else
  fail "no leftover after kill -9 (test did not reach the cases)"
fi
out="$(check cleanup target 2>&1)" || true
expect_contains "cleanup after kill -9" "${out}" "Aufgeräumt."
expect_equal "nothing left after cleanup" "$(state)" "${before}"

echo "== Lauf auf Klein (source)"
rc=0
out="$(check source 2>&1)" || rc=$?
echo "${out}"
expect_equal "source run exit code" "${rc}" 0
expect_contains "source run: all checks passed" "${out}" "Alle Prüfungen in Ordnung."
expect_not_contains "source run: no FEHLER" "${out}" "FEHLER"
expect_not_contains "no memory measurement on Klein" "${out}" "Messung: ARC"
expect_equal "pools unchanged after source run" "$(state)" "${before}"
expect_equal "no files left in /tmp" "$(own_files)" ""

echo "== kill -9 auf Klein; cleanup source"
setsid /bin/bash "${CHECK}" source >"${WORK}/kill-source.log" 2>&1 &
pid=$!
sleep 2
kill -9 -- "-${pid}"
wait "${pid}" 2>/dev/null || true
sleep 1
if zpool list -H -o name | grep -q '^twinraid-check-'; then pass "leftover pool after kill -9"; else
  fail "no leftover pool after kill -9"
fi
rc=0
out="$(check source 2>&1)" || rc=$?
expect_equal "source run refuses with a leftover pool" "${rc}" 2
out="$(check cleanup source 2>&1)" || true
expect_contains "cleanup source" "${out}" "Aufgeräumt."
expect_equal "pools unchanged after cleanup source" "$(state)" "${before}"
expect_equal "no files left in /tmp after cleanup" "$(own_files)" ""

if ((failures > 0)); then
  echo "${failures} check(s) failed" >&2
  exit 1
fi
echo "All zfs-check checks passed."
