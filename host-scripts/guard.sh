#!/bin/bash
# TwinRaid: SSH-Wächter auf Unraid Groß, Version 3 (Konzept 3.5, 3.7 und 5.6).
#
# Ablage auf Groß: /boot/config/twinraid/guard.sh. Auf einem USB-Stick ist /boot
# FAT32 und nicht ausführbar; authorized_keys ruft das Skript deshalb über
# /bin/bash auf (das funktioniert ebenso bei internem Boot von einem ZFS-Pool):
#
#   restrict,from="<IP von TwinRaid>",command="/bin/bash /boot/config/twinraid/guard.sh status" ssh-ed25519 … twinraid-status
#   restrict,from="<IP von TwinRaid>",command="/bin/bash /boot/config/twinraid/guard.sh power" ssh-ed25519 … twinraid-power
#   restrict,from="<IP des Hosts Klein>",command="/bin/bash /boot/config/twinraid/guard.sh sync" ssh-ed25519 … twinraid-sync
#
# Die gewünschte Anfrage steht in SSH_ORIGINAL_COMMAND. Sie wird nur mit festen
# Texten verglichen und nie als Befehl ausgewertet. Parameter gibt es nicht; welche
# VM gemeint ist, steht nur in /boot/config/twinraid/power.conf (eine Zeile
# "vm <UUID>"). Einzige Ausnahme ist der Name bei "receive <Name>": Er muss einer
# Zeile "mirror <Name>" in /boot/config/twinraid/mirror.conf entsprechen.
#
#   guard.sh status   Key „status“: check | vm-watch (verändert nichts)
#   guard.sh power    Key „power“:  check | vm-start | vm-shutdown | vm-force-stop | poweroff
#   guard.sh sync     Key „sync“:   check | receive <Name> (Spiegel, Konzept 3.7)
#
# Exit-Code 2: Anfrage abgelehnt. Exit-Code 3: Anfrage erlaubt, eine Prüfung lehnt
# sie aber ab (stderr "twinraid-guard: refused <Grund>"). Anfragen und Ablehnungen
# stehen im Syslog von Groß (Tools → System Log, Kennung twinraid-guard).
set -euo pipefail
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

readonly VERSION=3
readonly POWEROFF=/sbin/poweroff
readonly VIRSH=/usr/bin/virsh
readonly LOGGER=/usr/bin/logger
# zfs, setpriv und findmnt über den festen PATH oben: zfs liegt nicht auf jedem System
# im selben Ordner.
readonly ZFS=zfs
readonly SETPRIV=setpriv
readonly FINDMNT=findmnt
readonly POWER_CONF=/boot/config/twinraid/power.conf
readonly MIRROR_CONF=/boot/config/twinraid/mirror.conf
# Besitzer von mirror.conf (root).
readonly OWNER_UID=0
# Merkmale von Unraid Connect (Konzept 3.5).
readonly PLUGIN_DIR=/boot/config/plugins
readonly LOADED_PLUGIN_DIR=/var/log/plugins
readonly FLASHBACKUP_INI=/var/local/emhttp/flashbackup.ini
# Arbeitsspeicher und ZFS-Cache (ARC, Konzept 3.5).
readonly MEMINFO=/proc/meminfo
readonly ZFS_MODULE=/sys/module/zfs
readonly ARC_MAX_FILE="${ZFS_MODULE}/parameters/zfs_arc_max"
readonly ARCSTATS=/proc/spl/kstat/zfs/arcstats
# Sicherheitsabstand beim Start der VM: 1 GiB (Konzept 3.5; nach Test M1 neu bewertet).
readonly MEMORY_MARGIN=1073741824
# Zeitlimits für virsh in Sekunden.
readonly QUERY_TIMEOUT=10
readonly START_TIMEOUT=90
readonly FORCE_TIMEOUT=60
readonly SHUTDOWN_TIMEOUT=30
# Takt von vm-watch: Pause zwischen zwei Abfragen (Sekunden), Lebenszeichen,
# power.conf neu lesen und Merkmale von Unraid Connect und ARC (Millisekunden).
readonly WATCH_SLEEP=3
readonly WATCH_KEEPALIVE_MS=15000
readonly WATCH_CONF_MS=15000
readonly WATCH_CONNECT_MS=60000
# Höchstens zwei vm-watch-Schleifen gleichzeitig (eine alte endet beim nächsten Lebenszeichen).
readonly WATCH_LOCK_DIR=/var/run
readonly WATCH_SLOTS=2
readonly MAX_NAMES=20
readonly MAX_NAME_BYTES=256
readonly UUID_PATTERN='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
# Spiegel (Konzept 3.7): Pool, Wurzel und Ort stehen fest; <Name> kommt aus mirror.conf.
readonly POOL=mainpool
readonly MIRROR_ROOT=mainpool/twinraid-mirror
readonly MOUNT_ROOT=/mnt/mainpool/twinraid-mirror
readonly NAME_PATTERN='^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
readonly MAX_MIRRORS=20
# Ein Empfang zur Zeit; poweroff nimmt dieselbe Sperre (sync-running).
readonly RECEIVE_LOCK=/var/run/twinraid-guard-receive.lock
# So lange hält ein Hintergrundprozess die Sperre nach poweroff (Sekunden, wie das Warten
# von TwinRaid auf das Ausschalten); beim Herunterfahren beendet Unraid ihn ohnehin.
readonly POWEROFF_LOCK_SECONDS=900
# Empfang ohne root (E27): feste UID und GID ohne Eintrag in /etc/passwd und /etc/group,
# nie nobody (99) oder eine UID, unter der auf Unraid Samba oder Container laufen.
readonly PASSWD=/etc/passwd
readonly GROUP=/etc/group
readonly RECV_UID=64000
readonly RECV_GID=64000
# Genau diese 28 Rechte, nur als Local+Descendent auf der Wurzel (Z1b, Konzept 3.7).
readonly RECV_PERMS=acltype,atime,canmount,checksum,compression,copies,create,dedup,devices,exec,mount,mountpoint,primarycache,quota,readonly,receive:append,recordsize,refquota,refreservation,reservation,secondarycache,setuid,sharenfs,sharesmb,snapdir,sync,userprop,xattr
# Feste Befehlszeile (Konzept 3.7); dazu -o mountpoint=<MOUNT_ROOT>/<Name>. Kein -F, -c,
# -s, -d, -e oder -x. Alle Werte stehen danach mit Quelle local am Spiegel.
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
# des Wächters (ohne -p) bringt keine mit. Jeder Eintrag braucht eine Begründung.
readonly RECEIVED_ALLOWED=()

declare -A ROOT_TYPES=()

# Nur feste Texte und geprüfte Werte ins Syslog, nie die Anfrage selbst.
log() {
  "${LOGGER}" -t twinraid-guard -- "$1" 2>/dev/null || true
}

deny() {
  log "denied: $1"
  echo "twinraid-guard: denied" >&2
  exit 2
}

refuse() {
  log "refused: $1"
  echo "twinraid-guard: refused $1" >&2
  exit 3
}

# Fehler ohne festen Grund: Exit-Code 1 ohne Text, die Ursache steht im Syslog.
fail() {
  log "failed: $1"
  exit 1
}

in_list() {
  local wanted="$1" item
  shift
  for item in "$@"; do
    if [[ "${item}" == "${wanted}" ]]; then
      return 0
    fi
  done
  return 1
}

# Zahl aus 1 bis 8 Ziffern im Bereich [min, max]. Setzt INT.
int_in() {
  local value="$1" min="$2" max="$3"
  if [[ ! "${value}" =~ ^[0-9]{1,8}$ ]]; then
    return 1
  fi
  INT=$((10#${value}))
  ((INT >= min && INT <= max))
}

# Liest power.conf: genau eine Zeile "vm <UUID>", sonst nur Leerzeilen und
# Kommentare. Setzt CONF_UUID; Rückgabe 1, wenn die Datei fehlt oder ungültig ist.
read_power_conf() {
  CONF_UUID=""
  local line found="" count=0
  if [[ ! -f "${POWER_CONF}" || -L "${POWER_CONF}" ]]; then
    return 1
  fi
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    count=$((count + 1))
    if ((count > 50)); then
      return 1
    fi
    if [[ -z "${line}" || "${line}" == "#"* ]]; then
      continue
    fi
    if [[ -n "${found}" || ! "${line}" =~ ^vm\ ([0-9A-Fa-f-]{36})$ ]]; then
      return 1
    fi
    found="${BASH_REMATCH[1],,}"
  done <"${POWER_CONF}"
  if [[ ! "${found}" =~ ${UUID_PATTERN} ]]; then
    return 1
  fi
  CONF_UUID="${found}"
}

# virsh mit Zeitlimit; die Ausgabe landet in QUERY_OUT, nie bei TwinRaid.
# Rückgabe: 0 erfolgreich, 1 virsh meldet einen Fehler, 2 Zeitlimit überschritten.
query() {
  local status=0
  QUERY_OUT="$(timeout --kill-after=2 "${QUERY_TIMEOUT}" "${VIRSH}" "$@" 2>/dev/null)" || status=$?
  if ((status == 0)); then
    return 0
  fi
  if ((status == 124 || status == 137)); then
    return 2
  fi
  return 1
}

# UUIDs aus einer Ausgabe von "virsh list --uuid"; Rückgabe 1 bei fremden Zeilen.
parse_uuids() {
  UUIDS=()
  local line
  while IFS= read -r line; do
    if [[ -z "${line}" ]]; then
      continue
    fi
    if [[ ! "${line}" =~ ${UUID_PATTERN} ]]; then
      return 1
    fi
    UUIDS+=("${line}")
  done <<<"$1"
}

# Alle VMs und die ausgeschalteten. Setzt ALL_UUIDS und OFF_UUIDS.
# Rückgabe: 0 erfolgreich, 1 VM-Dienst nicht erreichbar, 2 Zeitlimit.
list_vms() {
  local rc=0
  query list --all --uuid || rc=$?
  if ((rc != 0)); then
    return "${rc}"
  fi
  parse_uuids "${QUERY_OUT}" || return 1
  ALL_UUIDS=("${UUIDS[@]+"${UUIDS[@]}"}")
  query list --inactive --uuid || rc=$?
  if ((rc != 0)); then
    return "${rc}"
  fi
  parse_uuids "${QUERY_OUT}" || return 1
  OFF_UUIDS=("${UUIDS[@]+"${UUIDS[@]}"}")
}

is_off() {
  local uuid
  for uuid in "${OFF_UUIDS[@]+"${OFF_UUIDS[@]}"}"; do
    if [[ "${uuid}" == "$1" ]]; then
      return 0
    fi
  done
  return 1
}

is_listed() {
  local uuid
  for uuid in "${ALL_UUIDS[@]+"${ALL_UUIDS[@]}"}"; do
    if [[ "${uuid}" == "$1" ]]; then
      return 0
    fi
  done
  return 1
}

# Zustand einer VM in den Werten der Unraid-API. Setzt DOMAIN_STATE.
# Rückgabe: 0 erfolgreich, 1 Fehler, 2 Zeitlimit.
domain_state() {
  local rc=0 text
  query domstate --domain "$1" || rc=$?
  if ((rc != 0)); then
    return "${rc}"
  fi
  text="${QUERY_OUT%%$'\n'*}"
  case "${text}" in
    running) DOMAIN_STATE=RUNNING ;;
    idle) DOMAIN_STATE=IDLE ;;
    paused) DOMAIN_STATE=PAUSED ;;
    "in shutdown") DOMAIN_STATE=SHUTDOWN ;;
    "shut off") DOMAIN_STATE=SHUTOFF ;;
    crashed) DOMAIN_STATE=CRASHED ;;
    pmsuspended) DOMAIN_STATE=PMSUSPENDED ;;
    *) DOMAIN_STATE=NOSTATE ;;
  esac
}

# Hex-Kodierung (Kleinbuchstaben) der ersten MAX_NAME_BYTES Byte. Setzt HEX.
hex_of() {
  local text="${1:0:${MAX_NAME_BYTES}}"
  if [[ -z "${text}" ]]; then
    text="?"
  fi
  HEX="$(printf '%s' "${text}" | od -An -v -tx1)"
  HEX="${HEX//[[:space:]]/}"
}

# Name einer VM, hex-kodiert. Setzt HEX; "3f" („?“), wenn virsh ihn nicht liefert.
name_hex() {
  local rc=0
  query domname --domain "$1" || rc=$?
  if ((rc != 0)); then
    QUERY_OUT="?"
  fi
  hex_of "${QUERY_OUT}"
}

connect_state() {
  local dir name
  CONNECT=absent
  for dir in "${PLUGIN_DIR}" "${LOADED_PLUGIN_DIR}"; do
    for name in dynamix.unraid.net.plg dynamix.unraid.net.staging.plg; do
      if [[ -e "${dir}/${name}" ]]; then
        CONNECT=present
      fi
    done
  done
}

# Flash Backup von Unraid Connect: Schlüssel "activated" in flashbackup.ini.
flash_backup_state() {
  local line value lower
  FLASH_BACKUP=inactive
  if [[ ! -e "${FLASHBACKUP_INI}" ]]; then
    return 0
  fi
  if [[ ! -f "${FLASHBACKUP_INI}" || ! -r "${FLASHBACKUP_INI}" ]]; then
    FLASH_BACKUP=unknown
    return 0
  fi
  {
    while IFS= read -r line || [[ -n "${line}" ]]; do
      line="${line%$'\r'}"
      if [[ "${line}" =~ ^activated[[:space:]]*=[[:space:]]*(.*)$ ]]; then
        value="${BASH_REMATCH[1]//\"/}"
        value="${value//[[:space:]]/}"
        lower="${value,,}"
        case "${lower}" in
          "" | 0 | false | no | off) ;;
          *) FLASH_BACKUP=active ;;
        esac
      fi
    done <"${FLASHBACKUP_INI}"
  } 2>/dev/null || FLASH_BACKUP=unknown
}

# Werte aus arcstats in Byte (leer, wenn nicht lesbar). Setzt ARC_SIZE, ARC_C_MIN, ARC_C_MAX.
read_arcstats() {
  local name type data
  ARC_SIZE=""
  ARC_C_MIN=""
  ARC_C_MAX=""
  if [[ ! -f "${ARCSTATS}" || ! -r "${ARCSTATS}" ]]; then
    return 0
  fi
  {
    while read -r name type data; do
      if [[ ! "${type}" =~ ^[0-9]+$ || ! "${data}" =~ ^[0-9]{1,18}$ ]]; then
        continue
      fi
      case "${name}" in
        size) ARC_SIZE="${data}" ;;
        c_min) ARC_C_MIN="${data}" ;;
        c_max) ARC_C_MAX="${data}" ;;
      esac
    done <"${ARCSTATS}"
  } 2>/dev/null || true
}

# Grenze des ARC für vm-watch (Konzept 3.5): zfs_arc_max aus Unraid und die wirksame
# Obergrenze c_max, in Byte. "-": ZFS ist nicht geladen; "?": nicht lesbar.
# Setzt ARC_MAX und C_MAX.
arc_state() {
  local value=""
  ARC_MAX="-"
  C_MAX="-"
  if [[ ! -d "${ZFS_MODULE}" ]]; then
    return 0
  fi
  ARC_MAX="?"
  { IFS= read -r value <"${ARC_MAX_FILE}"; } 2>/dev/null || true
  if [[ "${value}" =~ ^[0-9]{1,20}$ ]]; then
    ARC_MAX="${value}"
  fi
  read_arcstats
  C_MAX="${ARC_C_MAX:-?}"
}

# Zustände für eine Zeile von vm-watch. Setzt VM_STATE, OTHERS und NAMES.
collect_states() {
  local uuid="$1" rc=0 other count=0 names=""
  VM_STATE=UNKNOWN
  OTHERS="?"
  NAMES="-"
  list_vms || rc=$?
  if ((rc == 1)); then
    VM_STATE=LIBVIRT_DOWN
  fi
  if ((rc != 0)); then
    if [[ -z "${uuid}" ]]; then
      VM_STATE=NOT_CONFIGURED
    fi
    return 0
  fi
  if [[ -z "${uuid}" ]]; then
    # Ohne eingetragene VM ist offen, welche VMs „weitere“ sind.
    VM_STATE=NOT_CONFIGURED
    return 0
  fi
  if ! is_listed "${uuid}"; then
    VM_STATE=NOT_FOUND
  else
    domain_state "${uuid}" || rc=$?
    case "${rc}" in
      0) VM_STATE="${DOMAIN_STATE}" ;;
      2) return 0 ;;
      *) VM_STATE=NOT_FOUND ;;
    esac
  fi
  for other in "${ALL_UUIDS[@]+"${ALL_UUIDS[@]}"}"; do
    if [[ "${other}" == "${uuid}" ]] || is_off "${other}"; then
      continue
    fi
    count=$((count + 1))
    if ((count <= MAX_NAMES)); then
      name_hex "${other}"
      names="${names:+${names},}${HEX}"
    fi
  done
  OTHERS="${count}"
  NAMES="${names:--}"
}

# Belegt einen der WATCH_SLOTS Plätze für vm-watch; die Sperre gilt, bis das Skript endet.
claim_watch_slot() {
  local slot fd
  for ((slot = 1; slot <= WATCH_SLOTS; slot++)); do
    if ! exec {fd}>>"${WATCH_LOCK_DIR}/twinraid-guard-watch.${slot}.lock"; then
      return 1
    fi
    if flock -n "${fd}"; then
      return 0
    fi
    exec {fd}>&-
  done
  return 1
}

now_ms() {
  local micro="${EPOCHREALTIME/./}"
  NOW_MS=$((10#${micro} / 1000))
}

# Eine Zeile bei jeder Änderung, sonst alle WATCH_KEEPALIVE_MS als Lebenszeichen.
# Schließt TwinRaid die Verbindung, scheitert das nächste Schreiben und das Skript endet.
watch_vms() {
  local uuid="" own_name="-" line last="" conf_logged=0
  local next_conf=0 next_connect=0 next_keepalive=0
  claim_watch_slot 2>/dev/null || refuse watch-busy
  log "vm-watch started"
  while true; do
    now_ms
    if ((NOW_MS >= next_conf)); then
      if read_power_conf; then
        uuid="${CONF_UUID}"
        if query domname --domain "${uuid}"; then
          hex_of "${QUERY_OUT}"
          own_name="${HEX}"
        else
          own_name="-"
        fi
      else
        uuid=""
        own_name="-"
        if ((conf_logged == 0)); then
          log "power.conf is missing or invalid"
          conf_logged=1
        fi
      fi
      next_conf=$((NOW_MS + WATCH_CONF_MS))
    fi
    if ((NOW_MS >= next_connect)); then
      connect_state
      flash_backup_state
      arc_state
      next_connect=$((NOW_MS + WATCH_CONNECT_MS))
    fi
    collect_states "${uuid}"
    line="watch ${VERSION} vm=${VM_STATE} name=${own_name} others=${OTHERS} names=${NAMES}"
    line+=" connect=${CONNECT} flash_backup=${FLASH_BACKUP} arc_max=${ARC_MAX} c_max=${C_MAX}"
    now_ms
    if [[ "${line}" != "${last}" ]] || ((NOW_MS >= next_keepalive)); then
      printf '%s\n' "${line}"
      last="${line}"
      next_keepalive=$((NOW_MS + WATCH_KEEPALIVE_MS))
    fi
    sleep "${WATCH_SLEEP}"
  done
}

# Groß nur ausschalten, wenn keine VM läuft (Konzept 3.5 und 4.2). Die eingetragene
# VM darf „crashed“ sein; ohne gültige power.conf muss jede VM „shut off“ sein.
ensure_vms_off() {
  local uuid="" rc=0 other
  if read_power_conf; then
    uuid="${CONF_UUID}"
  fi
  list_vms || rc=$?
  if ((rc != 0)); then
    refuse vm-state-unknown
  fi
  for other in "${ALL_UUIDS[@]+"${ALL_UUIDS[@]}"}"; do
    if is_off "${other}"; then
      continue
    fi
    if [[ "${other}" != "${uuid}" ]]; then
      refuse other-vms-running
    fi
    domain_state "${other}" || rc=$?
    if ((rc != 0)); then
      refuse vm-state-unknown
    fi
    if [[ "${DOMAIN_STATE}" != SHUTOFF && "${DOMAIN_STATE}" != CRASHED ]]; then
      refuse vm-running
    fi
  done
}

# VM-Befehl für die VM aus power.conf; Ausgaben von virsh gehen nicht an TwinRaid.
vm_command() {
  local request="$1" command="$2" limit="$3"
  read_power_conf || refuse vm-not-configured
  log "${request} of VM ${CONF_UUID} requested by TwinRaid"
  exec timeout --kill-after=5 "${limit}" "${VIRSH}" "${command}" --domain "${CONF_UUID}" \
    >/dev/null 2>&1
}

# Arbeitsspeicher, den eine VM beim Start bekommen kann (Konzept 3.5, D4): MemAvailable
# plus der Teil des ARC über c_min, den ZFS bei Bedarf abgibt. MemAvailable enthält den
# ARC nicht (Z1). Setzt FREE_BYTES; Rückgabe 1 ohne MemAvailable.
free_memory() {
  local key value unit available=""
  if [[ -f "${MEMINFO}" && -r "${MEMINFO}" ]]; then
    {
      while read -r key value unit; do
        if [[ "${key}" == MemAvailable: && "${value}" =~ ^[0-9]{1,15}$ && "${unit}" == kB ]]; then
          available="${value}"
        fi
      done <"${MEMINFO}"
    } 2>/dev/null || true
  fi
  if [[ -z "${available}" ]]; then
    return 1
  fi
  FREE_BYTES=$((10#${available} * 1024))
  read_arcstats
  if [[ -n "${ARC_SIZE}" && -n "${ARC_C_MIN}" ]] && ((ARC_SIZE > ARC_C_MIN)); then
    FREE_BYTES=$((FREE_BYTES + ARC_SIZE - ARC_C_MIN))
  fi
}

# VM starten (Konzept 3.5, E25e): erst die Speicherprüfung, dann virsh start. Meldet virsh
# ein belegtes Gerät, endet guard.sh mit device-busy; die Meldung selbst geht nirgends hin.
vm_start() {
  local line max_kib="" output rc=0
  read_power_conf || refuse vm-not-configured
  log "vm-start of VM ${CONF_UUID} requested by TwinRaid"
  query dominfo --domain "${CONF_UUID}" || refuse vm-state-unknown
  while IFS= read -r line; do
    if [[ "${line}" =~ ^Max\ memory:[[:space:]]+([0-9]{1,12})\ KiB$ ]]; then
      max_kib="${BASH_REMATCH[1]}"
    fi
  done <<<"${QUERY_OUT}"
  if [[ -z "${max_kib}" ]]; then
    refuse vm-state-unknown
  fi
  free_memory || refuse memory-low
  if ((FREE_BYTES < 10#${max_kib} * 1024 + MEMORY_MARGIN)); then
    refuse memory-low
  fi
  output="$(timeout --kill-after=5 "${START_TIMEOUT}" "${VIRSH}" start --domain "${CONF_UUID}" \
    2>&1 >/dev/null)" || rc=$?
  if ((rc == 0)); then
    exit 0
  fi
  if [[ "${output}" == *"is in use by"* ]]; then
    refuse device-busy
  fi
  exit "${rc}"
}

# --- Spiegel (Modus sync, Konzept 3.7) ----------------------------------------------------

# Ungültige mirror.conf: einmal ins Syslog, mit der Nummer der Zeile.
conf_invalid() {
  CONF_STATE=invalid
  log "mirror.conf is invalid (line $1)"
}

# Liest mirror.conf. Setzt MIRRORS, KEEP_DAYS, RESERVE_PERCENT, MIN_INTERVAL_MINUTES und
# CONF_STATE (ok, missing, invalid); Rückgabe 1, wenn die Datei fehlt oder ungültig ist.
read_mirror_conf() {
  local line number=0 key value lower seen=" " owner
  MIRRORS=()
  KEEP_DAYS=30
  RESERVE_PERCENT=15
  MIN_INTERVAL_MINUTES=10
  CONF_STATE=missing
  if [[ ! -e "${MIRROR_CONF}" && ! -L "${MIRROR_CONF}" ]]; then
    return 1
  fi
  owner="$(stat -c '%u' -- "${MIRROR_CONF}" 2>/dev/null)" || owner=""
  if [[ -L "${MIRROR_CONF}" || ! -f "${MIRROR_CONF}" || "${owner}" != "${OWNER_UID}" ]]; then
    conf_invalid 0
    return 1
  fi
  while IFS= read -r line || [[ -n "${line}" ]]; do
    number=$((number + 1))
    if ((number > 200)); then
      conf_invalid "${number}"
      return 1
    fi
    line="${line%$'\r'}"
    # Kommentare: Namen enthalten nie "#".
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    if [[ -z "${line}" ]]; then
      continue
    fi
    if [[ ! "${line}" =~ ^([a-z_]{1,40})[[:space:]]+([^[:space:]]{1,64})$ ]]; then
      conf_invalid "${number}"
      return 1
    fi
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    case "${key}" in
      mirror)
        lower="${value,,}"
        # zfs-check: Test-Dataset von zfs-check.sh (Konzept 5.6), nie ein Spiegel.
        if [[ ! "${value}" =~ ${NAME_PATTERN} || "${lower}" == system || "${lower}" == domains ||
          "${lower}" == zfs-check ]] ||
          in_list "${value}" "${MIRRORS[@]+"${MIRRORS[@]}"}" || ((${#MIRRORS[@]} >= MAX_MIRRORS)); then
          conf_invalid "${number}"
          return 1
        fi
        MIRRORS+=("${value}")
        continue
        ;;
      keep_days | reserve_percent | min_interval_minutes)
        if [[ "${seen}" == *" ${key} "* ]]; then
          conf_invalid "${number}"
          return 1
        fi
        seen+="${key} "
        ;;
      *)
        conf_invalid "${number}"
        return 1
        ;;
    esac
    case "${key}" in
      keep_days) int_in "${value}" 30 3650 && KEEP_DAYS="${INT}" ;;
      reserve_percent) int_in "${value}" 5 50 && RESERVE_PERCENT="${INT}" ;;
      min_interval_minutes) int_in "${value}" 10 1440 && MIN_INTERVAL_MINUTES="${INT}" ;;
    esac || {
      conf_invalid "${number}"
      return 1
    }
  done <"${MIRROR_CONF}"
  CONF_STATE=ok
}

# Wurzel und die Datasets direkt darunter. Setzt ROOT_TYPES[<Name>]=<Typ>; Rückgabe 1,
# wenn mainpool/twinraid-mirror fehlt oder kein Dateisystem ist (mirror-root-missing).
list_root() {
  local output name type root_type=""
  ROOT_TYPES=()
  output="$("${ZFS}" list -H -o name,type -d 1 -t filesystem,volume -- "${MIRROR_ROOT}" 2>/dev/null)" ||
    return 1
  while IFS=$'\t' read -r name type; do
    if [[ "${name}" == "${MIRROR_ROOT}" ]]; then
      root_type="${type}"
    elif [[ "${name}" == "${MIRROR_ROOT}/"* ]]; then
      ROOT_TYPES["${name#"${MIRROR_ROOT}/"}"]="${type}"
    fi
  done <<<"${output}"
  [[ "${root_type}" == filesystem ]]
}

# Zustand eines Spiegels nach list_root: absent (kein Dataset), mirror (Dateisystem mit
# twinraid:mirror=1, Quelle local, E26) oder foreign. Setzt STATE.
mirror_state() {
  local name="$1" output
  STATE=absent
  if [[ -z "${ROOT_TYPES[${name}]+set}" ]]; then
    return 0
  fi
  STATE=foreign
  if [[ "${ROOT_TYPES[${name}]}" != filesystem ]]; then
    return 0
  fi
  output="$("${ZFS}" get -H -o value,source -- twinraid:mirror "${MIRROR_ROOT}/${name}" 2>/dev/null)" ||
    return 0
  if [[ "${output}" == $'1\tlocal' ]]; then
    STATE=mirror
  fi
}

# Eigenschaften mit Quelle received am Spiegel, an seinen Snapshots und Kindern, außer den
# erlaubten. Rückgabe 0, wenn es welche gibt oder die Liste nicht lesbar ist.
has_received_properties() {
  local output name property
  output="$("${ZFS}" get -H -r -t filesystem,volume,snapshot -o name,property -s received \
    -- all "$1" 2>/dev/null)" || return 0
  while IFS=$'\t' read -r name property; do
    if [[ -z "${name}" ]] || in_list "${property}" "${RECEIVED_ALLOWED[@]+"${RECEIVED_ALLOWED[@]}"}"; then
      continue
    fi
    return 0
  done <<<"${output}"
  return 1
}

# Kinder bringt nur ein Strom von zfs send -R mit. Rückgabe 0, wenn es welche gibt oder die
# Liste nicht lesbar ist.
has_children() {
  local output
  output="$("${ZFS}" list -H -r -o name -t filesystem,volume -- "$1" 2>/dev/null)" || return 0
  [[ "${output}" != "$1" ]]
}

# Gesperrt (props-mismatch): canmount ist nicht on, eine Eigenschaft kam aus einem Strom,
# oder der Spiegel hat Kinder. Aufheben kann das nur root von Hand (Konzept 3.7).
mirror_locked() {
  local canmount
  canmount="$("${ZFS}" get -H -o value -- canmount "$1" 2>/dev/null)" || return 0
  [[ "${canmount}" != on ]] || has_received_properties "$1" || has_children "$1"
}

# Alle festen Werte der Empfangszeile und der feste Ort stehen mit Quelle local am Spiegel
# (props-not-applied). compression=on gilt wie lz4 (Konzept 3.7, E25c).
fixed_values_ok() {
  local dataset="$1" name="$2" i pair list="" output property value source count=0
  local -A want=()
  for ((i = 0; i < ${#RECEIVE_OPTIONS[@]}; i++)); do
    if [[ "${RECEIVE_OPTIONS[i]}" == -o ]]; then
      pair="${RECEIVE_OPTIONS[i + 1]}"
      want["${pair%%=*}"]="${pair#*=}"
    fi
  done
  want[mountpoint]="${MOUNT_ROOT}/${name}"
  for property in "${!want[@]}"; do
    list+="${list:+,}${property}"
  done
  output="$("${ZFS}" get -H -o property,value,source -- "${list}" "${dataset}" 2>/dev/null)" ||
    return 1
  while IFS=$'\t' read -r property value source; do
    if [[ -z "${want[${property}]+set}" || "${source}" != local ]]; then
      return 1
    fi
    if [[ "${value}" != "${want[${property}]}" ]] &&
      ! [[ "${property}" == compression && "${value}" == on ]]; then
      return 1
    fi
    count=$((count + 1))
  done <<<"${output}"
  ((count == ${#want[@]}))
}

# Sperre (Konzept 3.7): Spiegel und Kinder aushängen und mit canmount=noauto auch nach einem
# Neustart ausgehängt lassen; Kinder als Volume bekommen volmode=none.
lock_mirror() {
  local dataset="$1" name="$2" output i entry type mounted
  local -a entries=()
  output="$("${ZFS}" list -H -r -o name,type,mounted -t filesystem,volume -- "${dataset}" \
    2>/dev/null)" || output="${dataset}"$'\tfilesystem\tyes'
  mapfile -t entries <<<"${output}"
  # Kinder vor dem Spiegel: von unten nach oben.
  for ((i = ${#entries[@]} - 1; i >= 0; i--)); do
    IFS=$'\t' read -r entry type mounted <<<"${entries[i]}"
    if [[ "${entry}" != "${dataset}" && "${entry}" != "${dataset}/"* ]]; then
      continue
    fi
    if [[ "${type}" == volume ]]; then
      "${ZFS}" set -- volmode=none "${entry}" >/dev/null 2>&1 || true
      continue
    fi
    if [[ "${mounted}" == yes ]] && ! "${ZFS}" unmount -- "${entry}" >/dev/null 2>&1; then
      log "mirror ${name}: unmount failed"
    fi
    "${ZFS}" set -- canmount=noauto "${entry}" >/dev/null 2>&1 || log "mirror ${name}: canmount not set"
  done
  log "mirror ${name} locked"
}

# Prüfung der Delegation (Konzept 3.7, E27): Die Ausgabe von zfs allow für den Spiegel oder
# die Wurzel zeigt auch übergeordnete Datasets. Erlaubt sind genau die Kopfzeile der Wurzel
# (wie print_fs_perms in zfs_main.c: mit "-" auf 70 Zeichen aufgefüllt), die Überschrift
# "Local+Descendent permissions:" und eine Zeile für UID 64000 mit genau RECV_PERMS.
delegation_ok() {
  local output header pattern
  local -a lines=()
  output="$("${ZFS}" allow -- "$1" 2>/dev/null)" || return 1
  header="---- Permissions on ${MIRROR_ROOT} "
  while ((${#header} < 70)); do
    header+="-"
  done
  pattern=$'^\t'"user \\(unknown: ${RECV_UID}\\) ([a-z:,]+)\$"
  mapfile -t lines <<<"${output}"
  if ((${#lines[@]} != 3)) || [[ "${lines[0]}" != "${header}" ||
    "${lines[1]}" != "Local+Descendent permissions:" || ! "${lines[2]}" =~ ${pattern} ]]; then
    return 1
  fi
  # Als Menge: zfs allow sortiert die Rechte selbst; doppelte zählen als Abweichung.
  [[ "$(tr ',' '\n' <<<"${BASH_REMATCH[1]}" | sort)" == "$(tr ',' '\n' <<<"${RECV_PERMS}" | sort)" ]]
}

# UID und GID 64000 haben keinen Eintrag (receiver-id-in-use): /etc/passwd Felder UID und
# GID, /etc/group Feld GID, als Zahl verglichen. Nicht lesbar zählt wie belegt.
receiver_ids_free() {
  local name uid gid
  if [[ ! -r "${PASSWD}" || ! -r "${GROUP}" ]]; then
    return 1
  fi
  while IFS=: read -r name _ uid gid _ || [[ -n "${name}" ]]; do
    if [[ "${uid}" =~ ^[0-9]{1,10}$ ]] && ((10#${uid} == RECV_UID)); then
      return 1
    fi
    if [[ "${gid}" =~ ^[0-9]{1,10}$ ]] && ((10#${gid} == RECV_GID)); then
      return 1
    fi
  done <"${PASSWD}"
  while IFS=: read -r name _ gid _ || [[ -n "${name}" ]]; do
    if [[ "${gid}" =~ ^[0-9]{1,10}$ ]] && ((10#${gid} == RECV_GID)); then
      return 1
    fi
  done <"${GROUP}"
}

# Sperre für Empfänge: flock auf eine Datei in /var/run, nur lesend geöffnet, damit auch
# zfs receive als UID 64000 sie halten kann, ohne in die Datei schreiben zu können. Die
# Sperre gilt, solange ein Prozess den Deskriptor offen hat: das Skript, zfs receive und
# nach poweroff der Hintergrundprozess aus hold_receive_lock. Rückgabe 1, wenn belegt.
claim_receive_lock() {
  if [[ -L "${RECEIVE_LOCK}" ]]; then
    log "receive lock is a symlink"
    return 1
  fi
  if [[ ! -e "${RECEIVE_LOCK}" ]]; then
    : >>"${RECEIVE_LOCK}" || return 1
  fi
  if [[ ! -f "${RECEIVE_LOCK}" ]] || ! exec {RECEIVE_FD}<"${RECEIVE_LOCK}"; then
    return 1
  fi
  flock -n "${RECEIVE_FD}"
}

# Läuft ein Empfang? Nur für check: legt die Datei nicht an und lässt keine Sperre zurück.
receive_running() {
  local fd
  if [[ -L "${RECEIVE_LOCK}" || ! -f "${RECEIVE_LOCK}" ]] || ! exec {fd}<"${RECEIVE_LOCK}"; then
    return 1
  fi
  if flock -n "${fd}"; then
    exec {fd}<&-
    return 1
  fi
  exec {fd}<&-
}

# Hält die Sperre nach poweroff weiter: /sbin/poweroff ist auf Unraid ein Link auf halt
# (sysvinit) und übergibt nur an init; es kehrt zurück, während Unraid noch herunterfährt.
# Ein Hintergrundprozess erbt den Deskriptor, ignoriert SIGHUP und hat sonst nichts offen.
hold_receive_lock() {
  (
    trap '' HUP
    exec sleep "${POWEROFF_LOCK_SECONDS}"
  ) </dev/null >/dev/null 2>&1 &
}

# Klone von Snapshots der Spiegel aus mirror.conf (clone-active, Konzept 4.6). Setzt CLONES;
# Rückgabe 1, wenn das nicht lesbar ist.
count_clones() {
  local name output value commas
  CLONES=0
  for name in "${MIRRORS[@]+"${MIRRORS[@]}"}"; do
    mirror_state "${name}"
    if [[ "${STATE}" != mirror ]]; then
      continue
    fi
    output="$("${ZFS}" get -H -r -t snapshot -o value -- clones "${MIRROR_ROOT}/${name}" \
      2>/dev/null)" || return 1
    while IFS= read -r value; do
      if [[ -n "${value}" && "${value}" != - ]]; then
        commas="${value//[^,]/}"
        CLONES=$((CLONES + ${#commas} + 1))
      fi
    done <<<"${output}"
  done
}

# Platz (space-low): available der Wurzel, Größe des Pools als used plus available.
# Setzt AVAILABLE und SIZE; Rückgabe 1, wenn nicht lesbar.
read_space() {
  local output property value used="" available=""
  AVAILABLE=""
  SIZE=""
  output="$("${ZFS}" get -H -p -o property,value -- used,available "${POOL}" 2>/dev/null)" || return 1
  while IFS=$'\t' read -r property value; do
    if [[ "${value}" =~ ^[0-9]{1,16}$ ]]; then
      case "${property}" in
        used) used="${value}" ;;
        available) available="${value}" ;;
      esac
    fi
  done <<<"${output}"
  value="$("${ZFS}" get -H -p -o value -- available "${MIRROR_ROOT}" 2>/dev/null)" || return 1
  if [[ -z "${used}" || -z "${available}" || ! "${value}" =~ ^[0-9]{1,16}$ ]]; then
    return 1
  fi
  SIZE=$((10#${used} + 10#${available}))
  AVAILABLE=$((10#${value}))
}

# Snapshots eines Spiegels in der Reihenfolge ihres Entstehens. Setzt SNAP_NAMES, SNAP_GUIDS,
# SNAP_CREATED und SNAP_RECEIVED (twinraid:received nur mit Quelle local, sonst "-").
read_snapshots() {
  local dataset="$1" output name guid created value source
  local -A received=()
  SNAP_NAMES=()
  SNAP_GUIDS=()
  SNAP_CREATED=()
  SNAP_RECEIVED=()
  output="$("${ZFS}" get -H -p -r -t snapshot -o name,value,source \
    -- twinraid:received "${dataset}" 2>/dev/null)" || return 1
  while IFS=$'\t' read -r name value source; do
    if [[ "${source}" == local && "${value}" =~ ^[0-9]{1,12}$ ]]; then
      received["${name}"]=$((10#${value}))
    fi
  done <<<"${output}"
  output="$("${ZFS}" list -H -p -t snapshot -o name,guid,creation -s createtxg \
    -- "${dataset}" 2>/dev/null)" || return 1
  while IFS=$'\t' read -r name guid created; do
    if [[ -z "${name}" ]]; then
      continue
    fi
    if [[ ! "${guid}" =~ ^[0-9]{1,20}$ || ! "${created}" =~ ^[0-9]{1,12}$ ]]; then
      return 1
    fi
    SNAP_NAMES+=("${name}")
    SNAP_GUIDS+=("${guid}")
    SNAP_CREATED+=("$((10#${created}))")
    SNAP_RECEIVED+=("${received[${name}]:--}")
  done <<<"${output}"
}

# Hängt einen geprüften Spiegel ein und prüft den Ort mit findmnt (mount-mismatch); bei
# falschem Ort wieder aushängen.
mount_mirror() {
  local dataset="$1" name="$2" mounted where
  mounted="$("${ZFS}" get -H -o value -- mounted "${dataset}" 2>/dev/null)" || return 1
  if [[ "${mounted}" != yes ]] && ! "${ZFS}" mount -- "${dataset}" >/dev/null 2>&1; then
    return 1
  fi
  where="$("${FINDMNT}" -n -o TARGET -S "${dataset}" 2>/dev/null)" || where=""
  if [[ "${where}" != "${MOUNT_ROOT}/${name}" ]]; then
    "${ZFS}" unmount -- "${dataset}" >/dev/null 2>&1 || true
    return 1
  fi
}

# Aufbewahrung (Konzept 3.7) für alle Spiegel aus mirror.conf, die in Ordnung sind: nur
# twinraid:received mit Quelle local zählt; er und creation (von der Uhr von Klein) müssen
# beide älter als KEEP_DAYS sein, damit eine vorgestellte Uhr auf Groß allein nichts löscht.
# Nie die drei neuesten, nie mit Klon, nie mit -r, nie ein Name ohne "@".
retention() {
  local name dataset i snap clones limit destroyed failed
  limit=$((NOW - KEEP_DAYS * 86400))
  list_root || return 0
  for name in "${MIRRORS[@]+"${MIRRORS[@]}"}"; do
    dataset="${MIRROR_ROOT}/${name}"
    mirror_state "${name}"
    if [[ "${STATE}" != mirror ]] || mirror_locked "${dataset}" ||
      ! fixed_values_ok "${dataset}" "${name}" || ! read_snapshots "${dataset}"; then
      continue
    fi
    destroyed=0
    failed=0
    for ((i = 0; i < ${#SNAP_NAMES[@]} - 3; i++)); do
      snap="${SNAP_NAMES[i]}"
      if [[ "${snap}" != "${dataset}@"* || "${snap#"${dataset}@"}" == *[@/#]* ||
        "${SNAP_RECEIVED[i]}" == - ]] ||
        ((SNAP_RECEIVED[i] >= limit || SNAP_CREATED[i] >= limit)); then
        continue
      fi
      clones="$("${ZFS}" get -H -o value -- clones "${snap}" 2>/dev/null)" || continue
      if [[ -n "${clones}" && "${clones}" != - ]]; then
        continue
      fi
      if "${ZFS}" destroy -- "${snap}" >/dev/null 2>&1; then
        destroyed=$((destroyed + 1))
      else
        failed=$((failed + 1))
      fi
    done
    if ((destroyed > 0 || failed > 0)); then
      log "retention of mirror ${name}: ${destroyed} snapshots destroyed, ${failed} failed"
    fi
  done
}

# Zeile für check: Zustand, Sperre und Snapshots eines Spiegels (Format in Konzept 3.7).
mirror_line() {
  local name="$1" dataset props="-" count=0 newest_guid="-" newest_created="-"
  local newest_received="-" oldest_created="-" last
  dataset="${MIRROR_ROOT}/${name}"
  mirror_state "${name}"
  if [[ "${STATE}" == foreign ]]; then
    count="-"
  elif [[ "${STATE}" == mirror ]]; then
    props=ok
    if mirror_locked "${dataset}" || ! fixed_values_ok "${dataset}" "${name}"; then
      props=mismatch
    fi
    if ! read_snapshots "${dataset}"; then
      count="?"
      newest_guid="?"
      newest_created="?"
      newest_received="?"
      oldest_created="?"
    elif ((${#SNAP_NAMES[@]} > 0)); then
      count="${#SNAP_NAMES[@]}"
      last=$((count - 1))
      newest_guid="${SNAP_GUIDS[last]}"
      newest_created="${SNAP_CREATED[last]}"
      newest_received="${SNAP_RECEIVED[last]}"
      oldest_created="${SNAP_CREATED[0]}"
    fi
  fi
  printf 'mirror %s state=%s props=%s count=%s newest_guid=%s newest_created=%s' \
    "${name}" "${STATE}" "${props}" "${count}" "${newest_guid}" "${newest_created}"
  printf ' newest_received=%s oldest_created=%s\n' "${newest_received}" "${oldest_created}"
}

# check im Modus sync: verändert nichts (Konzept 3.7).
sync_check() {
  local conf=ok root=ok deleg=ok receive=idle clones="?" available="?" size="?"
  local reserve="-" keep="-" interval="-" name
  read_mirror_conf || conf="${CONF_STATE}"
  list_root || root=missing
  echo "twinraid-guard ${VERSION} sync ok"
  if [[ "${conf}" == ok ]]; then
    reserve="${RESERVE_PERCENT}"
    keep="${KEEP_DAYS}"
    interval="${MIN_INTERVAL_MINUTES}"
    for name in "${MIRRORS[@]+"${MIRRORS[@]}"}"; do
      mirror_line "${name}"
    done
  fi
  if ! receiver_ids_free; then
    deleg=id-in-use
  elif [[ "${root}" != ok ]] || ! delegation_ok "${MIRROR_ROOT}"; then
    deleg=mismatch
  else
    for name in "${MIRRORS[@]+"${MIRRORS[@]}"}"; do
      mirror_state "${name}"
      if [[ "${STATE}" == mirror ]] && ! delegation_ok "${MIRROR_ROOT}/${name}"; then
        deleg=mismatch
      fi
    done
  fi
  if receive_running; then
    receive=busy
  fi
  if count_clones; then
    clones="${CLONES}"
  fi
  if read_space; then
    available="${AVAILABLE}"
    size="${SIZE}"
  fi
  printf 'summary root=%s conf=%s deleg=%s receive=%s clones=%s available=%s size=%s' \
    "${root}" "${conf}" "${deleg}" "${receive}" "${clones}" "${available}" "${size}"
  printf ' reserve_percent=%s keep_days=%s min_interval_minutes=%s\n' "${reserve}" "${keep}" "${interval}"
}

# Erste Meldung von zfs receive ins Syslog, nur druckbares ASCII, höchstens 200 Zeichen;
# sie geht nie an Klein (Konzept 3.7).
log_receive_message() {
  local first="${1%%$'\n'*}"
  if [[ -n "${first}" ]]; then
    first="${first//[^[:print:]]/?}"
    log "zfs receive: ${first:0:200}"
  fi
}

# receive <Name> (Konzept 3.7, E27): Vorprüfungen als root, Empfang als UID 64000 mit der
# festen Befehlszeile, danach als root prüfen, sperren oder einhängen, Aufbewahrung.
sync_receive() {
  local name="$1" dataset existed=0 rc=0 output snap last_received
  local -a before=()
  dataset="${MIRROR_ROOT}/${name}"
  if ! read_mirror_conf || ! in_list "${name}" "${MIRRORS[@]+"${MIRRORS[@]}"}"; then
    refuse name-denied
  fi
  log "receive into mirror ${name} requested"
  claim_receive_lock || refuse receive-busy
  list_root || refuse mirror-root-missing
  receiver_ids_free || refuse receiver-id-in-use
  mirror_state "${name}"
  if [[ "${STATE}" == foreign ]]; then
    refuse target-exists
  fi
  if [[ "${STATE}" == mirror ]]; then
    existed=1
    delegation_ok "${dataset}" || refuse delegation-mismatch
    if mirror_locked "${dataset}"; then
      refuse props-mismatch
    fi
    # Das Recht readonly erlaubt auch zfs set readonly=off; die festen Werte schützt diese
    # Prüfung, nicht die Delegation.
    if ! fixed_values_ok "${dataset}" "${name}"; then
      lock_mirror "${dataset}" "${name}"
      refuse props-not-applied
    fi
  else
    delegation_ok "${MIRROR_ROOT}" || refuse delegation-mismatch
  fi
  count_clones || refuse clone-active
  if ((CLONES > 0)); then
    refuse clone-active
  fi
  if ! read_space || ((AVAILABLE * 100 < RESERVE_PERCENT * SIZE)); then
    refuse space-low
  fi
  printf -v NOW '%(%s)T' -1
  if ((existed)); then
    read_snapshots "${dataset}" || fail "snapshots of mirror ${name} are not readable"
    # Mindestabstand: twinraid:received am neuesten Snapshot; ein Wert in der Zukunft (etwa
    # nach einer zurückgestellten Uhr) sperrt nicht.
    if ((${#SNAP_NAMES[@]} > 0)); then
      last_received="${SNAP_RECEIVED[${#SNAP_NAMES[@]} - 1]}"
      if [[ "${last_received}" != - ]] &&
        ((last_received <= NOW && NOW - last_received < MIN_INTERVAL_MINUTES * 60)); then
        refuse too-soon
      fi
    fi
    before=("${SNAP_NAMES[@]+"${SNAP_NAMES[@]}"}")
  fi

  # Eine abgebrochene Verbindung beendet zfs receive über das Ende des Stroms; die Prüfungen
  # danach laufen trotzdem.
  trap '' HUP
  log "receive into mirror ${name} started"
  output="$("${SETPRIV}" --reuid "${RECV_UID}" --regid "${RECV_GID}" --clear-groups \
    --inh-caps=-all --bounding-set=-all --no-new-privs -- \
    "${ZFS}" receive "${RECEIVE_OPTIONS[@]}" -o "mountpoint=${MOUNT_ROOT}/${name}" \
    -- "${dataset}" 2>&1 >/dev/null)" || rc=$?
  log_receive_message "${output}"

  # Auch nach einem Fehler: Ein Strom mit mehreren Snapshots kann einen Teil angelegt haben.
  if list_root && [[ -n "${ROOT_TYPES[${name}]+set}" ]]; then
    if has_received_properties "${dataset}" || has_children "${dataset}"; then
      lock_mirror "${dataset}" "${name}"
      refuse received-properties
    fi
    # Ein Volume hat die festen Werte nicht (etwa exec und mountpoint).
    if [[ "${ROOT_TYPES[${name}]}" != filesystem ]] || ! fixed_values_ok "${dataset}" "${name}"; then
      lock_mirror "${dataset}" "${name}"
      refuse props-not-applied
    fi
    read_snapshots "${dataset}" || fail "snapshots of mirror ${name} are not readable"
    printf -v NOW '%(%s)T' -1
    for snap in "${SNAP_NAMES[@]+"${SNAP_NAMES[@]}"}"; do
      if ! in_list "${snap}" "${before[@]+"${before[@]}"}"; then
        "${ZFS}" set -- "twinraid:received=${NOW}" "${snap}" >/dev/null 2>&1 ||
          log "mirror ${name}: twinraid:received not set"
      fi
    done
    mount_mirror "${dataset}" "${name}" || refuse mount-mismatch
  elif ((rc == 0)); then
    fail "mirror ${name} is missing after the receive"
  fi
  if ((rc != 0)); then
    if [[ "${output}" == *"has been modified"* ]]; then
      refuse target-modified
    fi
    fail "receive into mirror ${name} (zfs exit code ${rc})"
  fi
  retention
  log "receive into mirror ${name} finished"
}

mode="${1:-}"
request="${SSH_ORIGINAL_COMMAND:-}"

case "${mode}" in
  status)
    case "${request}" in
      check)
        echo "twinraid-guard ${VERSION} status ok"
        ;;
      vm-watch)
        watch_vms
        ;;
      *)
        deny "unknown request for mode status"
        ;;
    esac
    ;;
  power)
    case "${request}" in
      check)
        echo "twinraid-guard ${VERSION} power ok"
        ;;
      vm-start)
        vm_start
        ;;
      vm-shutdown)
        vm_command "ACPI shutdown" shutdown "${SHUTDOWN_TIMEOUT}"
        ;;
      vm-force-stop)
        vm_command "forced stop" destroy "${FORCE_TIMEOUT}"
        ;;
      poweroff)
        # Die Sperre bleibt bis zum Ausschalten belegt: Danach beginnt kein Empfang mehr.
        claim_receive_lock || refuse sync-running
        ensure_vms_off
        log "poweroff requested by TwinRaid"
        hold_receive_lock
        exec "${POWEROFF}"
        ;;
      *)
        deny "unknown request for mode power"
        ;;
    esac
    ;;
  sync)
    case "${request}" in
      check)
        sync_check
        ;;
      *)
        if [[ "${request}" =~ ^receive\ ([A-Za-z0-9][A-Za-z0-9._-]{0,63})$ ]]; then
          sync_receive "${BASH_REMATCH[1]}"
        else
          deny "unknown request for mode sync"
        fi
        ;;
    esac
    ;;
  *)
    deny "unknown mode"
    ;;
esac
