#!/bin/bash
# TwinRaid: SSH-Wächter auf Unraid Groß, Version 2 (Konzept 3.5 und 5.6).
#
# Ablage auf Groß: /boot/config/twinraid/guard.sh. Auf einem USB-Stick ist /boot
# FAT32 und nicht ausführbar; authorized_keys ruft das Skript deshalb über
# /bin/bash auf (das funktioniert ebenso bei internem Boot von einem ZFS-Pool):
#
#   restrict,from="<IP von TwinRaid>",command="/bin/bash /boot/config/twinraid/guard.sh status" ssh-ed25519 … twinraid-status
#   restrict,from="<IP von TwinRaid>",command="/bin/bash /boot/config/twinraid/guard.sh power" ssh-ed25519 … twinraid-power
#
# Die gewünschte Anfrage steht in SSH_ORIGINAL_COMMAND. Sie wird nur mit festen
# Texten verglichen und nie als Befehl ausgewertet. Parameter gibt es nicht:
# Welche VM gemeint ist, steht nur in /boot/config/twinraid/power.conf
# (eine Zeile "vm <UUID>").
#
#   guard.sh status   Key „status“: check | vm-watch (verändert nichts)
#   guard.sh power    Key „power“:  check | vm-start | vm-shutdown | vm-force-stop | poweroff
#   guard.sh sync     Key „sync“ (Phase 5); bis dahin wird alles abgelehnt
#
# Exit-Code 2: Anfrage abgelehnt. Exit-Code 3: Anfrage erlaubt, eine Prüfung lehnt
# sie aber ab (stderr "twinraid-guard: refused <Grund>"). Anfragen und Ablehnungen
# stehen im Syslog von Groß (Tools → System Log, Kennung twinraid-guard).
set -euo pipefail
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

readonly VERSION=2
readonly POWEROFF=/sbin/poweroff
readonly VIRSH=/usr/bin/virsh
readonly LOGGER=/usr/bin/logger
readonly POWER_CONF=/boot/config/twinraid/power.conf
# Merkmale von Unraid Connect (Konzept 3.5).
readonly PLUGIN_DIR=/boot/config/plugins
readonly LOADED_PLUGIN_DIR=/var/log/plugins
readonly FLASHBACKUP_INI=/var/local/emhttp/flashbackup.ini
# Zeitlimits für virsh in Sekunden.
readonly QUERY_TIMEOUT=10
readonly START_TIMEOUT=90
readonly FORCE_TIMEOUT=60
readonly SHUTDOWN_TIMEOUT=30
# Takt von vm-watch: Pause zwischen zwei Abfragen (Sekunden), Lebenszeichen,
# power.conf neu lesen und Merkmale von Unraid Connect (Millisekunden).
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
      next_connect=$((NOW_MS + WATCH_CONNECT_MS))
    fi
    collect_states "${uuid}"
    line="watch ${VERSION} vm=${VM_STATE} name=${own_name} others=${OTHERS} names=${NAMES}"
    line+=" connect=${CONNECT} flash_backup=${FLASH_BACKUP}"
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
        vm_command vm-start start "${START_TIMEOUT}"
        ;;
      vm-shutdown)
        vm_command "ACPI shutdown" shutdown "${SHUTDOWN_TIMEOUT}"
        ;;
      vm-force-stop)
        vm_command "forced stop" destroy "${FORCE_TIMEOUT}"
        ;;
      poweroff)
        ensure_vms_off
        log "poweroff requested by TwinRaid"
        exec "${POWEROFF}"
        ;;
      *)
        deny "unknown request for mode power"
        ;;
    esac
    ;;
  sync)
    deny "mode sync is not available yet"
    ;;
  *)
    deny "unknown mode"
    ;;
esac
