#!/bin/bash
# TwinRaid: Machbarkeitstest Z1 für den ZFS-Spiegel (Konzept 3.7 und 5.6).
#
# Prüft auf den echten Servern, wie sich OpenZFS bei genau den Befehlen verhält, die
# Wächter und guard.sh ab v0.5.1 verwenden: Snapshot mehrerer Datasets in einem Aufruf,
# Senden mit -L -c -e, Empfang mit der festen Befehlszeile (alle Eigenschaften mit -o
# direkt am Spiegel, -u, Prüfung vor dem Einhängen, nie -F), ein absichtlich böser Strom
# mit Sperre und Zurücksetzen, was ohne -F scheitert, Lesezeichen, Hold, zfs diff, die
# Größenschätzung, Klone für den Notbetrieb, ob -x wirkt (nur als Befund), ob
# receive:append unter Linux als zweite Sperre trägt und auf Groß, ob MemAvailable den
# ARC schon enthält.
#
#   zfs-check.sh target    auf Groß, nach „Groß prüfen“ (mainpool/twinraid-mirror muss es
#                          geben). Arbeitet nur in mainpool/twinraid-mirror/zfs-check.
#                          Für die Messung zum Arbeitsspeicher sollten alle VMs aus sein.
#   zfs-check.sh source    auf Klein. Arbeitet nur in einem eigenen Pool in einer Datei
#                          unter /tmp (cachefile=none); die echten Pools bleiben unberührt.
#   zfs-check.sh cleanup target|source
#                          entfernt Reste eines abgebrochenen Laufs (nur eigene Objekte).
#
# Alles, was das Skript anlegt, trägt die Eigenschaft twinraid:zfs-check. Am Ende und
# bei jedem Abbruch löscht es nur diese Objekte, einzeln und nie mit -r oder -R, und
# nennt alles, was es nicht entfernen konnte. Die Ausgabe enthält nur Codes und Zahlen;
# sie kann so in den PR. Exit-Code: 0 alles in Ordnung, 1 mindestens ein FEHLER,
# 2 Voraussetzung fehlt oder Reste eines früheren Laufs.
set -euo pipefail
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 022

readonly POOL=mainpool
readonly MIRROR_ROOT=mainpool/twinraid-mirror
readonly TARGET_ROOT=mainpool/twinraid-mirror/zfs-check
readonly PROPERTY=twinraid:zfs-check
readonly FILE_PREFIX=/tmp/twinraid-check-
readonly FILE_POOL_PREFIX=twinraid-check-
readonly FILE_POOL_SIZE=1G
readonly MEASURE_BYTES=536870912
readonly GIB=1073741824
# Feste Befehlszeile für den Empfang, wie guard.sh sie ab Version 3 verwendet (Konzept 3.7).
# Dazu kommt je Spiegel -o mountpoint=<fester Ort>; alle Werte gelten mit Quelle local.
readonly RECEIVE_OPTIONS=(
  -u
  -o readonly=on -o exec=off -o setuid=off -o devices=off
  -o primarycache=metadata -o secondarycache=all -o compression=lz4 -o atime=off
  -o xattr=sa -o acltype=posix -o recordsize=128K -o snapdir=hidden
  -o sync=standard -o dedup=off -o copies=1 -o checksum=on
  -o quota=none -o refquota=none -o reservation=none -o refreservation=none
  -o canmount=on
  -o sharesmb=off -o sharenfs=off -o twinraid:mirror=1
)
# Eigenschaften mit Quelle received, die nach einem Empfang erlaubt sind. Leer: Ein Strom
# des Wächters (ohne -p) bringt keine Eigenschaften mit. Jeder Eintrag braucht eine Begründung.
readonly RECEIVED_ALLOWED=()
# Die frühere Befehlszeile mit -x; nur noch für den Befund, ob -x auf diesem Server wirkt.
readonly X_RECEIVE_OPTIONS=(
  -o readonly=on -o exec=off -o setuid=off -o devices=off
  -x quota -x mountpoint
)
# Eigenschaften der Klone im Notbetrieb (Konzept 4.6).
readonly CLONE_OPTIONS=(
  -o readonly=off -o exec=on -o setuid=off -o devices=off -o primarycache=all
  -o compression=lz4 -o atime=off -o xattr=sa -o acltype=posix
)

failures=0
ROOT=""
RUN_ID=""
FILE_POOL=""
FILE_PATH=""
ALTROOT=""
ALLOWED_USER=""
CREATED_ROOT=0
CREATED_POOL=0
XATTR_SET=0
ACL_SET=0
FINISHED=0

ok() { printf 'ok     %s\n' "$1"; }
bad() {
  printf 'FEHLER %s\n' "$1"
  failures=$((failures + 1))
}
info() { printf 'info   %s\n' "$1"; }
stop() {
  printf 'zfs-check: %s\n' "$1" >&2
  exit 2
}

# Prüft einen Befehl, der gelingen muss; die Ausgabe von zfs wird nicht weitergegeben.
expect_success() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    ok "${label}"
  else
    bad "${label}"
  fi
}

expect_failure() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    bad "${label}"
  else
    ok "${label}"
  fi
}

# "--" steht immer vor dem ersten Operanden: Einige Unterbefehle von zfs lesen Optionen
# nur bis zum ersten Operanden.
prop() { zfs get -H -p -o value -- "$1" "$2"; }
prop_source() { zfs get -H -o source -- "$1" "$2"; }

mountpoint_of() { zfs list -H -o mountpoint -- "$1"; }
value_of() { zfs get -H -o value -- "$1" "$2"; }

# Fester Ort eines Spiegels: unter dem Ort des übergeordneten Datasets. Setzt MOUNT_SHOWN
# (wie zfs ihn anzeigt) und MOUNT_VALUE (der Wert für -o; ohne altroot des Datei-Pools).
mirror_mountpoint() {
  MOUNT_SHOWN="$(mountpoint_of "${1%/*}")/${1##*/}"
  MOUNT_VALUE="${MOUNT_SHOWN#"${ALTROOT}"}"
}

# Eigenschaften mit Quelle received am Spiegel und an seinen Snapshots, ohne die erlaubten.
received_properties() {
  local line property allowed keep
  {
    zfs get -H -o name,property -s received -- all "$1" 2>/dev/null || true
    zfs get -H -r -t snapshot -o name,property -s received -- all "$1" 2>/dev/null || true
  } | sort -u | while IFS= read -r line; do
    property="${line#*$'\t'}"
    keep=1
    for allowed in "${RECEIVED_ALLOWED[@]+"${RECEIVED_ALLOWED[@]}"}"; do
      if [[ "${property}" == "${allowed}" ]]; then
        keep=0
      fi
    done
    if ((keep)) && [[ -n "${line}" ]]; then
      printf '%s\n' "${line}"
    fi
  done
}

# Wie guard.sh nach dem Empfang (Konzept 3.7): erst prüfen, dann einhängen, dann den Ort
# prüfen. Rückgabe: 0 eingehängt am festen Ort, 2 Eigenschaften aus dem Strom (bleibt
# ausgehängt, gesperrt mit canmount=noauto), 3 Einhängen gescheitert oder falscher Ort
# (wieder ausgehängt).
verify_and_mount() {
  local target="$1" where
  mirror_mountpoint "${target}"
  RECEIVED_FOUND="$(received_properties "${target}")"
  if [[ -n "${RECEIVED_FOUND}" ]]; then
    # Ein schon eingehängter Spiegel (inkrementeller Empfang) wird ausgehängt; canmount=noauto
    # hält ihn auch nach einem Neustart ausgehängt, bis er von Hand zurückgesetzt ist.
    if [[ "$(value_of mounted "${target}")" == yes ]]; then
      zfs unmount -- "${target}" >/dev/null 2>&1 || true
    fi
    zfs set -- canmount=noauto "${target}" >/dev/null 2>&1 || true
    return 2
  fi
  if [[ "$(value_of mounted "${target}")" != yes ]]; then
    zfs mount -- "${target}" >/dev/null 2>&1 || return 3
  fi
  where="$(findmnt -n -o TARGET -S "${target}" 2>/dev/null || true)"
  if [[ "${where}" != "${MOUNT_SHOWN}" ]]; then
    zfs unmount -- "${target}" >/dev/null 2>&1 || true
    return 3
  fi
}

# Sendet mit den Optionen des Wächters (dazu weitere aus den Argumenten), empfängt mit der
# festen Befehlszeile und hängt nach der Prüfung ein.
send_receive() {
  local target="$1"
  shift
  mirror_mountpoint "${target}"
  zfs send -L -c -e "$@" | zfs receive "${RECEIVE_OPTIONS[@]}" -o "mountpoint=${MOUNT_VALUE}" \
    -- "${target}" || return 1
  verify_and_mount "${target}"
}

# Wie die Aufbewahrung in guard.sh: nur twinraid:received mit Quelle local zählt, älter als
# days Tage, nie die drei neuesten Snapshots. Druckt die Snapshots, die gelöscht würden.
retention_candidates() {
  local dataset="$1" days="$2" limit snapshots i value source
  limit=$(($(date +%s) - days * 86400))
  mapfile -t snapshots < <(zfs list -H -t snapshot -o name -s createtxg -- "${dataset}")
  for ((i = 0; i < ${#snapshots[@]} - 3; i++)); do
    IFS=$'\t' read -r value source < <(zfs get -H -p -o value,source -- twinraid:received "${snapshots[i]}")
    if [[ "${source}" == local && "${value}" =~ ^[0-9]{1,12}$ ]] && ((value < limit)); then
      printf '%s\n' "${snapshots[i]}"
    fi
  done
}

# Inhalt eines Dateisystems: Typ, Rechte, Links, Größe, Zeit, Pfad, Ziel und SHA-256.
listing() {
  local dir="$1"
  (
    cd -- "${dir}"
    find . -mindepth 1 -printf '%y %m %n %s %T@ %P %l\n' | sort
    find . -type f -print0 | sort -z | xargs -0 -r sha256sum
  )
}

same_content() {
  local a b
  a="$(listing "$(mountpoint_of "$1")")" || return 1
  b="$(listing "$(mountpoint_of "$2")")" || return 1
  [[ -n "${a}" && "${a}" == "${b}" ]]
}

snapshots_of() { zfs list -H -t snapshot -o name -s createtxg -- "$1" 2>/dev/null || true; }

# Alle mit -o gesetzten Werte und der feste Ort müssen am Spiegel stehen, Quelle local.
# Mit "gesperrt" wird canmount=noauto statt on erwartet.
check_mirror_properties() {
  local dataset="$1" label="$2" locked="${3:-}" i pair name value bad_list=""
  mirror_mountpoint "${dataset}"
  for ((i = 0; i < ${#RECEIVE_OPTIONS[@]}; i++)); do
    if [[ "${RECEIVE_OPTIONS[i]}" != -o ]]; then
      continue
    fi
    pair="${RECEIVE_OPTIONS[i + 1]}"
    name="${pair%%=*}"
    value="${pair#*=}"
    if [[ "${name}" == canmount && -n "${locked}" ]]; then
      value=noauto
    fi
    if [[ "$(value_of "${name}" "${dataset}")" != "${value}" ||
      "$(prop_source "${name}" "${dataset}")" != local ]]; then
      bad_list+=" ${name}"
    fi
  done
  if [[ "$(value_of mountpoint "${dataset}")" != "${MOUNT_SHOWN}" ||
    "$(prop_source mountpoint "${dataset}")" != local ]]; then
    bad_list+=" mountpoint"
  fi
  if [[ -z "${bad_list}" ]]; then
    ok "${label}: alle Eigenschaften am Spiegel mit Quelle local"
  else
    bad "${label}: Eigenschaften abweichend:${bad_list}"
  fi
}

# --- Aufräumen: nur eigene Objekte, einzeln, nie -r ---------------------------------------

# Entfernt alle Objekte unter einem eigenen Wurzel-Dataset, das die Eigenschaft
# twinraid:zfs-check trägt (mit expected genau diesen Wert). Holds des Tests lösen,
# Snapshots und Lesezeichen löschen, dann Datasets von unten nach oben; wiederholt,
# solange es vorangeht (Klone hängen an Snapshots). Druckt, was übrig bleibt.
remove_tree() {
  local root="$1" expected="${2:-}" mark name names progress=1 left
  if ! zfs list -H -o name -- "${root}" >/dev/null 2>&1; then
    return 0
  fi
  mark="$(prop "${PROPERTY}" "${root}" 2>/dev/null || echo -)"
  if [[ "${mark}" == - || -z "${mark}" || (-n "${expected}" && "${mark}" != "${expected}") ]]; then
    printf 'zfs-check: %s trägt %s nicht; nichts entfernt\n' "${root}" "${PROPERTY}" >&2
    return 1
  fi
  zfs unallow -u "${ALLOWED_USER:-nobody}" -- receive:append,mount,create "${root}" >/dev/null 2>&1 || true
  while ((progress)); do
    progress=0
    names="$(zfs list -H -t snapshot -o name -r -- "${root}" 2>/dev/null || true)"
    while IFS= read -r name; do
      if [[ "${name}" == "${root}"[@/]* ]]; then
        if zfs release -- twinraid-base "${name}" >/dev/null 2>&1; then
          progress=1
        fi
      fi
    done <<<"${names}"
    names="$(zfs list -H -t snapshot,bookmark -o name -r -- "${root}" 2>/dev/null || true)"
    while IFS= read -r name; do
      if [[ "${name}" == "${root}"[@#/]* ]]; then
        if zfs destroy -- "${name}" >/dev/null 2>&1; then
          progress=1
        fi
      fi
    done <<<"${names}"
    # Datasets: die tiefsten zuerst (mehr "/" im Namen).
    names="$(zfs list -H -t filesystem,volume -o name -r -- "${root}" 2>/dev/null |
      awk -F/ '{ print NF "\t" $0 }' | sort -rn | cut -f2- || true)"
    while IFS= read -r name; do
      if [[ "${name}" == "${root}/"* ]]; then
        if zfs destroy -- "${name}" >/dev/null 2>&1; then
          progress=1
        fi
      fi
    done <<<"${names}"
  done
  zfs destroy -- "${root}" >/dev/null 2>&1 || true
  left="$(zfs list -H -t all -o name -r -- "${root}" 2>/dev/null || true)"
  if [[ -n "${left}" ]]; then
    printf 'zfs-check: nicht entfernt:\n%s\n' "${left}" >&2
    return 1
  fi
}

destroy_file_pool() {
  local pool="$1" file="$2" vdevs
  if zpool list -H -o name -- "${pool}" >/dev/null 2>&1; then
    vdevs="$(zpool list -H -P -v -o name -- "${pool}" 2>/dev/null || true)"
    if [[ "${vdevs}" != *"${file}"* ]]; then
      printf 'zfs-check: %s liegt nicht in %s; nicht entfernt\n' "${pool}" "${file}" >&2
      return 1
    fi
    if ! zpool destroy -- "${pool}" >/dev/null 2>&1; then
      printf 'zfs-check: nicht entfernt: Pool %s\n' "${pool}" >&2
      return 1
    fi
  fi
  rm -f -- "${file}"
  # Nach dem Aushängen bleiben leere Einhängeordner; nur leere Ordner werden entfernt.
  if [[ -d "${file%.img}" ]]; then
    find "${file%.img}" -depth -type d -empty -delete 2>/dev/null || true
  fi
  if [[ -e "${file%.img}" ]]; then
    printf 'zfs-check: nicht entfernt: %s\n' "${file%.img}" >&2
    return 1
  fi
}

cleanup() {
  local rc=$?
  trap - EXIT
  set +e
  if ((!FINISHED)); then
    printf 'zfs-check: abgebrochen (Code %d); räume auf\n' "${rc}" >&2
  fi
  # Auf Klein liegt alles im eigenen Pool; er wird als Ganzes entfernt.
  if ((CREATED_ROOT && !CREATED_POOL)); then
    if remove_tree "${ROOT}" "${RUN_ID}"; then
      info "aufgeräumt: ${ROOT}"
    else
      rc=1
    fi
  fi
  if ((CREATED_POOL)); then
    if destroy_file_pool "${FILE_POOL}" "${FILE_PATH}"; then
      info "aufgeräumt: Pool ${FILE_POOL}"
    else
      rc=1
    fi
  fi
  exit "${rc}"
}

# --- Voraussetzungen ---------------------------------------------------------------------

need_root() {
  if ((EUID != 0)); then
    stop "muss als root laufen"
  fi
  if ! command -v zfs >/dev/null || ! command -v zpool >/dev/null; then
    stop "zfs und zpool fehlen"
  fi
}

new_run_id() {
  local hex
  hex="$(od -An -N4 -tx1 /dev/urandom)"
  RUN_ID="${hex//[[:space:]]/}"
}

mem_available() { awk '$1 == "MemAvailable:" { printf "%d\n", $2 * 1024 }' /proc/meminfo; }
arc_size() { awk '$1 == "size" { print $3 }' /proc/spl/kstat/zfs/arcstats; }

prepare_target() {
  local available
  zpool list -H -o name -- "${POOL}" >/dev/null 2>&1 || stop "Pool ${POOL} fehlt"
  if [[ "$(zfs get -H -o value -- type "${MIRROR_ROOT}" 2>/dev/null)" != filesystem ]]; then
    stop "${MIRROR_ROOT} fehlt; zuerst den Schritt Groß prüfen ausführen (Share twinraid-mirror anlegen)"
  fi
  if zfs list -H -o name -- "${TARGET_ROOT}" >/dev/null 2>&1; then
    zfs list -H -t all -o name -r -- "${TARGET_ROOT}" >&2
    stop "${TARGET_ROOT} gibt es schon (Rest eines früheren Laufs?); entfernen mit: zfs-check.sh cleanup target"
  fi
  available="$(prop available "${MIRROR_ROOT}")"
  if ((available < 2 * GIB)); then
    stop "auf ${MIRROR_ROOT} sind weniger als 2 GiB frei"
  fi
  ROOT="${TARGET_ROOT}"
  CREATED_ROOT=1
  zfs create -o "${PROPERTY}=${RUN_ID}" -o canmount=on -- "${ROOT}" >/dev/null
}

prepare_source() {
  local available tmp_free leftovers
  available="$(mem_available)"
  tmp_free="$(df --output=avail -B1 /tmp | tail -n 1)"
  if ((available < 3 * GIB / 2)); then
    stop "weniger als 1,5 GiB Arbeitsspeicher frei"
  fi
  if ((tmp_free < 5 * GIB / 4)); then
    stop "in /tmp sind weniger als 1,25 GiB frei"
  fi
  leftovers="$(zpool list -H -o name | grep -E "^${FILE_POOL_PREFIX}" || true)"
  if [[ -n "${leftovers}" ]]; then
    printf '%s\n' "${leftovers}" >&2
    stop "Reste eines früheren Laufs; entfernen mit: zfs-check.sh cleanup source"
  fi
  FILE_POOL="${FILE_POOL_PREFIX}${RUN_ID}"
  ALTROOT="${FILE_PREFIX}${RUN_ID}"
  FILE_PATH="${ALTROOT}.img"
  mkdir -m 0700 -- "${ALTROOT}"
  truncate -s "${FILE_POOL_SIZE}" -- "${FILE_PATH}"
  CREATED_POOL=1
  zpool create -o cachefile=none -o ashift=12 -R "${ALTROOT}" \
    -O compression=lz4 -O atime=off -O xattr=sa -O acltype=posix -O "${PROPERTY}=${RUN_ID}" \
    -- "${FILE_POOL}" "${FILE_PATH}" >/dev/null
  ROOT="${FILE_POOL}/zfs-check"
  CREATED_ROOT=1
  zfs create -o "${PROPERTY}=${RUN_ID}" -- "${ROOT}" >/dev/null
}

# --- Testdaten -------------------------------------------------------------------------

fill_source() {
  local dir="$1"
  mkdir -p -- "${dir}/a/b/c" "${dir}/leer"
  head -c 300000 /dev/urandom >"${dir}/a/zufall.bin"
  printf 'erste Fassung\n' >"${dir}/a/b/text.txt"
  printf 'Name mit Leerzeichen\n' >"${dir}/a/mit leerzeichen.txt"
  printf 'bleibt\n' >"${dir}/a/b/c/tief.txt"
  printf 'wird gelöscht\n' >"${dir}/loeschen-1.txt"
  printf 'wird gelöscht\n' >"${dir}/loeschen-2.txt"
  printf 'wird umbenannt\n' >"${dir}/alt.txt"
  ln -- "${dir}/a/b/text.txt" "${dir}/a/hardlink.txt"
  ln -s -- b/c/tief.txt "${dir}/a/symlink"
  chmod 0640 -- "${dir}/a/b/text.txt"
  chmod 0751 -- "${dir}/a/b"
  touch -d '2020-01-02 03:04:05' -- "${dir}/a/b/c/tief.txt"
  if ! command -v setfattr >/dev/null; then
    info "setfattr fehlt; xattrs werden nicht geprüft"
  elif setfattr -n user.twinraid -v test -- "${dir}/a/zufall.bin"; then
    XATTR_SET=1
  else
    info "setfattr scheitert; xattrs werden nicht geprüft"
  fi
  if ! command -v setfacl >/dev/null; then
    info "setfacl fehlt; ACLs werden nicht geprüft"
  elif setfacl -m u:nobody:r -- "${dir}/a/b/c/tief.txt"; then
    ACL_SET=1
  else
    info "setfacl scheitert; ACLs werden nicht geprüft"
  fi
}

change_source() {
  local dir="$1"
  rm -f -- "${dir}/loeschen-1.txt" "${dir}/loeschen-2.txt"
  mv -- "${dir}/alt.txt" "${dir}/neu.txt"
  printf 'zweite Fassung\n' >>"${dir}/a/b/text.txt"
  printf 'neu\n' >"${dir}/a/neu.txt"
}

# zfs diff zählt: gelöscht (-), neu (+), geändert (M), umbenannt (R). Setzt D_* .
count_diff() {
  local line
  D_REMOVED=0
  D_ADDED=0
  D_MODIFIED=0
  D_RENAMED=0
  while IFS= read -r line; do
    case "${line:0:1}" in
      -) D_REMOVED=$((D_REMOVED + 1)) ;;
      +) D_ADDED=$((D_ADDED + 1)) ;;
      M) D_MODIFIED=$((D_MODIFIED + 1)) ;;
      R) D_RENAMED=$((D_RENAMED + 1)) ;;
    esac
  done < <(zfs diff -H -- "$1" "$2" 2>/dev/null)
}

attr_checks() {
  local src_dir dst_dir
  src_dir="$(mountpoint_of "$1")"
  dst_dir="$(mountpoint_of "$2")"
  if ((XATTR_SET)) && command -v getfattr >/dev/null; then
    if [[ "$(getfattr --only-values -n user.twinraid -- "${dst_dir}/a/zufall.bin" 2>/dev/null)" == test ]]; then
      ok "xattr kommt an"
    else
      bad "xattr kommt nicht an"
    fi
  fi
  if ((ACL_SET)) && command -v getfacl >/dev/null; then
    if [[ "$(getfacl -cp -- "${src_dir}/a/b/c/tief.txt" 2>/dev/null)" == \
      "$(getfacl -cp -- "${dst_dir}/a/b/c/tief.txt" 2>/dev/null)" ]]; then
      ok "ACL kommt an"
    else
      bad "ACL kommt nicht an"
    fi
  fi
}

# --- Fälle -------------------------------------------------------------------------------

run_cases() {
  local src="${ROOT}/src" extra="${ROOT}/extra" dst="${ROOT}/dst" clone="${ROOT}/clone"
  local src_dir dst_dir guid_a txg_src txg_extra size_line before mp

  # Wie die Shares auf Klein (Konzept 3.6 und 3.7).
  zfs create -o "${PROPERTY}=${RUN_ID}" -o compression=lz4 -o xattr=sa -o acltype=posix \
    -o atime=off -- "${src}" >/dev/null
  zfs create -o "${PROPERTY}=${RUN_ID}" -- "${extra}" >/dev/null
  src_dir="$(mountpoint_of "${src}")"
  fill_source "${src_dir}"

  echo "== Snapshot und erster Empfang"
  expect_success "Snapshot zweier Datasets in einem Aufruf" zfs snapshot -- "${src}@a" "${extra}@a"
  txg_src="$(prop createtxg "${src}@a" 2>/dev/null || echo x)"
  txg_extra="$(prop createtxg "${extra}@a" 2>/dev/null || echo y)"
  if [[ "${txg_src}" == "${txg_extra}" ]]; then ok "beide Snapshots aus derselben Transaktion"; else
    bad "Snapshots aus verschiedenen Transaktionen"
  fi
  if send_receive "${dst}" "${src}@a" >/dev/null 2>&1; then ok "vollständiger Empfang"; else bad "vollständiger Empfang"; fi
  check_mirror_properties "${dst}" "erster Empfang"
  if same_content "${src}" "${dst}"; then
    ok "Inhalt, Rechte, Links und Zeiten gleich"
  else
    bad "Inhalt weicht ab"
  fi
  attr_checks "${src}" "${dst}"
  dst_dir="$(mountpoint_of "${dst}")"
  if touch -- "${dst_dir}/neu" 2>/dev/null; then bad "Spiegel ist beschreibbar"; else ok "Spiegel ist schreibgeschützt"; fi
  guid_a="$(prop guid "${dst}@a")"

  echo "== Inkrementell, zfs diff, Größe"
  change_source "${src_dir}"
  zfs snapshot -- "${src}@b"
  count_diff "${src}@a" "${src}@b"
  if ((D_REMOVED == 2 && D_RENAMED == 1 && D_ADDED >= 1 && D_MODIFIED >= 1)); then
    ok "zfs diff: ${D_REMOVED} gelöscht, ${D_ADDED} neu, ${D_MODIFIED} geändert, ${D_RENAMED} umbenannt"
  else
    bad "zfs diff: ${D_REMOVED} gelöscht, ${D_ADDED} neu, ${D_MODIFIED} geändert, ${D_RENAMED} umbenannt"
  fi
  size_line="$(zfs send -L -c -e -n -v -P -i "${src}@a" "${src}@b" 2>&1 | grep -E '^size[[:space:]]+[0-9]+$' || true)"
  if [[ -n "${size_line}" ]]; then ok "Größenschätzung: ${size_line//[[:space:]]/ } Byte"; else bad "keine Zeile size bei zfs send -nvP"; fi
  cat -- "${dst_dir}/a/b/text.txt" >/dev/null
  if send_receive "${dst}" -i "${src}@a" "${src}@b" >/dev/null 2>&1; then
    ok "inkrementeller Empfang (nach Lesen im Spiegel)"
  else
    bad "inkrementeller Empfang"
  fi
  if [[ "$(prop guid "${dst}@a")" == "${guid_a}" ]]; then ok "älterer Snapshot unverändert (GUID)"; else bad "GUID von @a geändert"; fi
  if same_content "${src}" "${dst}"; then ok "Inhalt nach dem Delta gleich"; else bad "Inhalt nach dem Delta weicht ab"; fi

  echo "== Abweichende Eigenschaft wird korrigiert"
  zfs set -- exec=on "${dst}"
  zfs snapshot -- "${src}@c"
  send_receive "${dst}" -i "${src}@b" "${src}@c" >/dev/null 2>&1 || true
  check_mirror_properties "${dst}" "nach Empfang mit vorher exec=on"

  echo "== Ohne -F"
  before="$(snapshots_of "${dst}")"
  zfs set -- readonly=off "${dst}"
  printf 'verändert\n' >"${dst_dir}/von-hand.txt"
  zfs set -- readonly=on "${dst}"
  zfs snapshot -- "${src}@d"
  expect_failure "verändertes Ziel: Empfang scheitert" send_receive "${dst}" -i "${src}@c" "${src}@d"
  if [[ "$(snapshots_of "${dst}")" == "${before}" ]]; then ok "Spiegel dabei unverändert"; else bad "Spiegel verändert"; fi
  zfs rollback -- "${dst}@c"
  expect_success "nach Rollback klappt der Empfang" send_receive "${dst}" -i "${src}@c" "${src}@d"
  before="$(snapshots_of "${dst}")"
  expect_failure "vollständiger Strom in vorhandenes Dataset scheitert" send_receive "${dst}" "${src}@a"
  if [[ "$(snapshots_of "${dst}")" == "${before}" ]]; then ok "Spiegel dabei unverändert"; else bad "Spiegel verändert"; fi
  zfs snapshot -- "${dst}@e" "${src}@e"
  expect_failure "Snapshot-Name am Ziel schon vorhanden: Empfang scheitert" \
    send_receive "${dst}" -i "${src}@d" "${src}@e"
  zfs destroy -- "${dst}@e"
  expect_success "danach klappt der Empfang" send_receive "${dst}" -i "${src}@d" "${src}@e"

  zfs snapshot -- "${src}@f"
  expect_success "Empfang von @f" send_receive "${dst}" -i "${src}@e" "${src}@f"

  echo "== Lesezeichen und Hold"
  expect_success "Lesezeichen anlegen" zfs bookmark -- "${src}@f" "${src}#f"
  expect_success "Basis-Snapshot löschen" zfs destroy -- "${src}@f"
  printf 'nach dem Lesezeichen\n' >"${src_dir}/lesezeichen.txt"
  zfs snapshot -- "${src}@g"
  expect_success "inkrementell vom Lesezeichen" send_receive "${dst}" -i "${src}#f" "${src}@g"
  expect_success "Hold setzen" zfs hold -- twinraid-base "${src}@g"
  expect_failure "Snapshot mit Hold lässt sich nicht löschen" zfs destroy -- "${src}@g"
  expect_success "Hold lösen" zfs release -- twinraid-base "${src}@g"

  echo "== Klon für den Notbetrieb"
  if zfs clone "${CLONE_OPTIONS[@]}" -o "${PROPERTY}=${RUN_ID}" -- "${dst}@g" "${clone}" >/dev/null 2>&1; then
    ok "Klon angelegt"
    mp="$(mountpoint_of "${clone}")"
    if printf 'x\n' >"${mp}/notbetrieb.txt" 2>/dev/null; then ok "Klon ist beschreibbar"; else bad "Klon ist nicht beschreibbar"; fi
    if [[ "$(prop exec "${clone}")" == on && "$(prop_source exec "${clone}")" == local &&
      "$(prop primarycache "${clone}")" == all && "$(prop setuid "${clone}")" == off &&
      "$(prop acltype "${clone}")" == posix && "$(prop_source acltype "${clone}")" == local ]]; then
      ok "Eigenschaften des Klons ausdrücklich gesetzt"
    else
      bad "Eigenschaften des Klons"
    fi
    zfs snapshot -- "${src}@h"
    expect_success "Empfang in den Spiegel trotz Klon (die Sperre ist Sache von guard.sh)" \
      send_receive "${dst}" -i "${src}@g" "${src}@h"
    expect_success "Klon wieder löschen" zfs destroy -- "${clone}"
  else
    bad "Klon angelegt"
  fi

  check_received_rules "${dst}"
  check_each_option "${src}" "${src}@h"
  check_hostile_stream
  check_x_finding
}

# Prüfung vor dem Einhängen bei normalen Strömen und die Regel "nur Quelle local".
check_received_rules() {
  local dst="$1" candidates
  echo "== Prüfung vor dem Einhängen, Aufbewahrung"
  if [[ -z "$(received_properties "${dst}")" ]]; then
    ok "normale Ströme bringen keine Eigenschaft mit Quelle received"
  else
    bad "normale Ströme bringen Eigenschaften mit: $(received_properties "${dst}" | cut -f2 | tr '\n' ' ')"
  fi
  if [[ "$(findmnt -n -o TARGET -S "${dst}" 2>/dev/null)" == "$(value_of mountpoint "${dst}")" ]]; then
    ok "Spiegel hängt genau am festen Ort"
  else
    bad "Spiegel hängt nicht am festen Ort"
  fi
  zfs set -- twinraid:received=1000000000 "${dst}@a"
  candidates="$(retention_candidates "${dst}" 30)"
  if [[ "${candidates}" == "${dst}@a" ]]; then
    ok "Aufbewahrung: nur der Snapshot mit altem twinraid:received (Quelle local)"
  else
    bad "Aufbewahrung wählt: ${candidates//$'\n'/ }"
  fi
  zfs inherit -- twinraid:received "${dst}@a"
}

# Nimmt guard.sh jedes -o auch beim inkrementellen Empfang an? Jede Option einzeln.
check_each_option() {
  local src="$1" base="$2" target="${ROOT}/einzeln" i n=0 pair previous="$2"
  echo "== Jedes -o einzeln beim inkrementellen Empfang"
  if ! send_receive "${target}" "${base}" >/dev/null 2>&1; then
    bad "Ausgangsstand für die Einzelprüfung"
    return 0
  fi
  mirror_mountpoint "${target}"
  for ((i = 0; i < ${#RECEIVE_OPTIONS[@]}; i++)); do
    if [[ "${RECEIVE_OPTIONS[i]}" != -o ]]; then
      continue
    fi
    pair="${RECEIVE_OPTIONS[i + 1]}"
    n=$((n + 1))
    printf 'Option %d\n' "${n}" >>"$(mountpoint_of "${src}")/einzeln.txt"
    zfs snapshot -- "${src}@o${n}"
    if zfs send -L -c -e -i "${previous}" "${src}@o${n}" |
      zfs receive -u -o "${pair}" -- "${target}" >/dev/null 2>&1; then
      ok "-o ${pair} inkrementell angenommen"
    else
      bad "-o ${pair} inkrementell nicht angenommen"
    fi
    previous="${src}@o${n}"
  done
  n=$((n + 1))
  zfs snapshot -- "${src}@o${n}"
  if zfs send -L -c -e -i "${previous}" "${src}@o${n}" |
    zfs receive -u -o "mountpoint=${MOUNT_VALUE}" -- "${target}" >/dev/null 2>&1; then
    ok "-o mountpoint=<fester Ort> inkrementell angenommen"
  else
    bad "-o mountpoint=<fester Ort> inkrementell nicht angenommen"
  fi
}

# Ein absichtlich böser Strom (mit -p und -I), wie ihn ein übernommenes Klein schicken
# könnte: nichts darf woanders hängen, kein Wert darf wirken, die Aufbewahrung darf
# nichts davon vorzeitig löschen (Konzept 3.7).
check_hostile_stream() {
  local evil="${ROOT}/boese-quelle" mirror="${ROOT}/boese" full="${ROOT}/boese-voll"
  local elsewhere rc dir
  echo "== Absichtlich böser Strom"
  elsewhere="$(mountpoint_of "${ROOT}")/woanders"
  zfs create -o "${PROPERTY}=${RUN_ID}" -- "${evil}" >/dev/null
  dir="$(mountpoint_of "${evil}")"
  printf 'Basis\n' >"${dir}/basis.txt"
  zfs snapshot -- "${evil}@basis"
  expect_success "Spiegel für den bösen Strom angelegt" send_receive "${mirror}" "${evil}@basis"
  printf 'Mitte\n' >"${dir}/mitte.txt"
  zfs snapshot -- "${evil}@mitte"
  zfs set -- twinraid:received=1000000000 "${evil}@mitte"
  printf 'x1\n' >"${dir}/x1.txt"
  zfs snapshot -- "${evil}@x1"
  printf 'x2\n' >"${dir}/x2.txt"
  zfs snapshot -- "${evil}@x2"
  zfs set -- canmount=noauto "${evil}"
  zfs unmount -- "${evil}"
  zfs set -- "mountpoint=${elsewhere#"${ALTROOT}"}" quota=1G refquota=1G reservation=1M \
    twinraid:mirror=0 "${evil}"
  # Auf Groß (Unraid, Modus target) kein sharesmb: ZFS würde sonst an Unraid vorbei eine
  # Samba-Freigabe versuchen. In der CI und auf Klein (eigener Datei-Pool) bleibt es.
  if [[ "${MODE}" == target && -e /etc/unraid-version ]]; then
    info "sharesmb auf Groß nicht im bösen Strom (keine Samba-Freigabe an Unraid vorbei)"
  elif ! zfs set -- sharesmb=on "${evil}" >/dev/null 2>&1; then
    info "sharesmb ließ sich an der Quelle nicht setzen; nicht im bösen Strom"
  fi
  zfs snapshot -- "${evil}@boese"
  rc=0
  send_receive "${mirror}" -p -I "${evil}@basis" "${evil}@boese" >/dev/null 2>&1 || rc=$?
  if ((rc == 2)); then
    ok "-p -I: Prüfung erkennt Eigenschaften aus dem Strom ($(printf '%s\n' "${RECEIVED_FOUND}" | grep -c .) Stück)"
  else
    bad "-p -I: Prüfung schlug nicht an (Code ${rc})"
  fi
  if [[ "$(value_of mounted "${mirror}")" == no ]]; then ok "-p -I: Spiegel bleibt ausgehängt"; else
    bad "-p -I: Spiegel ist eingehängt"
  fi
  if [[ -z "$(findmnt -n -M "${elsewhere}" 2>/dev/null)" ]]; then ok "-p -I: nichts hängt am Ort aus dem Strom"; else
    bad "-p -I: etwas hängt am Ort aus dem Strom"
  fi
  check_mirror_properties "${mirror}" "-p -I: feste Werte wirken, keiner aus dem Strom" gesperrt
  check_lock "${mirror}" "-p -I"
  if [[ "$(prop_source twinraid:received "${mirror}@mitte")" == received ]]; then
    ok "-p -I: altes twinraid:received am Zwischen-Snapshot hat Quelle received"
  else
    bad "-p -I: twinraid:received am Zwischen-Snapshot nicht mit Quelle received"
  fi
  if [[ -z "$(retention_candidates "${mirror}" 30)" ]]; then
    ok "-p -I: Aufbewahrung würde nichts davon löschen"
  else
    bad "-p -I: Aufbewahrung würde löschen: $(retention_candidates "${mirror}" 30 | tr '\n' ' ')"
  fi
  rc=0
  send_receive "${full}" -p "${evil}@boese" >/dev/null 2>&1 || rc=$?
  if ((rc == 2)) && [[ "$(value_of mounted "${full}")" == no ]]; then
    ok "-p vollständig: Prüfung erkennt Eigenschaften, Spiegel bleibt ausgehängt"
  else
    bad "-p vollständig: Code ${rc}, eingehängt: $(value_of mounted "${full}" 2>/dev/null)"
  fi
  check_mirror_properties "${full}" "-p vollständig: feste Werte wirken, keiner aus dem Strom" gesperrt
  check_lock "${full}" "-p vollständig"
  if [[ -z "$(findmnt -n -M "${elsewhere}" 2>/dev/null)" ]]; then ok "-p vollständig: nichts hängt am Ort aus dem Strom"; else
    bad "-p vollständig: etwas hängt am Ort aus dem Strom"
  fi
  check_reset "${evil}" "${mirror}"
}

# Sperre nach Eigenschaften aus dem Strom: canmount=noauto mit Quelle local.
check_lock() {
  if [[ "$(value_of canmount "$1")" == noauto && "$(prop_source canmount "$1")" == local ]]; then
    ok "$2: gesperrt mit canmount=noauto (bleibt auch nach einem Neustart ausgehängt)"
  else
    bad "$2: nicht gesperrt (canmount $(value_of canmount "$1"))"
  fi
}

# Eigenschaft aus der festen Empfangszeile (alle -o und der feste Ort)?
is_fixed_property() {
  local i
  if [[ "$1" == mountpoint ]]; then
    return 0
  fi
  for ((i = 0; i < ${#RECEIVE_OPTIONS[@]}; i++)); do
    if [[ "${RECEIVE_OPTIONS[i]}" == -o && "${RECEIVE_OPTIONS[i + 1]%%=*}" == "$1" ]]; then
      return 0
    fi
  done
  return 1
}

# Zurücksetzen nach props-mismatch wie in Konzept 3.7: verdächtige Snapshots einzeln löschen,
# zurück zum letzten guten, Eigenschaften mit Quelle received zurücksetzen, Werte prüfen,
# canmount=on; danach geht es inkrementell von der Basis weiter.
check_reset() {
  local evil="$1" mirror="$2" snap name property rc
  echo "== Zurücksetzen nach props-mismatch"
  for snap in boese x2 x1 mitte; do
    zfs destroy -- "${mirror}@${snap}" >/dev/null 2>&1 || bad "Zurücksetzen: ${snap} nicht gelöscht"
  done
  expect_success "Zurücksetzen: Rollback auf den letzten guten Snapshot" zfs rollback -- "${mirror}@basis"
  # Nur genau die gemeldeten Eigenschaften, nie eine feste der Empfangszeile (sonst fiele
  # etwa readonly=on weg); eine feste mit Quelle received wäre selbst schon ein Fehler.
  while IFS=$'\t' read -r name property; do
    if [[ -z "${property}" ]]; then
      continue
    fi
    if is_fixed_property "${property}"; then
      bad "Zurücksetzen: feste Eigenschaft ${property} mit Quelle received, nicht zurückgesetzt"
      continue
    fi
    zfs inherit -- "${property}" "${name}" >/dev/null 2>&1 || bad "Zurücksetzen: ${property} nicht zurückgesetzt"
  done < <(received_properties "${mirror}")
  if [[ -z "$(received_properties "${mirror}")" ]]; then
    ok "Zurücksetzen: keine Eigenschaft mehr mit Quelle received"
  else
    bad "Zurücksetzen: noch mit Quelle received: $(received_properties "${mirror}" | cut -f2 | tr '\n' ' ')"
  fi
  zfs set -- canmount=on "${mirror}"
  check_mirror_properties "${mirror}" "Zurücksetzen: feste Werte"
  zfs snapshot -- "${evil}@gut"
  rc=0
  send_receive "${mirror}" -i "${evil}@basis" "${evil}@gut" >/dev/null 2>&1 || rc=$?
  if ((rc == 0)); then
    ok "Zurücksetzen: Empfang von der Basis klappt, Spiegel hängt am festen Ort"
  else
    bad "Zurücksetzen: Empfang von der Basis scheitert (Code ${rc})"
  fi
}

# Befund, ob -x auf diesem Server wirkt (die frühere Befehlszeile), mit vollständigem und
# inkrementellem Strom. Ohne sharesmb und mit mountpoint=legacy, damit dabei nichts
# eingehängt oder freigegeben wird.
check_x_finding() {
  local evil="${ROOT}/x-quelle" target="${ROOT}/mit-x"
  echo "== Befund: wirkt -x?"
  zfs create -o "${PROPERTY}=${RUN_ID}" -- "${evil}" >/dev/null
  zfs snapshot -- "${evil}@x0"
  zfs set -- quota=1G mountpoint=legacy "${evil}"
  zfs snapshot -- "${evil}@x1"
  if zfs send -L -c -e -p "${evil}@x1" | zfs receive -u "${X_RECEIVE_OPTIONS[@]}" -- "${target}" >/dev/null 2>&1; then
    report_x vollständig "${target}"
  else
    info "Befund -x vollständig: Empfang gescheitert"
  fi
  zfs set -- quota=2G "${evil}"
  zfs snapshot -- "${evil}@x2"
  if zfs send -L -c -e -p -i "${evil}@x1" "${evil}@x2" |
    zfs receive -u "${X_RECEIVE_OPTIONS[@]}" -- "${target}" >/dev/null 2>&1; then
    report_x inkrementell "${target}"
  else
    info "Befund -x inkrementell: Empfang gescheitert"
  fi
}

report_x() {
  local kind="$1" target="$2" name value source
  for name in quota mountpoint; do
    value="$(value_of "${name}" "${target}" | tr -cd 'A-Za-z0-9/._-')"
    source="$(prop_source "${name}" "${target}" | cut -d' ' -f1)"
    if [[ "${source}" == received ]]; then
      info "Befund -x ${kind}: ${name} kam aus dem Strom (${value}); -x wirkt nicht"
    else
      info "Befund -x ${kind}: ${name} nicht aus dem Strom (Quelle ${source}); -x wirkt"
    fi
  done
}

# Trägt receive:append unter Linux als zweite Sperre? Nur auf eigenem Dataset unter
# ROOT, mit einem Benutzer ohne Rechte; die Rechte gelten nur auf ROOT und werden am
# Ende entfernt. Ein Ergebnis in jede Richtung ist kein FEHLER, sondern ein Befund.
check_receive_append() {
  local src="${ROOT}/src" deleg="${ROOT}/delegiert" uid gid user=nobody incremental full reason
  echo "== receive:append"
  if ! command -v setpriv >/dev/null; then
    info "receive:append: setpriv fehlt; nicht geprüft"
    return 0
  fi
  uid="$(id -u "${user}" 2>/dev/null)" || {
    info "receive:append: Benutzer nobody fehlt; nicht geprüft"
    return 0
  }
  gid="$(id -g "${user}")"
  send_receive "${deleg}" "${src}@a" >/dev/null 2>&1 || {
    bad "receive:append: Test-Dataset nicht angelegt"
    return 0
  }
  zfs set -- "${PROPERTY}=${RUN_ID}" "${deleg}"
  # receive:append braucht laut zfs-allow(8) zusätzlich mount und create.
  if ! zfs allow -u "${user}" -- receive:append,mount,create "${ROOT}" >/dev/null 2>&1; then
    info "receive:append: in dieser Version von OpenZFS nicht verfügbar"
    return 0
  fi
  ALLOWED_USER="${user}"
  incremental="$(mktemp)"
  full="$(mktemp)"
  zfs send -L -c -e -i "${src}@a" "${src}@b" >"${incremental}"
  zfs send -L -c -e "${src}@a" >"${full}"
  if reason="$(setpriv --reuid "${uid}" --regid "${gid}" --clear-groups -- \
    zfs receive -u -- "${deleg}" <"${incremental}" 2>&1)"; then
    info "receive:append: inkrementeller Empfang als Benutzer ohne root klappt"
  else
    reason="${reason%%$'\n'*}"
    info "receive:append: inkrementeller Empfang als Benutzer ohne root scheitert (${reason//[^A-Za-z0-9 :._\/@-]/})"
  fi
  # Bewusst -F, als Benutzer ohne root und nur auf das eigene Test-Dataset: genau das
  # soll receive:append verhindern (Konzept 3.7). Es ist die einzige Stelle mit -F.
  if setpriv --reuid "${uid}" --regid "${gid}" --clear-groups -- \
    zfs receive -u -F -- "${deleg}" <"${full}" >/dev/null 2>&1; then
    info "receive:append: Empfang mit -F wurde NICHT verhindert; trägt nicht als zweite Sperre"
  else
    info "receive:append: Empfang mit -F wurde verhindert"
  fi
  if setpriv --reuid "${uid}" --regid "${gid}" --clear-groups -- \
    zfs destroy -- "${deleg}@a" >/dev/null 2>&1; then
    info "receive:append: Löschen eines Snapshots wurde NICHT verhindert"
  else
    info "receive:append: Löschen eines Snapshots wurde verhindert"
  fi
  rm -f -- "${incremental}" "${full}"
  if zfs unallow -u "${user}" -- receive:append,mount,create "${ROOT}" >/dev/null 2>&1; then
    ok "receive:append: Recht wieder entfernt"
    ALLOWED_USER=""
  else
    bad "receive:append: Recht nicht entfernt"
  fi
}

# Enthält MemAvailable den ARC? Füllt den ARC mit 512 MiB Zufallsdaten (lz4 kann sie
# nicht verkleinern) und vergleicht, um wie viel MemAvailable sinkt.
measure_memory() {
  local dataset="${ROOT}/speicher" dir m0 a0 m1 a1 m2 a2 grown dropped percent running=""
  echo "== MemAvailable und ARC"
  if [[ ! -r /proc/spl/kstat/zfs/arcstats ]]; then
    info "Messung: arcstats fehlt; nicht gemessen"
    return 0
  fi
  if command -v virsh >/dev/null; then
    running="$(timeout 10 virsh list --name --state-running 2>/dev/null | grep -c . || true)"
    if [[ "${running}" != 0 ]]; then
      info "Messung: es laufen ${running} VMs; das Ergebnis ist ungenau (VMs vorher ausschalten)"
    fi
  fi
  zfs create -o "${PROPERTY}=${RUN_ID}" -o primarycache=all -o compression=off -- "${dataset}" >/dev/null
  dir="$(mountpoint_of "${dataset}")"
  sync
  sleep 2
  m0="$(mem_available)"
  a0="$(arc_size)"
  head -c "${MEASURE_BYTES}" /dev/urandom >"${dir}/zufall.bin"
  sync
  cat -- "${dir}/zufall.bin" >/dev/null
  cat -- "${dir}/zufall.bin" >/dev/null
  sleep 2
  m1="$(mem_available)"
  a1="$(arc_size)"
  grown=$((a1 - a0))
  dropped=$((m0 - m1))
  info "Messung: ARC $((a0 / 1048576)) → $((a1 / 1048576)) MiB, MemAvailable $((m0 / 1048576)) → $((m1 / 1048576)) MiB"
  if ((grown < 128 * 1048576)); then
    info "Messung: ARC wuchs nur um $((grown / 1048576)) MiB; nicht aussagekräftig (ARC-Grenze erreicht?)"
  else
    percent=$((dropped * 100 / grown))
    if ((percent >= 80)); then
      info "Messung: MemAvailable sank um ${percent} % des ARC-Wachstums: enthält den ARC nicht"
    elif ((percent <= 20)); then
      info "Messung: MemAvailable sank um ${percent} % des ARC-Wachstums: enthält den ARC schon"
    else
      info "Messung: MemAvailable sank um ${percent} % des ARC-Wachstums: enthält den ARC teilweise"
    fi
  fi
  zfs destroy -- "${dataset}" >/dev/null 2>&1 || true
  sleep 2
  m2="$(mem_available)"
  a2="$(arc_size)"
  info "Messung: nach dem Löschen ARC $((a2 / 1048576)) MiB, MemAvailable $((m2 / 1048576)) MiB"
}

cleanup_command() {
  local which="$1" pool file rc=0
  need_root
  case "${which}" in
    target)
      if ! zfs list -H -o name -- "${TARGET_ROOT}" >/dev/null 2>&1; then
        echo "Keine Reste in ${TARGET_ROOT}."
        return 0
      fi
      remove_tree "${TARGET_ROOT}" || rc=1
      ;;
    source)
      while IFS= read -r pool; do
        if [[ "${pool}" =~ ^${FILE_POOL_PREFIX}[0-9a-f]{8}$ ]]; then
          file="${FILE_PREFIX}${pool#"${FILE_POOL_PREFIX}"}.img"
          destroy_file_pool "${pool}" "${file}" || rc=1
        fi
      done < <(zpool list -H -o name 2>/dev/null || true)
      # Dateien ohne importierten Pool, etwa nach einem Neustart.
      for file in "${FILE_PREFIX}"*.img; do
        if [[ -f "${file}" && "${file}" =~ ^${FILE_PREFIX}[0-9a-f]{8}\.img$ ]]; then
          pool="${FILE_POOL_PREFIX}${file#"${FILE_PREFIX}"}"
          destroy_file_pool "${pool%.img}" "${file}" || rc=1
        fi
      done
      ;;
    *)
      usage
      ;;
  esac
  if ((rc == 0)); then
    echo "Aufgeräumt."
  fi
  return "${rc}"
}

usage() {
  cat >&2 <<'EOF'
Aufruf: zfs-check.sh target | source | cleanup target | cleanup source
EOF
  exit 2
}

MODE="${1:-}"
case "${MODE}" in
  target | source)
    [[ $# -eq 1 ]] || usage
    need_root
    new_run_id
    trap cleanup EXIT
    trap 'exit 130' INT TERM HUP
    printf 'zfs-check %s, Lauf %s\n' "${MODE}" "${RUN_ID}"
    info "$(zfs version 2>/dev/null | tr '\n' ' ' | tr -cd 'A-Za-z0-9 ._-')"
    if [[ "${MODE}" == target ]]; then
      prepare_target
    else
      prepare_source
    fi
    run_cases
    check_receive_append
    if [[ "${MODE}" == target ]]; then
      measure_memory
    fi
    FINISHED=1
    echo "== Ergebnis"
    if ((failures > 0)); then
      printf '%d FEHLER\n' "${failures}"
      exit 1
    fi
    echo "Alle Prüfungen in Ordnung."
    ;;
  cleanup)
    [[ $# -eq 2 ]] || usage
    cleanup_command "$2"
    ;;
  *)
    usage
    ;;
esac
