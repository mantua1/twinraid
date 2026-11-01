#!/bin/bash
# TwinRaid: Testdaten für die Test-Share (docs/EINRICHTUNG.md, Abschnitt 15, ab v0.5.1).
#
#   testdata.sh create [Ordner]   legt die Testdaten mit allen Sonderfällen und die Prüfliste an
#   testdata.sh change [Ordner]   ändert sie auf bekannte Weise und schreibt die Prüfliste neu
#   testdata.sh verify [Ordner]   vergleicht den Ordner mit der Prüfliste, ändert nichts
#
# Ohne Ordner gilt /mnt/mainpool/twinraidtest/testdata. create und change schreiben nur in
# einen Ordner namens testdata, und change nur, wenn dort die Kennung von create liegt.
# verify liest nur; es läuft deshalb auch in einem schreibgeschützten Spiegel auf Groß
# (/mnt/mainpool/twinraid-mirror/twinraidtest/testdata) oder in einem Snapshot.
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

XATTR=no
ACL=no
OWNER=no

die() {
  echo "testdata.sh: $*" >&2
  exit 2
}

usage() {
  echo "Aufruf: testdata.sh create|change|verify [Ordner]" >&2
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

cmd_verify() {
  local dir="$1" differences lines
  check_path "${dir}"
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

case "${1:-}" in
  create) [[ $# -le 2 ]] || usage; cmd_create "${2:-${DEFAULT_DIR}}" ;;
  change) [[ $# -le 2 ]] || usage; cmd_change "${2:-${DEFAULT_DIR}}" ;;
  verify) [[ $# -le 2 ]] || usage; cmd_verify "${2:-${DEFAULT_DIR}}" ;;
  *) usage ;;
esac
