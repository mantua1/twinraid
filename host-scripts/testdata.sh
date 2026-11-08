#!/bin/bash
# TwinRaid: Testdaten für die Test-Share (docs/EINRICHTUNG.md, Abschnitt 15, ab v0.5.1).
#
#   testdata.sh create [Ordner]   legt die Testdaten mit allen Sonderfällen und die Prüfliste an
#   testdata.sh change [Ordner]   ändert sie auf bekannte Weise und schreibt die Prüfliste neu
#   testdata.sh verify [Ordner]   vergleicht den Ordner mit der Prüfliste, ändert nichts
#   testdata.sh verify-latest [Dataset]
#                                 verify im neuesten Snapshot des Datasets (ab v0.5.2)
#   testdata.sh verify-container [Dataset|Ordner]
#                                 war der Testcontainer beim Snapshot sauber angehalten? (ab v0.5.2)
#   testdata.sh report source|target [Name]
#                                 prüft die Standardpunkte der Servertests auf Klein (source)
#                                 bzw. Groß (target), ändert nichts (ab v0.5.3)
#   testdata.sh bulk <GiB> [Ordner]
#                                 legt 1 bis 500 GiB Zufallsdaten für Tests mit großen
#                                 Mengen an (ab v0.5.3)
#
# Ohne Ordner gilt /mnt/mainpool/twinraidtest/testdata. create und change schreiben nur in
# einen Ordner namens testdata, und change nur, wenn dort die Kennung von create liegt.
# verify liest nur; es läuft deshalb auch in einem Snapshot, auf Groß etwa unter
# /mnt/mainpool/twinraid-mirror/twinraidtest/.zfs/snapshot/<Name>/testdata. Den eingehängten
# Spiegel selbst prüft es nicht: Dort kann der Kernel nach einem inkrementellen Empfang noch
# eine alte ACL zeigen, obwohl sie richtig angekommen ist (Konzept 3.7). verify-latest und
# verify-container nehmen ohne Angabe den Spiegel mainpool/twinraid-mirror/twinraidtest.
#
# report nimmt den Namen der Share (ohne Angabe twinraidtest): auf Klein das Dataset
# mainpool/<Name>, auf Groß den Spiegel mainpool/twinraid-mirror/<Name>. Es gibt je Punkt eine
# Zeile OK, ABWEICHUNG oder INFO aus, nur mit festen Texten, Zahlen, Snapshot-Namen und dem
# Namen aus dem Aufruf; Zeilen aus dem Syslog gibt es nie aus (dort stehen auch die UUID der
# VM und Container-Namen). bulk schreibt nur nach /mnt/mainpool/twinraidtest/kopie oder
# …/kopie2 (ohne Angabe kopie), nur in einen neuen Ordner und nur, wenn danach noch 20 % von
# mainpool frei sind.
#
# Die Prüfliste hält je Eintrag Art, Rechte, Besitzer, Zeitstempel, Größe, Hardlinks, SHA-256
# und bei den Sonderfällen xattrs und ACLs fest. Was ein Dateisystem nicht kann, vermerkt sie
# in ihrem Kopf (etwa acl no), und verify lässt es dann aus.
#
# Nur Bordmittel von Unraid. Der Inhalt der Testdaten ist erfunden; keine privaten Angaben.
set -euo pipefail
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 022

readonly DEFAULT_DIR=/mnt/mainpool/twinraidtest/testdata
readonly MARKER=.twinraid-testdata
readonly LIST=PRUEFLISTE.txt
readonly LIST_VERSION=1
# Umfang: viele kleine Dateien in mehreren Ordnern und einige große.
readonly SMALL_DIRS=20
readonly SMALL_PER_DIR=100
readonly LARGE_COUNT=3
readonly LARGE_MIB=64
readonly SPARSE_MIB=256
readonly DEPTH=40
# Was change je Lauf tut (docs/EINRICHTUNG.md nennt dieselben Zahlen).
readonly CHANGE_DELETE=50
readonly CHANGE_MODIFY=20
readonly CHANGE_RENAME=10
readonly CHANGE_ADD=30
readonly TEXT_LINE="TwinRaid-Testdaten: Dieser Text ist erfunden und wiederholt sich."
# Spiegel auf Groß (ab v0.5.2): verify lehnt den eingehängten Spiegel ab, auch über /mnt/user.
readonly ZFS=zfs
readonly DEFAULT_DATASET=mainpool/twinraid-mirror/twinraidtest
readonly DATASET_PATTERN='^mainpool(/[A-Za-z0-9][A-Za-z0-9._-]{0,63}){1,2}$'
readonly SNAPSHOT_PATTERN='^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
readonly MIRROR_ROOTS=(/mnt/mainpool/twinraid-mirror /mnt/user/twinraid-mirror)
# Der Testcontainer schreibt beim Anhalten diese Zeile in testcontainer/log der Test-Share.
readonly CONTAINER_LOG=testcontainer/log
readonly CONTAINER_CLEAN="sauber beendet"
# report (ab v0.5.3): Orte und Werte wie in source-guard.sh und guard.sh.
readonly POOL=mainpool
readonly MIRROR_BASE=mainpool/twinraid-mirror
readonly MIRROR_MOUNT=/mnt/mainpool/twinraid-mirror
readonly DEFAULT_NAME=twinraidtest
readonly NAME_PATTERN='^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
readonly OWN_SNAPSHOT_PATTERN='^twinraid-[0-9]{8}T[0-9]{6}Z$'
readonly GUARD_SH=/boot/config/twinraid/guard.sh
readonly SOURCE_GUARD_CONF=/boot/config/twinraid/source-guard.conf
readonly SYSLOG=/var/log/syslog
readonly FINDMNT=findmnt
readonly CREATED_BY=twinraid:created-by
readonly HOLD_TAG=twinraid-base
readonly SNAPSHOT_LIMIT=50
readonly DEFAULT_KEEP_DAYS=7
readonly DAY=86400
# Die festen Werte der Empfangszeile von guard.sh (Konzept 3.7), dazu mountpoint; ein Test
# vergleicht die Liste mit RECEIVE_OPTIONS in guard.sh.
readonly FIXED_PROPS=(
  readonly=on exec=off setuid=off devices=off
  primarycache=metadata secondarycache=all compression=lz4 atime=off
  xattr=sa acltype=posix recordsize=128K snapdir=hidden
  sync=standard dedup=off copies=1 checksum=on
  quota=none refquota=none reservation=none refreservation=none
  canmount=on
  sharesmb=off sharenfs=off twinraid:mirror=1
)
# Ablehnungen von guard.sh sync, die einen Empfang betreffen (Konzept 3.7).
readonly SYNC_REFUSALS='^refused: (name-denied|receive-busy|mirror-root-missing|receiver-id-in-use|target-exists|delegation-mismatch|props-mismatch|props-not-applied|clone-active|space-low|too-soon|received-properties|mount-mismatch|target-modified)$'
# bulk (ab v0.5.3)
readonly BULK_ROOT=/mnt/mainpool/twinraidtest
readonly BULK_NAMES=(kopie kopie2)
readonly BULK_MAX_GIB=500
readonly BULK_FREE_PERCENT=20
readonly GIB=1073741824
readonly RANDOM_SOURCE=/dev/urandom

XATTR=no
ACL=no
OWNER=no

die() {
  echo "testdata.sh: $*" >&2
  exit 2
}

usage() {
  echo "Aufruf: testdata.sh create|change|verify [Ordner]" >&2
  echo "        testdata.sh verify-latest [Dataset] | verify-container [Dataset|Ordner]" >&2
  echo "        testdata.sh report source|target [Name] | bulk <GiB> [Ordner]" >&2
  exit 2
}

# Nur absolute Pfade ohne Sonderzeichen und ohne "..". Beim Schreiben muss der Ordner
# testdata heißen, damit ein Tippfehler keine fremden Daten trifft.
check_path() {
  local dir="$1"
  [[ "${dir}" =~ ^/[A-Za-z0-9._/-]+$ && "/${dir}/" != */../* ]] ||
    die "Der Ordner muss ein absoluter Pfad ohne Sonderzeichen sein."
}

check_writable_path() {
  check_path "$1"
  [[ "${1##*/}" == testdata ]] || die "Der Ordner muss testdata heißen."
}

# --- Testdaten anlegen ---------------------------------------------------------------------

# Schreibt eine kleine Datei: je nach Nummer 1 bis 8 KiB Text, jede Datei mit anderem Inhalt.
write_small() {
  local path="$1" number="$2" i text=""
  for ((i = 0; i < (number % 8 + 1) * 15; i++)); do
    text+="${number}.${i} ${TEXT_LINE}"$'\n'
  done
  printf '%s' "${text}" >"${path}"
}

create_small() {
  local d f number=0 folder path
  for ((d = 1; d <= SMALL_DIRS; d++)); do
    printf -v folder 'viele/ordner-%03d' "${d}"
    mkdir -p -- "${folder}"
    for ((f = 1; f <= SMALL_PER_DIR; f++)); do
      number=$((number + 1))
      printf -v path '%s/datei-%05d.txt' "${folder}" "${number}"
      write_small "${path}" "${number}"
    done
  done
}

create_large() {
  local i
  mkdir -- gross
  for ((i = 1; i <= LARGE_COUNT; i++)); do
    head -c $((LARGE_MIB * 1024 * 1024)) /dev/urandom >"gross/zufall-${i}.bin"
  done
  head -c $((LARGE_MIB * 1024 * 1024)) /dev/zero >gross/nullen.bin
  # Eine Datei mit Loch: nur am Anfang und in der Mitte stehen Daten.
  printf 'Anfang\n' >gross/loch.bin
  printf 'Mitte\n' | dd of=gross/loch.bin bs=1M seek=$((SPARSE_MIB / 2)) conv=notrunc status=none
  truncate -s "${SPARSE_MIB}M" gross/loch.bin
}

create_names() {
  local long name
  # shellcheck disable=SC2016 # dollar$HOME ist hier der Dateiname.
  local -a names=(
    'mit leerzeichen.txt' ' fuehrendes leerzeichen.txt' 'leerzeichen am ende ' 'endet mit punkt.'
    'umlaute äöüßÄÖÜ.txt' 'emoji 😀.txt' 'ñandú 日本語.txt' '-fuehrendes-minus' '--'
    'stern*frage?.txt' 'klammern [a] (b) {c}.txt' "einfach'es anfuehrungszeichen.txt"
    'doppeltes"anfuehrungszeichen.txt' 'back\slash.txt' 'dollar$HOME.txt' 'semikolon;und&.txt'
    'pipe|kleiner<groesser>.txt' $'neue\nzeile.txt' $'tab\tzeichen.txt' '.versteckt' '..punkte'
    '~tilde' '#raute' 'Gross.txt' 'gross.txt'
  )
  mkdir -- namen
  for name in "${names[@]}"; do
    printf '%s\n' "${TEXT_LINE}" >"namen/${name}"
  done
  # Nicht gültiges UTF-8: nur, wenn das Dateisystem es annimmt (utf8only ist aus).
  printf '%s\n' "${TEXT_LINE}" 2>/dev/null >$'namen/latin1-\xe4.txt' || true
  printf -v long '%255s' ''
  printf '%s\n' "${TEXT_LINE}" >"namen/${long// /n}"
}

create_deep() {
  local i folder=tief part
  printf -v part 'ebene-%054d' 0
  for ((i = 1; i <= DEPTH; i++)); do
    folder+="/${part}"
  done
  mkdir -p -- "${folder}"
  printf '%s\n' "${TEXT_LINE}" >"${folder}/ganz-unten.txt"
}

create_links() {
  mkdir -- links links/unterordner
  printf '%s\n' "${TEXT_LINE}" >links/ziel.txt
  printf '%s\n' "${TEXT_LINE}" >'links/äöü.txt'
  ln -s ziel.txt links/relativ
  ln -s /mnt/user/nicht-vorhanden/datei.txt links/absolut
  ln -s fehlt.txt links/kaputt
  ln -s unterordner links/ordner
  ln -s schleife links/schleife
  ln -s 'äöü.txt' links/umlaut
  mkdir -- hardlinks hardlinks/unterordner
  printf '%s\n' "${TEXT_LINE}" >hardlinks/a.txt
  ln hardlinks/a.txt hardlinks/b.txt
  ln hardlinks/a.txt hardlinks/unterordner/c.txt
  printf '%s\n' "${TEXT_LINE}" >hardlinks/d.txt
  ln hardlinks/d.txt hardlinks/e.txt
}

create_modes() {
  local mode
  mkdir -- rechte
  # Mit root auch 4755 und 2755 (kein Programm, nur Text) und eine Datei ohne jedes Recht.
  for mode in 0644 0640 0600 0444 0755 0664 4755 2755 0000; do
    if [[ "${mode}" == 0000 && "${OWNER}" == no ]]; then
      continue
    fi
    printf 'Rechte %s, kein Programm.\n' "${mode}" >"rechte/datei-${mode}"
    chmod "${mode}" -- "rechte/datei-${mode}"
  done
  for mode in 0750 2775 1777 0700; do
    mkdir -- "rechte/ordner-${mode}"
    chmod "${mode}" -- "rechte/ordner-${mode}"
  done
}

create_owners() {
  local owner name
  mkdir -- besitzer
  for owner in 0:0 99:100 1000:1000 12345:54321; do
    name="datei-${owner/:/-}"
    printf '%s\n' "${TEXT_LINE}" >"besitzer/${name}"
    chown "${owner}" -- "besitzer/${name}"
  done
  mkdir -- besitzer/ordner
  ln -s datei-0-0 besitzer/link
  chown -h 99:100 -- besitzer/link
  chown 1000:100 -- besitzer/ordner
}

create_times() {
  mkdir -- zeiten zeiten/ordner
  printf '%s\n' "${TEXT_LINE}" >zeiten/1970.txt
  printf '%s\n' "${TEXT_LINE}" >zeiten/2000-nanosekunden.txt
  printf '%s\n' "${TEXT_LINE}" >zeiten/2100.txt
  ln -s 2100.txt zeiten/link
  touch -m -d @0 -- zeiten/1970.txt
  touch -m -d @946684800.123456789 -- zeiten/2000-nanosekunden.txt
  touch -m -d @4102444800 -- zeiten/2100.txt
  touch -h -m -d @1000000000 -- zeiten/link
  touch -m -d @1500000000 -- zeiten/ordner
}

create_special() {
  mkdir -- besonders besonders/leerer-ordner
  : >besonders/leere-datei
  mkfifo -- besonders/pipe
}

# xattrs nur, wenn das Dateisystem sie annimmt; sonst steht xattr no in der Prüfliste.
create_xattrs() {
  mkdir -- xattr xattr/ordner
  printf '%s\n' "${TEXT_LINE}" >xattr/datei.txt
  command -v setfattr >/dev/null && command -v getfattr >/dev/null || return 0
  setfattr -n user.twinraid.text -v 'Text äöü' -- xattr/datei.txt 2>/dev/null || return 0
  setfattr -n user.twinraid.binaer -v 0x00ff10 -- xattr/datei.txt
  setfattr -n user.twinraid.ordner -v ordner -- xattr/ordner
  XATTR=yes
}

create_acls() {
  mkdir -- acl acl/ordner
  printf '%s\n' "${TEXT_LINE}" >acl/datei.txt
  command -v setfacl >/dev/null && command -v getfacl >/dev/null || return 0
  setfacl -m u:1000:r-x,g:100:rw- -- acl/datei.txt 2>/dev/null || return 0
  setfacl -m u:1000:rwx -- acl/ordner
  setfacl -d -m g:100:r-x -- acl/ordner
  ACL=yes
}

# --- Prüfliste -----------------------------------------------------------------------------

declare -A LINK_GROUPS=()

hash_text() {
  local digest
  digest="$(sha256sum)" || digest=unreadable
  printf '%s' "${digest%% *}"
}

# Eine Zeile je Eintrag: Art, Rechte, Besitzer, Zeit, Größe, Hardlinks, Gruppe, SHA-256,
# xattrs, ACL, Pfad. Die Gruppe ersetzt die Inode-Nummer, die auf jedem Server anders ist.
entry() {
  local path="$1" kind mode owner mtime size links inode group=- digest=- xattrs=- acls=- quoted info
  info="$(stat -c '%a %u:%g %.9Y %s %h %i' -- "${path}")"
  read -r mode owner mtime size links inode <<<"${info}"
  if [[ "${OWNER}" == no ]]; then
    owner=-
  fi
  if [[ -L "${path}" ]]; then
    kind=l
    digest="$(readlink -- "${path}" | hash_text)"
  elif [[ -d "${path}" ]]; then
    kind=d
    size=-
    links=-
  elif [[ -f "${path}" ]]; then
    kind=f
    digest="$(hash_text <"${path}")"
    if ((links > 1)); then
      if [[ -z "${LINK_GROUPS[${inode}]+set}" ]]; then
        LINK_GROUPS[${inode}]=$((${#LINK_GROUPS[@]} + 1))
      fi
      group="${LINK_GROUPS[${inode}]}"
    fi
  else
    kind=p
    size=-
  fi
  if [[ "${XATTR}" == yes && "${path}" == ./xattr/* && "${kind}" != l ]]; then
    xattrs="$({ getfattr --absolute-names -d -m '^user\.' -e hex -- "${path}" 2>/dev/null || true; } |
      { grep -v '^# file:' || true; } | hash_text)"
  fi
  if [[ "${ACL}" == yes && "${path}" == ./acl/* ]]; then
    acls="$({ getfacl --numeric --absolute-names --omit-header -- "${path}" 2>/dev/null || true; } | hash_text)"
  fi
  printf -v quoted '%q' "${path}"
  printf '%s %s %s %s %s %s %s %s %s %s %s\n' "${kind}" "${mode}" "${owner}" "${mtime}" "${size}" \
    "${links}" "${group}" "${digest}" "${xattrs}" "${acls}" "${quoted}"
}

entries() {
  local path
  while IFS= read -r -d '' path; do
    entry "${path}"
  done < <(find . -mindepth 1 \( -path "./${LIST}" -o -path "./${LIST}.tmp" -o -path "./${MARKER}" \) \
    -prune -o -print0 | sort -z)
}

write_list() {
  local generation="$1"
  {
    printf '# TwinRaid-Testdaten, Prüfliste Version %s\n' "${LIST_VERSION}"
    printf '# generation %s\n# xattr %s\n# acl %s\n# owner %s\n' "${generation}" "${XATTR}" "${ACL}" "${OWNER}"
    entries
  } >"${LIST}.tmp"
  mv -f -- "${LIST}.tmp" "${LIST}"
}

# Liest den Kopf der Prüfliste; setzt GENERATION, XATTR, ACL und OWNER.
read_header() {
  local list="$1" key value
  GENERATION=""
  while read -r _ key value; do
    case "${key}" in
      generation) GENERATION="${value}" ;;
      xattr) XATTR="${value}" ;;
      acl) ACL="${value}" ;;
      owner) OWNER="${value}" ;;
      *) ;;
    esac
  done < <(grep -m 5 '^# ' -- "${list}")
  [[ "${GENERATION}" =~ ^[0-9]{1,6}$ && "${XATTR}" =~ ^(yes|no)$ && "${ACL}" =~ ^(yes|no)$ &&
    "${OWNER}" =~ ^(yes|no)$ ]] || die "Die Prüfliste ist ungültig."
}

summary() {
  local list_hash
  list_hash="$(hash_text <"${LIST}")"
  echo "Einträge: $(grep -c -v '^#' -- "${LIST}")"
  echo "Sonderfälle: xattr ${XATTR}, acl ${ACL}, Besitzer ${OWNER}"
  echo "Prüfliste: ${LIST}, SHA-256 ${list_hash}"
}

# --- Befehle -------------------------------------------------------------------------------

cmd_create() {
  local dir="$1"
  check_writable_path "${dir}"
  [[ -d "${dir%/*}" ]] || die "Den Ordner über testdata gibt es nicht: ${dir%/*}"
  [[ ! -e "${dir}" ]] || die "${dir} gibt es schon; create legt nur neu an."
  if ((EUID == 0)); then
    OWNER=yes
  fi
  mkdir -- "${dir}"
  cd -- "${dir}"
  printf 'twinraid-testdata\n' >"${MARKER}"
  create_small
  create_large
  create_names
  create_deep
  create_links
  create_modes
  if [[ "${OWNER}" == yes ]]; then
    create_owners
  fi
  create_times
  create_special
  create_xattrs
  create_acls
  write_list 0
  echo "Testdaten angelegt: ${dir}"
  summary
}

cmd_change() {
  local dir="$1" i folder base file
  local -a small=()
  check_writable_path "${dir}"
  [[ -f "${dir}/${MARKER}" && -f "${dir}/${LIST}" ]] ||
    die "${dir} enthält keine Testdaten von create."
  read_header "${dir}/${LIST}"
  cd -- "${dir}"
  GENERATION=$((GENERATION + 1))
  mapfile -d '' -t small < <(find viele -type f -name 'datei-*' -print0 | sort -z)
  ((${#small[@]} >= CHANGE_DELETE + CHANGE_MODIFY + CHANGE_RENAME)) ||
    die "Es sind zu wenige Dateien übrig; Ordner löschen und neu anlegen."
  for ((i = 0; i < CHANGE_DELETE; i++)); do
    rm -- "${small[i]}"
  done
  for ((i = CHANGE_DELETE; i < CHANGE_DELETE + CHANGE_MODIFY; i++)); do
    printf 'geändert in Lauf %s\n' "${GENERATION}" >>"${small[i]}"
  done
  for ((i = CHANGE_DELETE + CHANGE_MODIFY; i < CHANGE_DELETE + CHANGE_MODIFY + CHANGE_RENAME; i++)); do
    base="${small[i]##*/}"
    folder="${small[i]%/*}"
    mv -- "${small[i]}" "${folder}/umbenannt-${GENERATION}-${base}"
  done
  mkdir -p -- neu
  for ((i = 1; i <= CHANGE_ADD; i++)); do
    printf -v file 'neu/lauf-%03d-datei-%03d.txt' "${GENERATION}" "${i}"
    write_small "${file}" "$((GENERATION * 1000 + i))"
  done
  # Ein Block mitten in einer großen Datei: Das Delta bleibt klein, die Prüfsumme nicht.
  head -c 1048576 /dev/urandom | dd of=gross/zufall-1.bin bs=1M seek=$((LARGE_MIB / 2)) \
    conv=notrunc status=none
  # Nur Eigenschaften, kein Inhalt.
  chmod 0660 -- rechte/datei-0664
  touch -m -d "@$((1600000000 + GENERATION))" -- zeiten/2000-nanosekunden.txt
  ln -sfn -- "ziel-lauf-${GENERATION}.txt" links/relativ
  if [[ "${XATTR}" == yes ]]; then
    setfattr -n user.twinraid.text -v "Lauf ${GENERATION}" -- xattr/datei.txt
  fi
  if [[ "${ACL}" == yes ]]; then
    setfacl -m "u:1000:r--" -- acl/datei.txt
  fi
  write_list "${GENERATION}"
  echo "Testdaten geändert: ${dir} (Lauf ${GENERATION})"
  echo "Dateien: ${CHANGE_DELETE} gelöscht, ${CHANGE_MODIFY} geändert, ${CHANGE_ADD} neu, ${CHANGE_RENAME} umbenannt"
  summary
}

# Im eingehängten Spiegel kann eine alte ACL stehen (Konzept 3.7); geprüft wird dort nur in
# einem Snapshot. realpath fasst doppelte Schrägstriche und Links zusammen.
check_not_mounted_mirror() {
  local dir="$1" real root
  real="$(realpath -e -- "${dir}" 2>/dev/null)" || return 0
  for root in "${MIRROR_ROOTS[@]}"; do
    if [[ "${real}/" == "${root}/"* && "${real}" != */.zfs/snapshot/* ]]; then
      die "${dir} ist der eingehängte Spiegel; geprüft wird nur im Snapshot: testdata.sh verify-latest"
    fi
  done
}

# Neuester Snapshot eines Datasets und sein Ordner unter .zfs/snapshot. Setzt SNAPSHOT_NAME
# und SNAPSHOT_DIR. Liest nur.
latest_snapshot() {
  local dataset="$1" newest mountpoint mounted
  [[ "${dataset}" =~ ${DATASET_PATTERN} ]] || die "Ungültiges Dataset: ${dataset}"
  newest="$("${ZFS}" list -H -t snapshot -d 1 -o name -s createtxg -- "${dataset}" 2>/dev/null |
    tail -n 1)" || newest=""
  [[ "${newest}" == "${dataset}@"* ]] || die "${dataset} hat keinen Snapshot."
  SNAPSHOT_NAME="${newest#"${dataset}@"}"
  [[ "${SNAPSHOT_NAME}" =~ ${SNAPSHOT_PATTERN} ]] || die "Unerwarteter Snapshot-Name."
  mountpoint="$("${ZFS}" get -H -o value mountpoint -- "${dataset}" 2>/dev/null)" || mountpoint=""
  mounted="$("${ZFS}" get -H -o value mounted -- "${dataset}" 2>/dev/null)" || mounted=""
  check_path "${mountpoint}"
  [[ "${mounted}" == yes ]] ||
    die "${dataset} ist nicht eingehängt (gesperrt?); siehe docs/WIEDERHERSTELLUNG.md."
  SNAPSHOT_DIR="${mountpoint}/.zfs/snapshot/${SNAPSHOT_NAME}"
}

cmd_verify_latest() {
  latest_snapshot "$1"
  echo "Snapshot: ${SNAPSHOT_NAME}"
  cmd_verify "${SNAPSHOT_DIR}/testdata"
}

# Letzte Zeile des Logs des Testcontainers im Snapshot: „sauber beendet“ heißt, er war beim
# Snapshot angehalten (Konzept 4.3, Test 6a). Ein Ordner muss die Wurzel eines Snapshots
# der Test-Share sein, etwa …/.zfs/snapshot/<Name>.
cmd_verify_container() {
  local target="$1" dir last
  if [[ "${target}" == /* ]]; then
    check_path "${target}"
    dir="${target}"
  else
    latest_snapshot "${target}"
    echo "Snapshot: ${SNAPSHOT_NAME}"
    dir="${SNAPSHOT_DIR}"
  fi
  [[ -f "${dir}/${CONTAINER_LOG}" ]] || die "In ${dir} liegt kein ${CONTAINER_LOG}."
  last="$(tail -n 1 -- "${dir}/${CONTAINER_LOG}")"
  if [[ "${last}" == "${CONTAINER_CLEAN}" ]]; then
    echo "Testcontainer war beim Snapshot sauber angehalten."
  elif [[ "${last}" =~ ^zaehler\ [0-9]{1,12}$ ]]; then
    echo "Testcontainer lief beim Snapshot (${last})."
  elif [[ -z "${last}" ]]; then
    echo "Das Log des Testcontainers war beim Snapshot leer (er leert es alle 10 Minuten)."
  else
    echo "Die letzte Zeile im Log des Testcontainers ist unbekannt."
  fi
}

cmd_verify() {
  local dir="$1" differences lines
  check_path "${dir}"
  check_not_mounted_mirror "${dir}"
  [[ -f "${dir}/${LIST}" ]] || die "In ${dir} liegt keine Prüfliste."
  read_header "${dir}/${LIST}"
  cd -- "${dir}"
  differences="$(diff <(grep -v '^#' -- "${LIST}") <(entries) | grep '^[<>]' || true)"
  if [[ -n "${differences}" ]]; then
    lines="$(grep -c '' <<<"${differences}")"
    echo "ABWEICHUNG: ${lines} Zeilen unterscheiden sich (< Prüfliste, > Ordner). Die ersten 20:"
    head -n 20 <<<"${differences}"
    exit 1
  fi
  echo "Keine Abweichung (Lauf ${GENERATION})."
  summary
}

# --- report (ab v0.5.3) --------------------------------------------------------------------

REPORT_DEVIATIONS=0

# Eine Zeile je Punkt. Der Text kommt nur aus festen Mustern, Zahlen, geprüften Werten und
# dem Namen aus dem Aufruf.
report_line() {
  printf '%-10s %s: %s\n' "$1" "$2" "$3"
  if [[ "$1" == ABWEICHUNG ]]; then
    REPORT_DEVIATIONS=$((REPORT_DEVIATIONS + 1))
  fi
}

# Ein Wert von zfs, nur wenn er harmlos aussieht; sonst "?".
safe_value() {
  if [[ -z "$1" ]]; then
    printf '%s' -
  elif [[ "$1" =~ ^[A-Za-z0-9._/:=+-]{1,64}$ ]]; then
    printf '%s' "$1"
  else
    printf '%s' '?'
  fi
}

# Ein Snapshot-Name des Wächters (twinraid-<Zeit>), sonst "?".
safe_snapshot() {
  if [[ "$1" =~ ${OWN_SNAPSHOT_PATTERN} ]]; then
    printf '%s' "$1"
  else
    printf '%s' '?'
  fi
}

# Die Nachrichten einer Kennung im Syslog, ohne Zeit und Rechnernamen; setzt SYSLOG_MESSAGES.
# Rückgabe 1, wenn das Syslog nicht lesbar ist. Die Nachrichten werden nie ausgegeben.
syslog_messages() {
  local tag="$1" line pattern
  SYSLOG_MESSAGES=()
  [[ -f "${SYSLOG}" && -r "${SYSLOG}" ]] || return 1
  pattern=" ${tag}(\[[0-9]+\])?: (.*)$"
  while IFS= read -r line; do
    if [[ "${line}" =~ ${pattern} ]]; then
      SYSLOG_MESSAGES+=("${BASH_REMATCH[2]}")
    fi
  done < <(grep -a -F -- " ${tag}" "${SYSLOG}" || true)
}

# snapshot_keep_days aus source-guard.conf, nur über ein festes Muster; sonst der Standard.
read_keep_days() {
  local line pattern='^[[:space:]]*snapshot_keep_days[[:space:]]+([0-9]{1,3})[[:space:]]*(#.*)?$'
  KEEP_DAYS="${DEFAULT_KEEP_DAYS}"
  KEEP_ORIGIN=Standard
  if [[ ! -f "${SOURCE_GUARD_CONF}" || -L "${SOURCE_GUARD_CONF}" ]]; then
    return 0
  fi
  line="$(grep -E -m 1 '^[[:space:]]*snapshot_keep_days[[:space:]]' -- "${SOURCE_GUARD_CONF}" 2>/dev/null)" ||
    line=""
  if [[ "${line}" =~ ${pattern} ]]; then
    KEEP_DAYS=$((10#${BASH_REMATCH[1]}))
    KEEP_ORIGIN="source-guard.conf"
  fi
}

# Klein: Hold, Lesezeichen, eigene Snapshots, Syslog des Wächters (Konzept 5.6).
report_source() {
  local name="$1" ds="${POOL}/$1" out snap property value source tag now limit i start=-1
  local held="" held_count=0 own_count=0 foreign=0 old=0 oldest=-1 failures=0 text
  local bookmark newest_bookmark="" bookmarks=0 has_held_bookmark=0
  local -A own=() created=() refs=() txg=()
  local -a names=() own_names=() problems=()
  echo "testdata.sh report source: ${ds}"

  if ! out="$("${ZFS}" get -H -p -d 1 -t snapshot -o name,property,value,source \
    -- "${CREATED_BY},creation,userrefs,createtxg" "${ds}" 2>/dev/null)"; then
    report_line ABWEICHUNG Snapshots "${ds} ist nicht lesbar"
    return 0
  fi
  while IFS=$'\t' read -r snap property value source; do
    if [[ "${snap}" != "${ds}@"* ]]; then
      continue
    fi
    case "${property}" in
      "${CREATED_BY}")
        if [[ "${value}" == source-guard && "${source}" == local ]]; then
          own["${snap}"]=1
        fi
        ;;
      creation) [[ ! "${value}" =~ ^[0-9]{1,12}$ ]] || created["${snap}"]=$((10#${value})) ;;
      userrefs) [[ ! "${value}" =~ ^[0-9]{1,9}$ ]] || refs["${snap}"]=$((10#${value})) ;;
      createtxg) [[ ! "${value}" =~ ^[0-9]{1,19}$ ]] || txg["${snap}"]="${value}" ;;
      *) ;;
    esac
  done <<<"${out}"
  mapfile -t names < <(for snap in "${!txg[@]}"; do
    printf '%s\t%s\n' "${txg[${snap}]}" "${snap}"
  done | sort -n | cut -f 2)

  # Eigene Snapshots: Eigenschaft des Wächters und sein Namensmuster. Namen aus zfs stehen
  # nie als Index in einem Rechenausdruck oder bei unset; bash würde ihn dort auswerten.
  for snap in "${names[@]+"${names[@]}"}"; do
    if [[ -z "${own[${snap}]+set}" || ! "${snap#"${ds}@"}" =~ ${OWN_SNAPSHOT_PATTERN} ||
      -z "${created[${snap}]+set}" ]]; then
      foreign=$((foreign + 1))
      continue
    fi
    own_names+=("${snap}")
    own_count=$((own_count + 1))
    value="${refs[${snap}]:-0}"
    if ((value > 0)); then
      out="$("${ZFS}" holds -H -- "${snap}" 2>/dev/null)" || out=""
      while IFS=$'\t' read -r property tag _; do
        if [[ "${property}" == "${snap}" && "${tag}" == "${HOLD_TAG}" ]]; then
          held="${snap#"${ds}@"}"
          held_count=$((held_count + 1))
        fi
      done <<<"${out}"
    fi
  done

  if ((held_count == 1)); then
    report_line OK Hold "${HOLD_TAG} an ${held}"
  elif ((held_count == 0)); then
    report_line ABWEICHUNG Hold "kein eigener Snapshot hat den Hold ${HOLD_TAG}"
  else
    report_line ABWEICHUNG Hold "${held_count} eigene Snapshots haben den Hold ${HOLD_TAG} (erwartet: einer)"
  fi

  if ! out="$("${ZFS}" list -H -p -d 1 -t bookmark -o name,createtxg -s createtxg \
    -- "${ds}" 2>/dev/null)"; then
    report_line ABWEICHUNG Lesezeichen "nicht lesbar"
  else
    while IFS=$'\t' read -r bookmark _; do
      if [[ "${bookmark}" == "${ds}#"* && "${bookmark#"${ds}#"}" =~ ${OWN_SNAPSHOT_PATTERN} ]]; then
        bookmarks=$((bookmarks + 1))
        newest_bookmark="${bookmark#"${ds}#"}"
        if [[ "${newest_bookmark}" == "${held}" ]]; then
          has_held_bookmark=1
        fi
      fi
    done <<<"${out}"
    if ((held_count != 1)); then
      report_line INFO Lesezeichen "nicht geprüft (keine eindeutige Basis); ${bookmarks} eigene"
    elif [[ "${newest_bookmark}" == "${held}" ]]; then
      report_line OK Lesezeichen "#${held} ist das neueste von ${bookmarks} eigenen"
    elif ((bookmarks == 0)); then
      report_line ABWEICHUNG Lesezeichen "kein eigenes Lesezeichen"
    elif ((has_held_bookmark)); then
      report_line ABWEICHUNG Lesezeichen "#${held} gibt es, das neueste ist aber #${newest_bookmark}"
    else
      report_line ABWEICHUNG Lesezeichen "kein Lesezeichen #${held}; das neueste ist #${newest_bookmark}"
    fi
  fi

  # Die Aufbewahrung läuft nach jedem Senden und einmal täglich; ein Tag Spielraum.
  read_keep_days
  printf -v now '%(%s)T' -1
  limit=$((now - (KEEP_DAYS + 1) * DAY))
  for snap in "${own_names[@]+"${own_names[@]}"}"; do
    value="${created[${snap}]}"
    if ((oldest < 0 || value < oldest)); then
      oldest="${value}"
    fi
    if [[ "${snap#"${ds}@"}" != "${held}" ]] && ((value < limit)); then
      old=$((old + 1))
    fi
  done
  if ((own_count == 0)); then
    report_line ABWEICHUNG Snapshots "keine eigenen Snapshots; ${foreign} fremde"
  else
    text="${own_count} eigene (höchstens ${SNAPSHOT_LIMIT}), der älteste vor $(((now - oldest) / DAY)) Tagen"
    text+=", Aufbewahrung ${KEEP_DAYS} Tage (${KEEP_ORIGIN}); ${foreign} fremde"
    if ((own_count > SNAPSHOT_LIMIT)); then
      problems+=("mehr als ${SNAPSHOT_LIMIT}")
    fi
    if ((old > 0)); then
      problems+=("${old} außer der Basis älter als ${KEEP_DAYS} Tage und ein Tag Spielraum")
    fi
    if ((${#problems[@]} == 0)); then
      report_line OK Snapshots "${text}"
    else
      report_line ABWEICHUNG Snapshots "${text}; $(IFS=';' && printf '%s' "${problems[*]}")"
    fi
  fi

  if ! syslog_messages twinraid-source-guard; then
    report_line ABWEICHUNG Syslog "${SYSLOG} ist nicht lesbar"
  else
    for i in "${!SYSLOG_MESSAGES[@]}"; do
      if [[ "${SYSLOG_MESSAGES[i]}" == "started (version "* ]]; then
        start="${i}"
      fi
    done
    for ((i = start < 0 ? 0 : start; i < ${#SYSLOG_MESSAGES[@]}; i++)); do
      value="${SYSLOG_MESSAGES[i]}"
      if [[ "${value}" == "sending ${name} failed"* || "${value}" == "dataset ${name}: hold on the new base failed"* ||
        "${value}" == "dataset ${name}: bookmark failed"* ]]; then
        failures=$((failures + 1))
      elif [[ "${value}" =~ ^retention:\ [0-9]+\ own\ snapshots\ destroyed,\ ([0-9]+)\ failed$ ]] &&
        ((10#${BASH_REMATCH[1]} > 0)); then
        failures=$((failures + 1))
      fi
    done
    if ((start < 0)); then
      text="im ganzen Syslog (kein Start des Wächters darin)"
    else
      text="seit dem letzten Start des Wächters"
    fi
    if ((failures == 0)); then
      report_line OK Syslog "${text} keine Fehlerzeile zu ${name} und zur Aufbewahrung"
    else
      report_line ABWEICHUNG Syslog "${text} ${failures} Fehlerzeilen zu ${name} oder zur Aufbewahrung (Einzelheiten im Syslog von Klein)"
    fi
  fi

  if ((held_count == 1)); then
    echo "Vergleich mit Groß: Der neueste Snapshot des Spiegels muss ${held} heißen (testdata.sh report target)."
  fi
}

# Groß: check, feste Werte, received, Einhängen, twinraid:received, Testdaten, Syslog von
# guard.sh (Konzept 5.6).
report_target() {
  local name="$1" ds="${MIRROR_BASE}/$1" mount="${MIRROR_MOUNT}/$1" out rc line pair property
  local value source version="" state="" props="" count="" root="" conf="" deleg="" receive=""
  local clones="" newest="" total=0 missing=0 lines start=-1 finished=0 refusal="" warnings=0 i
  local mirror_re='^mirror ([A-Za-z0-9._-]+) state=([a-z]+) props=([a-z-]+) count=([0-9]+|-|[?]) '
  local summary_re='^summary root=([a-z]+) conf=([a-z]+) deleg=([a-z-]+) receive=([a-z]+) clones=([0-9]+|[?]) '
  local -A want=()
  local -a bad=()
  echo "testdata.sh report target: ${ds}"

  rc=0
  out="$(SSH_ORIGINAL_COMMAND=check /bin/bash -- "${GUARD_SH}" sync 2>/dev/null)" || rc=$?
  while IFS= read -r line; do
    if [[ "${line}" =~ ^twinraid-guard\ ([0-9]{1,3})\ sync\ ok$ ]]; then
      version=$((10#${BASH_REMATCH[1]}))
    elif [[ "${line}" =~ ${mirror_re} && "${BASH_REMATCH[1]}" == "${name}" ]]; then
      state="${BASH_REMATCH[2]}"
      props="${BASH_REMATCH[3]}"
      count="${BASH_REMATCH[4]}"
    elif [[ "${line}" =~ ${summary_re} ]]; then
      root="${BASH_REMATCH[1]}"
      conf="${BASH_REMATCH[2]}"
      deleg="${BASH_REMATCH[3]}"
      receive="${BASH_REMATCH[4]}"
      clones="${BASH_REMATCH[5]}"
    fi
  done <<<"${out}"
  if ((rc != 0)); then
    report_line ABWEICHUNG check "guard.sh sync check endete mit Exit-Code ${rc}"
  elif [[ -z "${version}" || -z "${root}" ]]; then
    report_line ABWEICHUNG check "unvollständige Antwort von guard.sh sync check"
  elif [[ -z "${state}" ]]; then
    report_line ABWEICHUNG check "${name} steht nicht in der Antwort (mirror.conf?)"
  else
    text="guard.sh ${version} sync ok; state=${state} props=${props} count=${count}; root=${root} conf=${conf}"
    text+=" deleg=${deleg} receive=${receive} clones=${clones}"
    if ((version >= 3)) && [[ "${state}" == mirror && "${props}" == ok && "${root}" == ok &&
      "${conf}" == ok && "${deleg}" == ok && "${receive}" == idle && "${clones}" == 0 ]]; then
      report_line OK check "${text}"
    else
      report_line ABWEICHUNG check "${text}"
    fi
  fi

  for pair in "${FIXED_PROPS[@]}"; do
    want["${pair%%=*}"]="${pair#*=}"
  done
  want[mountpoint]="${mount}"
  line=""
  for property in "${!want[@]}"; do
    line+="${line:+,}${property}"
  done
  if ! out="$("${ZFS}" get -H -o property,value,source -- "${line}" "${ds}" 2>/dev/null)"; then
    report_line ABWEICHUNG "Feste Werte" "${ds} ist nicht lesbar"
  else
    count=0
    while IFS=$'\t' read -r property value source; do
      if [[ -z "${property}" || -z "${want[${property}]+set}" ]]; then
        continue
      fi
      count=$((count + 1))
      if [[ "${source}" != local ]] ||
        { [[ "${value}" != "${want[${property}]}" ]] && ! [[ "${property}" == compression && "${value}" == on ]]; }; then
        bad+=("${property}=$(safe_value "${value}") (Quelle $(safe_value "${source}"))")
      fi
    done <<<"${out}"
    if ((count < ${#want[@]})); then
      bad+=("$((${#want[@]} - count)) Werte fehlen")
    fi
    if ((${#bad[@]} == 0)); then
      report_line OK "Feste Werte" "alle ${#want[@]} mit Quelle local, darunter readonly=on, canmount=on, twinraid:mirror=1"
    else
      report_line ABWEICHUNG "Feste Werte" "$(IFS=';' && printf '%s' "${bad[*]}")"
    fi
  fi

  if ! out="$("${ZFS}" get -H -r -t filesystem,volume,snapshot -o name,property -s received \
    -- all "${ds}" 2>/dev/null)"; then
    report_line ABWEICHUNG received "nicht lesbar"
  else
    lines="$(grep -c . <<<"${out}" || true)"
    if ((lines == 0)); then
      report_line OK received "keine Eigenschaft mit Quelle received"
    else
      report_line ABWEICHUNG received "${lines} Eigenschaften mit Quelle received (zfs get -r -s received all ${ds})"
    fi
  fi

  out="$("${FINDMNT}" -n -r -o TARGET,SOURCE,FSTYPE -M "${mount}" 2>/dev/null)" || out=""
  if [[ "${out}" == "${mount} ${ds} zfs" ]]; then
    report_line OK Einhängen "${mount}, Quelle ${ds}, Typ zfs"
  elif [[ -z "${out}" ]]; then
    report_line ABWEICHUNG Einhängen "${mount} ist nicht eingehängt"
  else
    report_line ABWEICHUNG Einhängen "${mount} ist anders eingehängt (Quelle oder Typ passt nicht)"
  fi

  if ! out="$("${ZFS}" get -H -p -d 1 -t snapshot -o name,value,source -- twinraid:received \
    "${ds}" 2>/dev/null)"; then
    report_line ABWEICHUNG twinraid:received "nicht lesbar"
  else
    while IFS=$'\t' read -r line value source; do
      if [[ "${line}" != "${ds}@"* ]]; then
        continue
      fi
      total=$((total + 1))
      if [[ "${source}" != local || ! "${value}" =~ ^[0-9]{1,12}$ ]]; then
        missing=$((missing + 1))
      fi
    done <<<"${out}"
    newest="$("${ZFS}" list -H -t snapshot -d 1 -o name -s createtxg -- "${ds}" 2>/dev/null |
      tail -n 1)" || newest=""
    newest="$(safe_snapshot "${newest#"${ds}@"}")"
    if ((total == 0)); then
      report_line ABWEICHUNG twinraid:received "der Spiegel hat keine Snapshots"
    elif ((missing == 0)); then
      report_line OK twinraid:received "an allen ${total} Snapshots mit Quelle local; neuester ${newest}"
    else
      report_line ABWEICHUNG twinraid:received "fehlt an ${missing} von ${total} Snapshots (oder nicht mit Quelle local)"
    fi
  fi

  rc=0
  out="$( (cmd_verify_latest "${ds}") 2>&1)" || rc=$?
  if ((rc == 0)) && [[ "${out}" =~ Snapshot:\ ([^$'\n']+) ]]; then
    value="$(safe_snapshot "${BASH_REMATCH[1]}")"
    [[ "${out}" =~ Keine\ Abweichung\ \(Lauf\ ([0-9]+)\) ]] || BASH_REMATCH=("" "?")
    report_line OK Testdaten "verify-latest ohne Abweichung, Snapshot ${value}, Lauf ${BASH_REMATCH[1]}"
  elif ((rc == 1)); then
    report_line ABWEICHUNG Testdaten "verify-latest findet Abweichungen (Einzelheiten: testdata.sh verify-latest ${ds})"
  elif [[ "${out}" == *"keine Prüfliste"* ]]; then
    report_line INFO Testdaten "keine Testdaten im neuesten Snapshot, nicht geprüft"
  else
    report_line ABWEICHUNG Testdaten "verify-latest nicht möglich (Einzelheiten: testdata.sh verify-latest ${ds})"
  fi

  # Der letzte Empfang in diesen Spiegel: von „requested“ bis „finished“; danach nur noch
  # Warnungen zu diesem Spiegel.
  if ! syslog_messages twinraid-guard; then
    report_line ABWEICHUNG Syslog "${SYSLOG} ist nicht lesbar"
  else
    for i in "${!SYSLOG_MESSAGES[@]}"; do
      if [[ "${SYSLOG_MESSAGES[i]}" == "receive into mirror ${name} requested" ]]; then
        start="${i}"
      fi
    done
    if ((start < 0)); then
      report_line ABWEICHUNG Syslog "kein Empfang in ${name} im Syslog"
    else
      for ((i = start + 1; i < ${#SYSLOG_MESSAGES[@]}; i++)); do
        value="${SYSLOG_MESSAGES[i]}"
        if [[ "${value}" == "mirror ${name} locked" || "${value}" == "mirror ${name}: "* ||
          "${value}" == "failed: "*"mirror ${name} "* ]]; then
          warnings=$((warnings + 1))
        elif [[ "${value}" =~ ^retention\ of\ mirror\ ([A-Za-z0-9._-]+):\ [0-9]+\ snapshots\ destroyed,\ ([0-9]+)\ failed$ ]] &&
          [[ "${BASH_REMATCH[1]}" == "${name}" ]] && ((10#${BASH_REMATCH[2]} > 0)); then
          warnings=$((warnings + 1))
        elif ((finished == 0)); then
          if [[ "${value}" == "receive into mirror ${name} finished" ]]; then
            finished=1
          elif [[ "${value}" =~ ${SYNC_REFUSALS} && -z "${refusal}" ]]; then
            refusal="${value#refused: }"
          elif [[ "${value}" == "zfs receive: "* ]]; then
            warnings=$((warnings + 1))
          fi
        fi
      done
      if ((finished == 0)); then
        report_line ABWEICHUNG Syslog "der letzte Empfang in ${name} endete nicht mit finished${refusal:+ (abgelehnt: ${refusal})}"
      elif ((warnings > 0)); then
        report_line ABWEICHUNG Syslog "${warnings} Warnungen zu ${name} seit dem letzten Empfang (Einzelheiten im Syslog von Groß)"
      else
        report_line OK Syslog "der letzte Empfang in ${name} endete mit finished, ohne Warnung"
      fi
    fi
  fi

  if [[ -n "${newest}" && "${newest}" != '?' ]]; then
    echo "Vergleich mit Klein: Der gehaltene Snapshot auf Klein muss ${newest} heißen (testdata.sh report source)."
  fi
}

cmd_report() {
  local side="$1" name="$2"
  [[ "${side}" == source || "${side}" == target ]] || usage
  [[ "${name}" =~ ${NAME_PATTERN} ]] || die "Ungültiger Name; erlaubt sind die Zeichen von Share-Namen."
  if [[ "${side}" == source ]]; then
    report_source "${name}"
  else
    report_target "${name}"
  fi
  if ((REPORT_DEVIATIONS == 0)); then
    echo "Alles in Ordnung."
  elif ((REPORT_DEVIATIONS == 1)); then
    echo "1 Abweichung."
    exit 1
  else
    echo "${REPORT_DEVIATIONS} Abweichungen."
    exit 1
  fi
}

# --- bulk (ab v0.5.3) ----------------------------------------------------------------------

BULK_FILE=""
BULK_DIR=""
BULK_DONE=0

# Strg+C oder TERM: die angefangene Datei löschen, die fertigen bleiben.
bulk_abort() {
  trap - INT TERM
  if [[ -n "${BULK_FILE}" ]]; then
    rm -f -- "${BULK_FILE}"
  fi
  echo "testdata.sh: abgebrochen; ${BULK_DONE} fertige Dateien bleiben in ${BULK_DIR}, die angefangene ist gelöscht." >&2
  exit 130
}

check_bulk_path() {
  local dir="$1" name allowed=0
  for name in "${BULK_NAMES[@]}"; do
    if [[ "${dir}" == "${BULK_ROOT}/${name}" ]]; then
      allowed=1
    fi
  done
  ((allowed)) || die "Der Ordner muss ${BULK_ROOT}/kopie oder ${BULK_ROOT}/kopie2 sein."
  [[ -d "${BULK_ROOT}" && ! -L "${BULK_ROOT}" ]] || die "Den Ordner ${BULK_ROOT} gibt es nicht."
  [[ ! -e "${dir}" && ! -L "${dir}" ]] || die "${dir} gibt es schon; bulk legt nur neu an."
}

cmd_bulk() {
  local amount="$1" dir="$2" out used available size most i
  if [[ ! "${amount}" =~ ^[0-9]{1,3}$ ]] || ((10#${amount} < 1 || 10#${amount} > BULK_MAX_GIB)); then
    die "Die Menge muss eine ganze Zahl von 1 bis ${BULK_MAX_GIB} (GiB) sein."
  fi
  amount=$((10#${amount}))
  check_bulk_path "${dir}"
  out="$("${ZFS}" list -H -p -o used,available -- "${POOL}" 2>/dev/null)" || out=""
  [[ "${out}" =~ ^([0-9]{1,19})$'\t'([0-9]{1,19})$ ]] || die "Der Platz auf ${POOL} ist nicht lesbar."
  used="${BASH_REMATCH[1]}"
  available="${BASH_REMATCH[2]}"
  size=$((used + available))
  # Danach noch BULK_FREE_PERCENT frei: 15 % Reserve des Wächters plus 5 % Abstand.
  if (((available - amount * GIB) * 100 < BULK_FREE_PERCENT * size)); then
    most=$(((available * 100 - BULK_FREE_PERCENT * size) / 100 / GIB))
    if ((most < 0)); then
      most=0
    fi
    die "Zu wenig Platz: danach wären keine ${BULK_FREE_PERCENT} % von ${POOL} mehr frei; möglich sind höchstens ${most} GiB."
  fi
  mkdir -- "${dir}"
  BULK_DIR="${dir}"
  trap bulk_abort INT TERM
  for ((i = 1; i <= amount; i++)); do
    printf -v BULK_FILE '%s/zufall-%03d.bin' "${dir}" "${i}"
    if ! head -c "${GIB}" -- "${RANDOM_SOURCE}" >"${BULK_FILE}"; then
      rm -f -- "${BULK_FILE}"
      die "Schreiben scheiterte; ${BULK_DONE} fertige Dateien bleiben in ${dir}."
    fi
    BULK_FILE=""
    BULK_DONE="${i}"
    echo "${i} von ${amount} GiB geschrieben"
  done
  trap - INT TERM
  echo "Fertig: ${amount} GiB Zufallsdaten in ${dir}, Dateien zu 1 GiB."
}

case "${1:-}" in
  create) [[ $# -le 2 ]] || usage; cmd_create "${2:-${DEFAULT_DIR}}" ;;
  change) [[ $# -le 2 ]] || usage; cmd_change "${2:-${DEFAULT_DIR}}" ;;
  verify) [[ $# -le 2 ]] || usage; cmd_verify "${2:-${DEFAULT_DIR}}" ;;
  verify-latest) [[ $# -le 2 ]] || usage; cmd_verify_latest "${2:-${DEFAULT_DATASET}}" ;;
  verify-container) [[ $# -le 2 ]] || usage; cmd_verify_container "${2:-${DEFAULT_DATASET}}" ;;
  report) [[ $# -ge 2 && $# -le 3 ]] || usage; cmd_report "$2" "${3:-${DEFAULT_NAME}}" ;;
  bulk) [[ $# -ge 2 && $# -le 3 ]] || usage; cmd_bulk "$2" "${3:-${BULK_ROOT}/${BULK_NAMES[0]}}" ;;
  *) usage ;;
esac
