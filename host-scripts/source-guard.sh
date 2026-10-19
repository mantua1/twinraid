#!/bin/bash
# TwinRaid: Wächter auf dem Host von Unraid Klein, Version 1 (Konzept 3.6).
#
# Ablage auf Klein: /boot/config/twinraid/source-guard.sh, Konfiguration daneben in
# source-guard.conf (Vorlage: host-scripts/source-guard.conf.example). Gestartet wird
# der Wächter aus /boot/config/go, vor emhttp:
#
#   /bin/bash /boot/config/twinraid/source-guard.sh start
#
# TwinRaid gibt ihm nur Aufträge über eine einzelne Datei, die als Datei in den
# Container eingebunden ist, und liest seinen Status (state/status.json, read-only
# eingebunden). Was erlaubt ist, entscheidet der Wächter nach seiner eigenen
# Konfiguration. Container hält er nur an, wenn sie in "allow" stehen, nie TwinRaid
# selbst, und startet nur die wieder, die er selbst angehalten hat. Zwischen zwei Läufen
# liegt eine Pause (min_run_interval_minutes); alle Fristen laufen nach der Laufzeit des
# Systems, nicht nach der Uhrzeit.
#
#   source-guard.sh start | stop | restart | status | check-config | reset-run-interval
#   source-guard.sh selftest <Container> | selftest-deadman <Container>
#
# Version 1 (TwinRaid v0.5.0) kennt die Aufträge begin, stop, ping und finish.
# snapshot, prepare, confirm, send und close lehnt sie mit not_supported ab.
#
# Ins Syslog (Kennung twinraid-source-guard) kommen nur feste Texte, Codes, Zahlen
# und geprüfte Container-Namen, nie der Inhalt der Auftragsdatei.
set -euo pipefail
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

readonly VERSION=1
readonly PROTOCOL=1
readonly CONF=/boot/config/twinraid/source-guard.conf
readonly BASE_DIR=/run/twinraid-source-guard
# Andere Schreibweise desselben Ordners: auf Unraid ist /var/run ein Link auf /run.
readonly BASE_DIR_ALIAS=/var/run/twinraid-source-guard
# Besitzer der Dateien und Ordner des Wächters (root); im Test der eigene Benutzer.
readonly OWNER_UID=0
readonly DOCKER=/usr/bin/docker
readonly ZFS=/usr/sbin/zfs
readonly ZPOOL=/usr/sbin/zpool
readonly LOGGER=/usr/bin/logger
readonly MOUNT=/bin/mount
readonly FINDMNT=/bin/findmnt
# Laufzeit des Systems für alle Fristen (unabhängig von Sprüngen der Uhr).
readonly UPTIME=/proc/uptime
readonly POOL=mainpool
readonly SELF_DATASET=mainpool/appdata/twinraid
# Merkmale von Unraid Connect (Konzept 3.5), wie in guard.sh.
readonly PLUGIN_DIR=/boot/config/plugins
readonly LOADED_PLUGIN_DIR=/var/log/plugins
readonly FLASHBACKUP_INI=/var/local/emhttp/flashbackup.ini
# Selbstschutz: Image und Label von TwinRaid.
readonly SELF_IMAGE=ghcr.io/mantua1/twinraid
readonly SELF_SOURCE=https://github.com/mantua1/twinraid
# Zeiten in Sekunden; MINUTE für max_stop_minutes und min_run_interval_minutes (im Test kürzer).
readonly MINUTE=60
readonly LOOP_SLEEP=1
readonly STATUS_INTERVAL=5
readonly DOCKER_INTERVAL=5
readonly CONTAINER_INTERVAL=15
readonly ZFS_INTERVAL=30
readonly HOST_INTERVAL=60
readonly DOCKER_TIMEOUT=10
readonly STOP_EXTRA=15
readonly REREAD_DELAY=0.2
readonly LOG_INTERVAL=60
# Überwachungsschleife: Neustart nach RESTART_DELAY, höchstens RESTART_LIMIT-mal in RESTART_WINDOW.
readonly RESTART_DELAY=5
readonly RESTART_LIMIT=10
readonly RESTART_WINDOW=600

readonly IN_DIR="${BASE_DIR}/in"
readonly REQUEST="${IN_DIR}/request"
readonly STATE_DIR="${BASE_DIR}/state"
readonly STATUS_FILE="${STATE_DIR}/status.json"
readonly PRIVATE_DIR="${BASE_DIR}/private"
readonly TMPFS_OPTIONS=size=64k,mode=0700,nosuid,nodev,noexec
readonly REQUEST_SIZE=256
readonly MAX_SEQ=999999999
SELF="$(readlink -f -- "${BASH_SOURCE[0]}")"
readonly SELF

# Zeichenregeln. Nur Werte, die hierauf passen, gelangen in status.json.
readonly CONTAINER_RE='^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
readonly SHARE_RE='^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
readonly ID_RE='^[0-9a-f]{64}$'
readonly INSTANCE_RE='^[0-9a-f]{16}$'
readonly CODE_RE='^[a-z][a-z0-9_]{0,39}$'
readonly COMPRESSION_RE='^[a-z0-9-]{1,20}$'
readonly NUMBER_RE='^[0-9]{1,20}$'
# Ein Auftrag: genau diese Form, mit Leerzeichen auf 255 Zeichen aufgefüllt, dann "\n".
readonly ORDER_RE='^TRSG1 ([1-9][0-9]{0,8}) ((begin|ping|snapshot|finish|prepare|confirm|send|close)|stop ([A-Za-z0-9][A-Za-z0-9_.-]{0,127})) ([0-9a-f]{16}) *$'

# --- Hilfen ------------------------------------------------------------------------------

# NOW: Uhrzeit (Unix-Sekunden) für Zeitpunkte im Status und im Log.
# UP: Laufzeit des Systems in Sekunden für Fristen, Abstände und Pausen.
NOW=0
UP=0
now() {
  local uptime rest
  printf -v NOW '%(%s)T' -1
  read -r uptime rest <"${UPTIME}"
  UP="${uptime%%.*}"
}

# Nur feste Texte und geprüfte Werte ins Syslog.
log() {
  "${LOGGER}" -t twinraid-source-guard -- "$1" 2>/dev/null || true
}

# Höchstens einmal pro LOG_INTERVAL je Schlüssel.
declare -A LOG_LAST=()
log_limited() {
  local key="$1" last="${LOG_LAST[$1]:--1}"
  now
  if ((last < 0 || UP - last >= LOG_INTERVAL)); then
    LOG_LAST[${key}]="${UP}"
    log "$2"
  fi
}

die() {
  printf 'source-guard: %s\n' "$1" >&2
  exit "${2:-1}"
}

need_root() {
  if ((EUID != OWNER_UID)); then
    die "muss als root laufen"
  fi
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

# Datei atomar schreiben: temporäre Datei im selben Ordner, dann mv.
write_atomic() {
  local target="$1" content="$2" mode="$3" tmp
  tmp="$(mktemp -- "${target%/*}/.${target##*/}.XXXXXX")" || return 1
  if ! printf '%s' "${content}" >"${tmp}" || ! chmod "${mode}" -- "${tmp}" ||
    ! mv -f -- "${tmp}" "${target}"; then
    rm -f -- "${tmp}"
    return 1
  fi
}

# Ordner anlegen bzw. prüfen: kein Symlink, Ordner, Besitzer OWNER_UID, Rechte mode.
ensure_dir() {
  local dir="$1" mode="$2" owner
  if [[ -L "${dir}" ]]; then
    return 1
  fi
  if [[ ! -e "${dir}" ]]; then
    mkdir -m "${mode}" -- "${dir}" || return 1
  fi
  if [[ ! -d "${dir}" ]]; then
    return 1
  fi
  owner="$(stat -c '%u' -- "${dir}")" || return 1
  if [[ "${owner}" != "${OWNER_UID}" ]]; then
    return 1
  fi
  chmod "${mode}" -- "${dir}"
}

# --- JSON nur aus geprüften Werten -------------------------------------------------------

# Setzt JSON auf "\"wert\"", wenn der Wert auf das Muster passt, sonst auf null.
json_token() {
  if [[ -n "$1" && "$1" =~ $2 ]]; then
    JSON="\"$1\""
  else
    JSON=null
  fi
}

json_number() {
  if [[ "$1" =~ ^(0|[1-9][0-9]{0,18})$ ]]; then
    JSON="$1"
  else
    JSON=null
  fi
}

# --- Konfiguration -----------------------------------------------------------------------

ALLOW=()
DENY=()
DATASETS=()
MAX_STOP_MINUTES=20
HEARTBEAT_TIMEOUT=120
STOP_TIMEOUT=60
MIN_RUN_INTERVAL=30
MAX_DELETIONS=1000
TARGET_HOST=""
TARGET_PORT=22
SNAPSHOT_KEEP_DAYS=7
SNAPSHOT_RESERVE_PERCENT=15
CONFIG_ERROR=""
CONFIG_DETAIL=""
CONFIG_LINE=0

# Bei einem Fehler gilt nichts aus der Datei: keine Freigaben, keine Datasets.
config_fail() {
  CONFIG_ERROR=config_invalid
  CONFIG_DETAIL="$1"
  CONFIG_LINE="$2"
  ALLOW=()
  DENY=()
  DATASETS=()
  return 1
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

valid_host() {
  local host="$1" part
  if [[ "${host}" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
    for part in "${BASH_REMATCH[@]:1}"; do
      if ((10#${part} > 255)); then
        return 1
      fi
    done
    return 0
  fi
  ((${#host} <= 253)) &&
    [[ "${host}" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$ ]]
}

# Liest source-guard.conf. Beim ersten Fehler: CONFIG_ERROR, CONFIG_DETAIL, CONFIG_LINE.
load_config() {
  local line key value number=0 owner seen=" " lower
  CONFIG_ERROR=""
  CONFIG_DETAIL=""
  CONFIG_LINE=0
  ALLOW=()
  DENY=()
  DATASETS=()
  if [[ -L "${CONF}" || ! -f "${CONF}" ]]; then
    config_fail missing_file 0
    return 1
  fi
  owner="$(stat -c '%u' -- "${CONF}")" || owner=""
  if [[ "${owner}" != "${OWNER_UID}" ]]; then
    config_fail bad_owner 0
    return 1
  fi
  while IFS= read -r line || [[ -n "${line}" ]]; do
    number=$((number + 1))
    if ((number > 200)); then
      config_fail too_long "${number}"
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
    if [[ ! "${line}" =~ ^([a-z_]{1,40})[[:space:]]+([^[:space:]]{1,253})$ ]]; then
      config_fail syntax "${number}"
      return 1
    fi
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    case "${key}" in
      allow | deny | dataset) ;;
      *)
        if [[ "${seen}" == *" ${key} "* ]]; then
          config_fail duplicate "${number}"
          return 1
        fi
        seen+="${key} "
        ;;
    esac
    case "${key}" in
      allow | deny)
        if [[ ! "${value}" =~ ${CONTAINER_RE} ]]; then
          config_fail bad_value "${number}"
          return 1
        fi
        if in_list "${value}" "${ALLOW[@]+"${ALLOW[@]}"}" "${DENY[@]+"${DENY[@]}"}"; then
          config_fail duplicate "${number}"
          return 1
        fi
        if [[ "${key}" == allow ]]; then
          ALLOW+=("${value}")
        else
          DENY+=("${value}")
        fi
        if ((${#ALLOW[@]} + ${#DENY[@]} > 50)); then
          config_fail too_many "${number}"
          return 1
        fi
        ;;
      dataset)
        if [[ ! "${value}" =~ ${SHARE_RE} ]]; then
          config_fail bad_value "${number}"
          return 1
        fi
        lower="${value,,}"
        if [[ "${lower}" == system || "${lower}" == domains ]]; then
          config_fail forbidden_dataset "${number}"
          return 1
        fi
        if in_list "${value}" "${DATASETS[@]+"${DATASETS[@]}"}"; then
          config_fail duplicate "${number}"
          return 1
        fi
        DATASETS+=("${value}")
        if ((${#DATASETS[@]} > 20)); then
          config_fail too_many "${number}"
          return 1
        fi
        ;;
      max_stop_minutes)
        int_in "${value}" 5 120 || config_fail bad_value "${number}" || return 1
        MAX_STOP_MINUTES="${INT}"
        ;;
      heartbeat_timeout_seconds)
        int_in "${value}" 30 600 || config_fail bad_value "${number}" || return 1
        HEARTBEAT_TIMEOUT="${INT}"
        ;;
      stop_timeout_seconds)
        int_in "${value}" 10 600 || config_fail bad_value "${number}" || return 1
        STOP_TIMEOUT="${INT}"
        ;;
      min_run_interval_minutes)
        int_in "${value}" 10 1440 || config_fail bad_value "${number}" || return 1
        MIN_RUN_INTERVAL="${INT}"
        ;;
      max_deletions)
        int_in "${value}" 0 10000000 || config_fail bad_value "${number}" || return 1
        MAX_DELETIONS="${INT}"
        ;;
      target_host)
        valid_host "${value}" || config_fail bad_value "${number}" || return 1
        TARGET_HOST="${value}"
        ;;
      target_port)
        int_in "${value}" 1 65535 || config_fail bad_value "${number}" || return 1
        TARGET_PORT="${INT}"
        ;;
      snapshot_keep_days)
        int_in "${value}" 1 365 || config_fail bad_value "${number}" || return 1
        SNAPSHOT_KEEP_DAYS="${INT}"
        ;;
      snapshot_reserve_percent)
        int_in "${value}" 5 50 || config_fail bad_value "${number}" || return 1
        SNAPSHOT_RESERVE_PERCENT="${INT}"
        ;;
      *)
        config_fail unknown_key "${number}"
        return 1
        ;;
    esac
  done <"${CONF}"
}

# --- Docker ------------------------------------------------------------------------------

DOCKER_OK=0

docker_call() {
  timeout --kill-after=2 "${DOCKER_TIMEOUT}" "${DOCKER}" "$@"
}

docker_check() {
  local version
  DOCKER_OK=0
  if [[ ! -x "${DOCKER}" ]]; then
    return 1
  fi
  version="$(docker_call info --format '{{.ServerVersion}}' 2>/dev/null)" || return 1
  if [[ -n "${version}" ]]; then
    DOCKER_OK=1
  fi
}

# Container nach Name oder ID. Setzt C_ID, C_NAME und C_RUNNING (true|false).
# Mit expected muss der Name genau passen (kein Treffer über einen ID-Präfix).
# Rückgabe: 0 gefunden, 1 nicht gefunden, 2 Docker nicht erreichbar.
lookup_container() {
  local ref="$1" expected="${2:-}" out
  C_ID=""
  C_NAME=""
  C_RUNNING=""
  if ! out="$(docker_call inspect --type container --format '{{.Id}} {{.Name}} {{.State.Running}}' -- "${ref}" 2>/dev/null)"; then
    if docker_check; then
      return 1
    fi
    return 2
  fi
  if [[ ! "${out}" =~ ^([0-9a-f]{64})\ /([A-Za-z0-9][A-Za-z0-9_.-]{0,127})\ (true|false)$ ]]; then
    return 1
  fi
  C_ID="${BASH_REMATCH[1]}"
  C_NAME="${BASH_REMATCH[2]}"
  C_RUNNING="${BASH_REMATCH[3]}"
  if [[ -n "${expected}" && "${C_NAME}" != "${expected}" ]]; then
    C_ID=""
    return 1
  fi
}

# Ist der Container TwinRaid? Merkmale: Image, Label, Einbindung unter BASE_DIR.
# Setzt SELF_BY (image|label|mount|""). Rückgabe: 0 ja, 1 nein, 2 unbekannt.
# Die Ausgabe von Docker wird nur verglichen, nie weitergegeben.
is_self() {
  local id="$1" out image label line first=1 second=1
  SELF_BY=""
  # "with" überspringt Container ganz ohne Labels.
  if ! out="$(docker_call inspect --type container --format '{{.Config.Image}}{{"\n"}}{{with .Config.Labels}}{{index . "org.opencontainers.image.source"}}{{end}}{{"\n"}}{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' -- "${id}" 2>/dev/null)"; then
    return 2
  fi
  while IFS= read -r line; do
    if ((first)); then
      first=0
      image="${line%%@*}"
      if [[ "${image##*/}" == *:* ]]; then
        image="${image%:*}"
      fi
      if [[ "${image}" == "${SELF_IMAGE}" || "${image}" == twinraid ]]; then
        SELF_BY=image
        return 0
      fi
      continue
    fi
    if ((second)); then
      second=0
      label="${line}"
      if [[ "${label}" == "${SELF_SOURCE}" ]]; then
        SELF_BY=label
        return 0
      fi
      continue
    fi
    if [[ "${line}" == "${BASE_DIR}" || "${line}" == "${BASE_DIR}/"* ||
      "${line}" == "${BASE_DIR_ALIAS}" || "${line}" == "${BASE_DIR_ALIAS}/"* ]]; then
      SELF_BY=mount
      return 0
    fi
  done <<<"${out}"
  return 1
}

# --- Zustand des Wächters (private/) -----------------------------------------------------

INSTANCE=""
LAST_SEQ=0
STATE_LOST=0
RUN_OPEN=0
# RUN_OPENED ist eine Uhrzeit; RUN_HEARTBEAT und RUN_MAX sind Fristen in Laufzeit (UP).
RUN_OPENED=0
RUN_HEARTBEAT=0
RUN_MAX=0
RUN_IDS=()
RUN_NAMES=()
LAST_RUN=0
LAST_RUN_OPENED=0
LAST_RUN_CLOSED=0
# Ende des letzten Laufs in Laufzeit (UP), für die Pause zwischen zwei Läufen; 0: keine Pause.
LAST_RUN_CLOSED_UP=0
LAST_RUN_REASON=""
LAST_RUN_IDS=()
LAST_RUN_NAMES=()
LAST_RUN_RESULTS=()

new_instance() {
  local hex
  hex="$(od -An -N8 -tx1 /dev/urandom)"
  INSTANCE="${hex//[[:space:]]/}"
}

save_seq() {
  write_atomic "${PRIVATE_DIR}/last_seq" "${LAST_SEQ}" 0600
}

save_run() {
  local text i
  if ((RUN_OPEN == 0)); then
    rm -f -- "${PRIVATE_DIR}/run"
    return 0
  fi
  text="opened ${RUN_OPENED}"$'\n'"heartbeat ${RUN_HEARTBEAT}"$'\n'"max ${RUN_MAX}"$'\n'
  for i in "${!RUN_IDS[@]}"; do
    text+="container ${RUN_IDS[i]} ${RUN_NAMES[i]}"$'\n'
  done
  write_atomic "${PRIVATE_DIR}/run" "${text}" 0600
}

load_run() {
  local key a b
  RUN_OPEN=0
  RUN_IDS=()
  RUN_NAMES=()
  if [[ ! -f "${PRIVATE_DIR}/run" ]]; then
    return 0
  fi
  while read -r key a b; do
    case "${key}" in
      opened | heartbeat | max)
        [[ "${a}" =~ ${NUMBER_RE} ]] || continue
        case "${key}" in
          opened) RUN_OPENED="${a}" ;;
          heartbeat) RUN_HEARTBEAT="${a}" ;;
          max) RUN_MAX="${a}" ;;
        esac
        ;;
      container)
        if [[ "${a}" =~ ${ID_RE} && "${b}" =~ ${CONTAINER_RE} ]]; then
          RUN_IDS+=("${a}")
          RUN_NAMES+=("${b}")
        fi
        ;;
    esac
  done <"${PRIVATE_DIR}/run"
  if ((RUN_MAX > 0)); then
    RUN_OPEN=1
  fi
}

save_last_run() {
  local text i
  text="opened ${LAST_RUN_OPENED}"$'\n'"closed ${LAST_RUN_CLOSED}"$'\n'"reason ${LAST_RUN_REASON}"$'\n'
  text+="closed_up ${LAST_RUN_CLOSED_UP}"$'\n'
  for i in "${!LAST_RUN_IDS[@]}"; do
    text+="container ${LAST_RUN_IDS[i]} ${LAST_RUN_NAMES[i]} ${LAST_RUN_RESULTS[i]}"$'\n'
  done
  write_atomic "${PRIVATE_DIR}/last_run" "${text}" 0600
}

load_last_run() {
  local key a b c
  LAST_RUN=0
  LAST_RUN_IDS=()
  LAST_RUN_NAMES=()
  LAST_RUN_RESULTS=()
  if [[ ! -f "${PRIVATE_DIR}/last_run" ]]; then
    return 0
  fi
  while read -r key a b c; do
    case "${key}" in
      opened) [[ "${a}" =~ ${NUMBER_RE} ]] && LAST_RUN_OPENED="${a}" ;;
      closed) [[ "${a}" =~ ${NUMBER_RE} ]] && LAST_RUN_CLOSED="${a}" ;;
      closed_up) [[ "${a}" =~ ${NUMBER_RE} ]] && LAST_RUN_CLOSED_UP="${a}" ;;
      reason) [[ "${a}" =~ ${CODE_RE} ]] && LAST_RUN_REASON="${a}" ;;
      container)
        if [[ "${a}" =~ ${ID_RE} && "${b}" =~ ${CONTAINER_RE} && "${c}" =~ ${CODE_RE} ]]; then
          LAST_RUN_IDS+=("${a}")
          LAST_RUN_NAMES+=("${b}")
          LAST_RUN_RESULTS+=("${c}")
        fi
        ;;
    esac
  done <"${PRIVATE_DIR}/last_run"
  if [[ -n "${LAST_RUN_REASON}" ]]; then
    LAST_RUN=1
  fi
}

# Zustand laden oder neu anlegen. Fehlt private/, aber die Auftragsdatei enthält schon
# etwas, ist der Zustand verloren (nur nach einem Eingriff von Hand): Der vorgefundene
# Auftrag wird nie ausgeführt (Konzept 3.6).
init_private() {
  local value line size
  if [[ -f "${PRIVATE_DIR}/instance" ]]; then
    value="$(<"${PRIVATE_DIR}/instance")"
    if [[ "${value}" =~ ${INSTANCE_RE} ]]; then
      INSTANCE="${value}"
      value=0
      if [[ -f "${PRIVATE_DIR}/last_seq" ]]; then
        value="$(<"${PRIVATE_DIR}/last_seq")"
      fi
      if [[ "${value}" =~ ^[0-9]{1,9}$ ]]; then
        LAST_SEQ=$((10#${value}))
      fi
      if [[ -f "${PRIVATE_DIR}/state_lost" ]]; then
        STATE_LOST=1
      fi
      load_run
      load_last_run
      return 0
    fi
  fi
  new_instance
  LAST_SEQ=0
  if [[ -f "${REQUEST}" && ! -L "${REQUEST}" ]]; then
    size="$(stat -c '%s' -- "${REQUEST}")" || size=0
    if ((size > 0)); then
      STATE_LOST=1
      if ((size == REQUEST_SIZE)); then
        IFS= read -r -N "${REQUEST_SIZE}" line <"${REQUEST}" || true
        if [[ "${line:0:255}" =~ ${ORDER_RE} ]]; then
          LAST_SEQ=$((10#${BASH_REMATCH[1]}))
        fi
      fi
      : >"${PRIVATE_DIR}/state_lost"
      log "state lost: found an order without saved state, it is discarded (discarded_at_start)"
    fi
  fi
  save_seq
  write_atomic "${PRIVATE_DIR}/instance" "${INSTANCE}" 0600
  log "new instance started"
}

# --- Ordner, tmpfs und Auftragsdatei -----------------------------------------------------

LAYOUT_ERROR=""
LAYOUT_DETAIL=""

layout_fail() {
  LAYOUT_ERROR="$1"
  LAYOUT_DETAIL="$2"
  log "layout problem: $2"
  return 1
}

# Legt die Ordner an, hängt das tmpfs für in/ ein und legt die Auftragsdatei an.
# Ein Ordner an der Stelle der Auftragsdatei (Docker legt ihn an, wenn TwinRaid vor dem
# Wächter startet) wird gemeldet, nie überdeckt (Konzept 3.6).
prepare_layout() {
  local fstype entries
  LAYOUT_ERROR=""
  LAYOUT_DETAIL=""
  ensure_dir "${BASE_DIR}" 0755 || layout_fail misconfigured base_unusable || return 1
  ensure_dir "${PRIVATE_DIR}" 0700 || layout_fail misconfigured private_unusable || return 1
  ensure_dir "${STATE_DIR}" 0755 || layout_fail misconfigured state_unusable || return 1
  if [[ -L "${IN_DIR}" || (-e "${IN_DIR}" && ! -d "${IN_DIR}") ]]; then
    layout_fail misconfigured in_unusable
    return 1
  fi
  if [[ ! -e "${IN_DIR}" ]]; then
    mkdir -m 0700 -- "${IN_DIR}" || layout_fail misconfigured in_unusable || return 1
  fi
  fstype="$("${FINDMNT}" -n -o FSTYPE -M "${IN_DIR}" 2>/dev/null)" || fstype=""
  if [[ -z "${fstype}" ]]; then
    if [[ -L "${REQUEST}" || -e "${REQUEST}" ]]; then
      if [[ -d "${REQUEST}" && ! -L "${REQUEST}" ]]; then
        layout_fail request_not_a_file request_is_directory || true
        log "request is a directory (TwinRaid started before the guard): stop TwinRaid, remove the directory, restart the guard, then start TwinRaid"
        return 1
      fi
      layout_fail misconfigured in_unexpected_content
      return 1
    fi
    entries="$(find "${IN_DIR}" -mindepth 1 -maxdepth 1 -print -quit)"
    if [[ -n "${entries}" ]]; then
      layout_fail misconfigured in_unexpected_content
      return 1
    fi
    if ! "${MOUNT}" -t tmpfs -o "${TMPFS_OPTIONS}" tmpfs "${IN_DIR}"; then
      layout_fail misconfigured mount_failed
      return 1
    fi
  elif [[ "${fstype}" != tmpfs ]]; then
    layout_fail misconfigured in_wrong_mount
    return 1
  fi
  if [[ -L "${REQUEST}" ]]; then
    layout_fail request_not_a_file request_is_symlink
    return 1
  fi
  if [[ -d "${REQUEST}" ]]; then
    layout_fail request_not_a_file request_is_directory || true
    log "request is a directory (TwinRaid started before the guard): stop TwinRaid, remove the directory, restart the guard, then start TwinRaid"
    return 1
  fi
  if [[ ! -e "${REQUEST}" ]]; then
    : >"${REQUEST}" || layout_fail misconfigured request_create_failed || return 1
    chmod 0600 -- "${REQUEST}"
  fi
  if [[ ! -f "${REQUEST}" ]]; then
    layout_fail request_not_a_file request_not_regular
    return 1
  fi
}

# --- Aufträge lesen ----------------------------------------------------------------------

REQUEST_ERROR=""
REQUEST_DETAIL=""
ORDER_LINE=""
LAST_REJECTED=""
REJECTED_COUNT=0
REJECTED_AT=0

# Prüft die Auftragsdatei vor jedem Lesen und liest genau REQUEST_SIZE Byte.
# Rückgabe: 0 Inhalt gelesen (ORDER_LINE), 1 leer, 2 Datei unbrauchbar, 3 zu groß.
read_request() {
  local info ftype fuid flinks fsize fchange
  ORDER_LINE=""
  REQUEST_ERROR=""
  REQUEST_DETAIL=""
  if [[ -L "${REQUEST}" ]]; then
    REQUEST_ERROR=request_not_a_file
    REQUEST_DETAIL=request_is_symlink
    return 2
  fi
  if [[ ! -e "${REQUEST}" ]]; then
    : >"${REQUEST}" || true
    chmod 0600 -- "${REQUEST}" 2>/dev/null || true
    log_limited request_missing "request file was missing and was created again; restart TwinRaid"
    return 1
  fi
  info="$(stat -c '%F|%u|%h|%s|%Z' -- "${REQUEST}")" || return 2
  IFS='|' read -r ftype fuid flinks fsize fchange <<<"${info}"
  if [[ "${ftype}" != "regular file" && "${ftype}" != "regular empty file" ]]; then
    REQUEST_ERROR=request_not_a_file
    REQUEST_DETAIL=request_not_regular
    if [[ "${ftype}" == directory ]]; then
      REQUEST_DETAIL=request_is_directory
    fi
    return 2
  fi
  if [[ "${fuid}" != "${OWNER_UID}" ]]; then
    REQUEST_ERROR=request_not_a_file
    REQUEST_DETAIL=request_bad_owner
    return 2
  fi
  if [[ "${flinks}" != 1 ]]; then
    REQUEST_ERROR=request_not_a_file
    REQUEST_DETAIL=request_hardlink
    return 2
  fi
  if [[ "${fsize}" == 0 ]]; then
    return 1
  fi
  if ((fsize > REQUEST_SIZE)); then
    ORDER_LINE="size:${fsize}:${fchange}"
    return 3
  fi
  IFS= read -r -N "${REQUEST_SIZE}" ORDER_LINE <"${REQUEST}" || true
  return 0
}

# Prüft eine gelesene Zeile. Setzt O_SEQ, O_ORDER und O_CONTAINER.
parse_order() {
  local line="$1" body payload sum
  O_SEQ=0
  O_ORDER=""
  O_CONTAINER=""
  if ((${#line} != REQUEST_SIZE)) || [[ "${line: -1}" != $'\n' ]]; then
    return 1
  fi
  body="${line:0:255}"
  if [[ ! "${body}" =~ ${ORDER_RE} ]]; then
    return 1
  fi
  O_SEQ=$((10#${BASH_REMATCH[1]}))
  payload="TRSG1 ${BASH_REMATCH[1]} ${BASH_REMATCH[2]}"
  O_ORDER="${BASH_REMATCH[3]:-stop}"
  O_CONTAINER="${BASH_REMATCH[4]}"
  sum="$(printf '%s' "${payload}" | sha256sum)"
  if [[ "${sum:0:16}" != "${BASH_REMATCH[5]}" ]]; then
    return 1
  fi
}

reject() {
  local key="$1"
  if [[ "${key}" == "${LAST_REJECTED}" ]]; then
    return 0
  fi
  LAST_REJECTED="${key}"
  REJECTED_COUNT=$((REJECTED_COUNT + 1))
  now
  REJECTED_AT="${NOW}"
  log_limited rejected "order rejected (rejected_format)"
  STATUS_DIRTY=1
}

# --- Läufe -------------------------------------------------------------------------------

LAST_DONE_SEQ=0
LAST_DONE_ORDER=""
LAST_DONE_CONTAINER=""
LAST_DONE_RESULT=""
LAST_DONE_AT=0

open_run() {
  now
  RUN_OPEN=1
  RUN_OPENED="${NOW}"
  RUN_HEARTBEAT=$((UP + HEARTBEAT_TIMEOUT))
  RUN_MAX=$((UP + MAX_STOP_MINUTES * MINUTE))
  RUN_IDS=()
  RUN_NAMES=()
  save_run
}

# Startet die gemerkten Container in umgekehrter Reihenfolge und schließt den Lauf.
# Ist Docker nicht erreichbar, bleibt der Lauf offen (Rückgabe 1).
close_run() {
  local reason="$1" i id name result rc
  if ((RUN_OPEN == 0)); then
    return 0
  fi
  if ! docker_check; then
    log_limited close_postponed "run cannot be closed yet: docker unavailable"
    return 1
  fi
  LAST_RUN_IDS=()
  LAST_RUN_NAMES=()
  LAST_RUN_RESULTS=()
  for ((i = ${#RUN_IDS[@]} - 1; i >= 0; i--)); do
    id="${RUN_IDS[i]}"
    name="${RUN_NAMES[i]}"
    rc=0
    lookup_container "${id}" || rc=$?
    if ((rc == 1)); then
      result=gone
      rc=0
      lookup_container "${name}" "${name}" || rc=$?
      if ((rc == 0)); then
        result=changed
      fi
    elif ((rc == 2)); then
      result=start_failed
    elif [[ "${C_RUNNING}" == true ]]; then
      result=already_running
    else
      result=start_failed
      if docker_call start -- "${id}" >/dev/null 2>&1 && lookup_container "${id}" &&
        [[ "${C_RUNNING}" == true ]]; then
        result=started
      fi
    fi
    LAST_RUN_IDS+=("${id}")
    LAST_RUN_NAMES+=("${name}")
    LAST_RUN_RESULTS+=("${result}")
    log "container ${name}: ${result}"
  done
  now
  LAST_RUN=1
  LAST_RUN_OPENED="${RUN_OPENED}"
  LAST_RUN_CLOSED="${NOW}"
  LAST_RUN_CLOSED_UP="${UP}"
  LAST_RUN_REASON="${reason}"
  save_last_run
  RUN_OPEN=0
  RUN_IDS=()
  RUN_NAMES=()
  save_run
  log "run closed (${reason})"
  STATUS_DIRTY=1
}

# Sekunden, bis wieder ein Lauf geöffnet werden darf (Pause nach dem letzten Lauf). Setzt WAIT.
run_wait() {
  local pause=$((MIN_RUN_INTERVAL * MINUTE))
  WAIT=0
  now
  # Ein Ende "in der Zukunft" stammt aus einem früheren Systemstart und zählt nicht.
  if ((LAST_RUN_CLOSED_UP > 0 && LAST_RUN_CLOSED_UP <= UP)); then
    WAIT=$((LAST_RUN_CLOSED_UP + pause - UP))
    if ((WAIT < 0)); then
      WAIT=0
    fi
  fi
}

# source-guard.sh reset-run-interval legt diese Datei an; nur root auf Klein kann das.
check_reset_request() {
  if [[ -f "${PRIVATE_DIR}/reset-run-interval" ]]; then
    rm -f -- "${PRIVATE_DIR}/reset-run-interval"
    LAST_RUN_CLOSED_UP=0
    if ((LAST_RUN)); then
      save_last_run
    fi
    log "pause between runs lifted by root on Klein"
    STATUS_DIRTY=1
  fi
}

check_deadman() {
  if ((RUN_OPEN == 0)); then
    return 0
  fi
  now
  if ((UP >= RUN_MAX)); then
    log_limited deadman "deadman: maximum time of the run exceeded"
    close_run deadman_max || true
  elif ((UP >= RUN_HEARTBEAT)); then
    log_limited deadman "deadman: no heartbeat from TwinRaid"
    close_run deadman_heartbeat || true
  fi
}

order_stop() {
  local name="$1" rc=0 self_rc=0
  if ((RUN_OPEN == 0)); then
    RESULT=no_run
    return 0
  fi
  if ! in_list "${name}" "${ALLOW[@]+"${ALLOW[@]}"}" ||
    in_list "${name}" "${DENY[@]+"${DENY[@]}"}"; then
    RESULT=not_allowed
    return 0
  fi
  if ! docker_check; then
    RESULT=docker_unavailable
    return 0
  fi
  lookup_container "${name}" "${name}" || rc=$?
  if ((rc == 1)); then
    RESULT=not_found
    return 0
  elif ((rc == 2)); then
    RESULT=docker_unavailable
    return 0
  fi
  is_self "${C_ID}" || self_rc=$?
  if ((self_rc != 1)); then
    # 0: TwinRaid selbst; 2: unbekannt. In beiden Fällen nie anhalten.
    RESULT=denied_self
    log "refused to stop ${name}: it may be TwinRaid itself"
    return 0
  fi
  if [[ "${C_RUNNING}" != true ]]; then
    RESULT=not_running
    return 0
  fi
  # Erst merken, dann anhalten: Stirbt der Wächter dazwischen, startet finish ihn wieder.
  if ! in_list "${C_ID}" "${RUN_IDS[@]+"${RUN_IDS[@]}"}"; then
    RUN_IDS+=("${C_ID}")
    RUN_NAMES+=("${name}")
    save_run
  fi
  timeout --kill-after=5 $((STOP_TIMEOUT + STOP_EXTRA)) \
    "${DOCKER}" stop -t "${STOP_TIMEOUT}" -- "${C_ID}" >/dev/null 2>&1 || true
  RESULT=stop_failed
  if lookup_container "${C_ID}" && [[ "${C_RUNNING}" == false ]]; then
    RESULT=stopped
  fi
}

# Führt einen angenommenen Auftrag aus. Setzt RESULT.
execute_order() {
  local order="$1" container="$2" blocking
  RESULT=""
  current_error
  blocking="${ERROR}"
  case "${blocking}" in
    config_invalid | misconfigured | request_not_a_file)
      RESULT="${blocking}"
      return 0
      ;;
  esac
  case "${order}" in
    begin)
      run_wait
      if ((RUN_OPEN)); then
        RESULT=run_open
      elif ((WAIT > 0)); then
        RESULT=too_soon
      elif ! docker_check; then
        RESULT=docker_unavailable
      else
        open_run
        RESULT=opened
      fi
      ;;
    ping)
      if ((RUN_OPEN)); then
        now
        RUN_HEARTBEAT=$((UP + HEARTBEAT_TIMEOUT))
        save_run
        RESULT=extended
      else
        RESULT=no_run
      fi
      ;;
    stop)
      order_stop "${container}"
      ;;
    finish)
      if ((RUN_OPEN == 0)); then
        RESULT=no_run
      elif close_run finish; then
        RESULT=finished
      else
        RESULT=docker_unavailable
      fi
      ;;
    *)
      RESULT=not_supported
      ;;
  esac
}

handle_request() {
  local rc=0 line
  read_request || rc=$?
  case "${rc}" in
    1) return 0 ;;
    2)
      log_limited request_error "request file unusable (${REQUEST_DETAIL:-unreadable})"
      STATUS_DIRTY=1
      return 0
      ;;
    3)
      reject "${ORDER_LINE}"
      return 0
      ;;
  esac
  line="${ORDER_LINE}"
  if ! parse_order "${line}"; then
    # Vielleicht gerade halb geschrieben: einmal kurz warten und neu lesen.
    sleep "${REREAD_DELAY}"
    rc=0
    read_request || rc=$?
    if ((rc != 0)); then
      return 0
    fi
    line="${ORDER_LINE}"
    if ! parse_order "${line}"; then
      reject "$(printf '%s' "${line}" | sha256sum)"
      return 0
    fi
  fi
  if ((O_SEQ <= LAST_SEQ)); then
    return 0
  fi
  # Erst die Nummer sichern, dann prüfen und ausführen.
  LAST_SEQ="${O_SEQ}"
  save_seq
  LAST_REJECTED=""
  execute_order "${O_ORDER}" "${O_CONTAINER}"
  now
  LAST_DONE_SEQ="${O_SEQ}"
  LAST_DONE_ORDER="${O_ORDER}"
  LAST_DONE_CONTAINER="${O_CONTAINER}"
  LAST_DONE_RESULT="${RESULT}"
  LAST_DONE_AT="${NOW}"
  if [[ "${O_ORDER}" == stop ]]; then
    log "order ${O_SEQ}: stop ${O_CONTAINER}: ${RESULT}"
  else
    log "order ${O_SEQ}: ${O_ORDER}: ${RESULT}"
  fi
  STATUS_DIRTY=1
}

# --- Beobachtung: Container, ZFS, Unraid Connect ----------------------------------------

ALLOWED_IDS=()
ALLOWED_STATES=()
RUNTIME_ERROR=""

refresh_containers() {
  local i name rc self_rc
  ALLOWED_IDS=()
  ALLOWED_STATES=()
  RUNTIME_ERROR=""
  for i in "${!ALLOW[@]}"; do
    name="${ALLOW[i]}"
    ALLOWED_IDS+=("")
    ALLOWED_STATES+=(unknown)
    if ((DOCKER_OK == 0)); then
      continue
    fi
    rc=0
    lookup_container "${name}" "${name}" || rc=$?
    if ((rc == 1)); then
      ALLOWED_STATES[i]=missing
      continue
    elif ((rc == 2)); then
      continue
    fi
    ALLOWED_IDS[i]="${C_ID}"
    ALLOWED_STATES[i]=stopped
    if [[ "${C_RUNNING}" == true ]]; then
      ALLOWED_STATES[i]=running
    fi
    self_rc=0
    is_self "${C_ID}" || self_rc=$?
    if ((self_rc == 0)); then
      ALLOWED_STATES[i]=self
      RUNTIME_ERROR=allow_contains_twinraid
      log_limited allow_self "config invalid: allow contains TwinRaid (${name})"
    fi
  done
}

ZFS_STATE=unavailable
POOL_USED=""
POOL_AVAILABLE=""
DS_PRESENT=()
DS_REFERENCED=()
DS_COMPRESSION=()
SELF_DATASET_STATE=unknown

zfs_call() {
  timeout --kill-after=2 "${DOCKER_TIMEOUT}" "$@"
}

# Mount-Ziel /config oder /data eines TwinRaid-Containers liegt im eigenen Dataset?
path_in_dataset() {
  local source="$1" mountpoint="$2"
  if [[ "${source}" == /mnt/user/* ]]; then
    source="/mnt/${POOL}/${source#/mnt/user/}"
  fi
  [[ "${source}" == "${mountpoint}" || "${source}" == "${mountpoint}/"* ]]
}

# Eigenes Dataset von TwinRaid (E18): found, missing, outside, no_container oder unknown.
check_self_dataset() {
  local out name type mountpoint ids id dest source found=0 inside line config data
  SELF_DATASET_STATE=unknown
  if ! out="$(zfs_call "${ZFS}" list -H -o name,type,mountpoint -- "${SELF_DATASET}" 2>/dev/null)"; then
    SELF_DATASET_STATE=missing
    return 0
  fi
  IFS=$'\t' read -r name type mountpoint <<<"${out}"
  if [[ "${name}" != "${SELF_DATASET}" || "${type}" != filesystem ||
    ! "${mountpoint}" =~ ^/[A-Za-z0-9._/-]{1,255}$ ]]; then
    SELF_DATASET_STATE=missing
    return 0
  fi
  if ((DOCKER_OK == 0)); then
    return 0
  fi
  ids="$(docker_call ps -a -q --no-trunc 2>/dev/null)" || return 0
  inside=1
  while IFS= read -r id; do
    if [[ ! "${id}" =~ ${ID_RE} ]]; then
      continue
    fi
    # Nur über Image oder Label erkannte Container zählen. Ein Container, der nur die
    # Ordner des Wächters einbindet, ist kein Beweis für TwinRaid; er wird übersprungen.
    # Bleibt kein TwinRaid übrig, ergibt das no_container, und appdata wird nicht gesendet:
    # die vorsichtige Richtung.
    if ! is_self "${id}" || [[ "${SELF_BY}" == mount ]]; then
      continue
    fi
    found=1
    config=0
    data=0
    out="$(docker_call inspect --type container --format '{{range .Mounts}}{{.Destination}}{{"\t"}}{{.Source}}{{"\n"}}{{end}}' -- "${id}" 2>/dev/null)" || return 0
    while IFS= read -r line; do
      dest="${line%%$'\t'*}"
      source="${line#*$'\t'}"
      case "${dest}" in
        /config | /data)
          if path_in_dataset "${source}" "${mountpoint}"; then
            if [[ "${dest}" == /config ]]; then config=1; else data=1; fi
          fi
          ;;
      esac
    done <<<"${out}"
    if ((config == 0 || data == 0)); then
      inside=0
    fi
  done <<<"${ids}"
  if ((found == 0)); then
    SELF_DATASET_STATE=no_container
  elif ((inside)); then
    SELF_DATASET_STATE=found
  else
    SELF_DATASET_STATE=outside
  fi
}

refresh_zfs() {
  local out used available ds name type referenced compression
  ZFS_STATE=unavailable
  POOL_USED=""
  POOL_AVAILABLE=""
  DS_PRESENT=()
  DS_REFERENCED=()
  DS_COMPRESSION=()
  SELF_DATASET_STATE=unknown
  if [[ ! -x "${ZFS}" || ! -x "${ZPOOL}" ]]; then
    return 0
  fi
  out="$(zfs_call "${ZPOOL}" list -H -o name -- "${POOL}" 2>/dev/null)" || return 0
  if [[ "${out}" != "${POOL}" ]]; then
    return 0
  fi
  ZFS_STATE=ok
  if out="$(zfs_call "${ZFS}" list -H -p -o used,available -- "${POOL}" 2>/dev/null)" &&
    [[ "${out}" =~ ^([0-9]{1,20})$'\t'([0-9]{1,20})$ ]]; then
    used="${BASH_REMATCH[1]}"
    available="${BASH_REMATCH[2]}"
    POOL_USED="${used}"
    POOL_AVAILABLE="${available}"
  fi
  for ds in "${DATASETS[@]+"${DATASETS[@]}"}"; do
    DS_PRESENT+=(false)
    DS_REFERENCED+=("")
    DS_COMPRESSION+=("")
    if out="$(zfs_call "${ZFS}" list -H -p -o name,type,referenced,compression -- "${POOL}/${ds}" 2>/dev/null)"; then
      IFS=$'\t' read -r name type referenced compression <<<"${out}"
      if [[ "${name}" == "${POOL}/${ds}" && "${type}" == filesystem ]]; then
        DS_PRESENT[-1]=true
        DS_REFERENCED[-1]="${referenced}"
        DS_COMPRESSION[-1]="${compression}"
      fi
    fi
  done
  check_self_dataset
}

CONNECT=absent
FLASH_BACKUP=inactive

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

# --- Status ------------------------------------------------------------------------------

ERROR=""
ERROR_DETAIL=""
STARTED_AT=0
STATUS_DIRTY=1

# Fehler mit Vorrang: Ablage, Konfiguration, Auftragsdatei, verlorener Zustand, Docker.
current_error() {
  ERROR=""
  ERROR_DETAIL=""
  if [[ -n "${LAYOUT_ERROR}" ]]; then
    ERROR="${LAYOUT_ERROR}"
    ERROR_DETAIL="${LAYOUT_DETAIL}"
  elif [[ -n "${CONFIG_ERROR}" ]]; then
    ERROR="${CONFIG_ERROR}"
    ERROR_DETAIL="${CONFIG_DETAIL}"
  elif [[ -n "${RUNTIME_ERROR}" ]]; then
    ERROR=config_invalid
    ERROR_DETAIL="${RUNTIME_ERROR}"
  elif [[ -n "${REQUEST_ERROR}" ]]; then
    ERROR="${REQUEST_ERROR}"
    ERROR_DETAIL="${REQUEST_DETAIL}"
  elif ((STATE_LOST)); then
    ERROR=state_lost
  elif ((DOCKER_OK == 0)); then
    ERROR=docker_unavailable
  fi
}

# Ein Eintrag {"name":…,"id":…[,"result":…]} aus geprüften Werten. Setzt JSON.
container_json() {
  local item
  json_token "$1" "${CONTAINER_RE}"
  item='{"name":'"${JSON}"
  json_token "$2" "${ID_RE}"
  item+=',"id":'"${JSON}"
  if (($# > 2)); then
    json_token "$3" "${CODE_RE}"
    item+=',"result":'"${JSON}"
  fi
  JSON="${item}}"
}

write_status() {
  local json item i list="" present
  now
  current_error
  json='{"protocol":'"${PROTOCOL}"',"version":'"${VERSION}"
  json+=',"heartbeat_at":'"${NOW}"',"started_at":'"${STARTED_AT}"
  json_token "${INSTANCE}" "${INSTANCE_RE}"
  json+=',"instance":'"${JSON}"
  json_token "${ERROR}" "${CODE_RE}"
  json+=',"error":'"${JSON}"
  json_token "${ERROR_DETAIL}" "${CODE_RE}"
  json+=',"error_detail":'"${JSON}"
  json_number "${CONFIG_LINE}"
  json+=',"config_line":'"${JSON}"
  json+=',"last_seq":'"${LAST_SEQ}"
  if ((LAST_DONE_SEQ > 0)); then
    json_token "${LAST_DONE_ORDER}" "${CODE_RE}"
    item='{"seq":'"${LAST_DONE_SEQ}"',"order":'"${JSON}"
    json_token "${LAST_DONE_CONTAINER}" "${CONTAINER_RE}"
    item+=',"container":'"${JSON}"
    json_token "${LAST_DONE_RESULT}" "${CODE_RE}"
    item+=',"result":'"${JSON}"',"at":'"${LAST_DONE_AT}"'}'
    json+=',"last":'"${item}"
  else
    json+=',"last":null'
  fi
  json_number "${REJECTED_AT}"
  if ((REJECTED_AT == 0)); then
    JSON=null
  fi
  json+=',"rejected":{"count":'"${REJECTED_COUNT}"',"last_at":'"${JSON}"'}'
  json+=',"host":{"connect":"'"${CONNECT}"'","flash_backup":"'"${FLASH_BACKUP}"'"}'
  json+=',"limits":{"max_stop_minutes":'"${MAX_STOP_MINUTES}"
  json+=',"heartbeat_timeout_seconds":'"${HEARTBEAT_TIMEOUT}"
  json+=',"stop_timeout_seconds":'"${STOP_TIMEOUT}"',"min_run_interval_minutes":'"${MIN_RUN_INTERVAL}"
  json+=',"max_deletions":'"${MAX_DELETIONS}"
  json+=',"snapshot_keep_days":'"${SNAPSHOT_KEEP_DAYS}"
  json+=',"snapshot_reserve_percent":'"${SNAPSHOT_RESERVE_PERCENT}"'}'
  # Ziel für das Senden (ab v0.5.1): nur ob eingetragen und der Port, keine Adresse.
  if [[ -n "${TARGET_HOST}" ]]; then
    json+=',"target":{"configured":true,"port":'"${TARGET_PORT}"'}'
  else
    json+=',"target":{"configured":false,"port":'"${TARGET_PORT}"'}'
  fi
  for i in "${!ALLOW[@]}"; do
    json_token "${ALLOW[i]}" "${CONTAINER_RE}"
    item='{"name":'"${JSON}"
    json_token "${ALLOWED_IDS[i]:-}" "${ID_RE}"
    item+=',"id":'"${JSON}"
    json_token "${ALLOWED_STATES[i]:-unknown}" "${CODE_RE}"
    item+=',"state":'"${JSON}"'}'
    list+="${list:+,}${item}"
  done
  json+=',"allowed":['"${list}"']'
  if ((RUN_OPEN)); then
    list=""
    for i in "${!RUN_IDS[@]}"; do
      container_json "${RUN_NAMES[i]}" "${RUN_IDS[i]}"
      list+="${list:+,}${JSON}"
    done
    # Fristen laufen in Laufzeit; im Status stehen sie als Uhrzeit.
    json+=',"run":{"opened_at":'"${RUN_OPENED}"',"heartbeat_deadline":'"$((NOW + RUN_HEARTBEAT - UP))"
    json+=',"max_deadline":'"$((NOW + RUN_MAX - UP))"',"stopped":['"${list}"']}'
  else
    json+=',"run":null'
  fi
  run_wait
  if ((WAIT > 0)); then
    json+=',"run_allowed_at":'"$((NOW + WAIT))"
  else
    json+=',"run_allowed_at":null'
  fi
  if ((LAST_RUN)); then
    list=""
    for i in "${!LAST_RUN_IDS[@]}"; do
      container_json "${LAST_RUN_NAMES[i]}" "${LAST_RUN_IDS[i]}" "${LAST_RUN_RESULTS[i]}"
      list+="${list:+,}${JSON}"
    done
    item='{"opened_at":'"${LAST_RUN_OPENED}"',"closed_at":'"${LAST_RUN_CLOSED}"
    json_token "${LAST_RUN_REASON}" "${CODE_RE}"
    item+=',"reason":'"${JSON}"',"containers":['"${list}"']}'
    json+=',"last_run":'"${item}"
  else
    json+=',"last_run":null'
  fi
  json+=',"zfs":{"state":"'"${ZFS_STATE}"'"'
  if [[ -n "${POOL_USED}" ]]; then
    json_number "${POOL_USED}"
    item='{"used":'"${JSON}"
    json_number "${POOL_AVAILABLE}"
    item+=',"available":'"${JSON}"'}'
    json+=',"pool":'"${item}"
  else
    json+=',"pool":null'
  fi
  list=""
  for i in "${!DATASETS[@]}"; do
    json_token "${DATASETS[i]}" "${SHARE_RE}"
    # Ohne ZFS ist unbekannt, ob es das Dataset gibt.
    present=null
    if [[ "${ZFS_STATE}" == ok ]]; then
      present="${DS_PRESENT[i]:-false}"
    fi
    item='{"name":'"${JSON}"',"present":'"${present}"
    json_number "${DS_REFERENCED[i]:-}"
    item+=',"referenced":'"${JSON}"
    json_token "${DS_COMPRESSION[i]:-}" "${COMPRESSION_RE}"
    item+=',"compression":'"${JSON}"'}'
    list+="${list:+,}${item}"
  done
  json+=',"datasets":['"${list}"'],"self_dataset":"'"${SELF_DATASET_STATE}"'"}'
  if ((LAST_SEQ >= MAX_SEQ)); then
    json+=',"seq_exhausted":true'
  else
    json+=',"seq_exhausted":false'
  fi
  json+='}'
  if ! write_atomic "${STATUS_FILE}" "${json}"$'\n' 0644; then
    log_limited status_write "status.json could not be written"
  fi
  STATUS_DIRTY=0
}

# --- Hauptschleife -----------------------------------------------------------------------

STOP_REQUESTED=0

on_term() {
  STOP_REQUESTED=1
}

# Aktueller Elternprozess (Feld 4 in /proc/self/stat, nach dem Namen in Klammern).
parent_pid() {
  local stat
  stat="$(<"/proc/$$/stat")"
  stat="${stat##*) }"
  read -r _ PARENT _ <<<"${stat}"
}

run_guard() {
  local next_docker=0 next_containers=0 next_zfs=0 next_host=0 next_status=0
  local was_docker=-1 sleeper="" run_lock first_parent
  need_root
  now
  STARTED_AT="${NOW}"
  parent_pid
  first_parent="${PARENT}"
  prepare_layout || true
  ensure_dir "${PRIVATE_DIR}" 0700 || die "private/ ist nicht nutzbar"
  # Eigene Sperre: Nie lesen zwei Wächter gleichzeitig Aufträge, auch nicht kurz nach
  # einem Neustart, während ein alter Wächter noch endet.
  exec {run_lock}>>"${PRIVATE_DIR}/run.lock"
  if ! flock -w 10 "${run_lock}"; then
    log "another guard process still runs, ending"
    exit 1
  fi
  init_private
  if ! load_config; then
    log "config invalid: ${CONFIG_DETAIL} in line ${CONFIG_LINE}"
  fi
  trap on_term TERM INT
  log "started (version ${VERSION})"
  while ((STOP_REQUESTED == 0)); do
    # Fehlt die Überwachungsschleife (etwa nach kill -9), endet der Wächter ohne finish;
    # ein neues "start" übernimmt den gespeicherten Zustand.
    parent_pid
    if [[ "${PARENT}" != "${first_parent}" ]]; then
      log "supervisor is gone, ending"
      break
    fi
    check_reset_request
    now
    if ((NOW >= next_docker)); then
      docker_check || true
      if ((DOCKER_OK != was_docker)); then
        if ((DOCKER_OK)); then
          log "docker available"
          next_containers=0
        else
          log "docker unavailable, trying again"
        fi
        was_docker="${DOCKER_OK}"
        STATUS_DIRTY=1
      fi
      next_docker=$((NOW + DOCKER_INTERVAL))
    fi
    if ((NOW >= next_containers)); then
      refresh_containers
      next_containers=$((NOW + CONTAINER_INTERVAL))
    fi
    if ((NOW >= next_zfs)); then
      refresh_zfs
      next_zfs=$((NOW + ZFS_INTERVAL))
    fi
    if ((NOW >= next_host)); then
      connect_state
      flash_backup_state
      next_host=$((NOW + HOST_INTERVAL))
    fi
    check_deadman
    if [[ -z "${LAYOUT_ERROR}" ]]; then
      handle_request
    fi
    now
    if ((STATUS_DIRTY || NOW >= next_status)); then
      write_status
      next_status=$((NOW + STATUS_INTERVAL))
    fi
    sleep "${LOOP_SLEEP}" &
    sleeper=$!
    wait "${sleeper}" || true
  done
  if [[ -n "${sleeper}" ]]; then
    kill "${sleeper}" 2>/dev/null || true
  fi
  # Nur auf ausdrücklichen Befehl (stop) zuerst finish; nicht beim Herunterfahren.
  if [[ -f "${PRIVATE_DIR}/stop.request" ]] && ((RUN_OPEN)); then
    docker_check || true
    close_run stop_command || true
  fi
  write_status
  log "stopped"
}

# --- Start, Stopp, Überwachung -----------------------------------------------------------

supervisor_pid() {
  local pid
  SUP_PID=""
  if [[ ! -f "${PRIVATE_DIR}/supervisor.pid" ]]; then
    return 1
  fi
  pid="$(<"${PRIVATE_DIR}/supervisor.pid")"
  if [[ ! "${pid}" =~ ^[0-9]{1,9}$ || ! -r "/proc/${pid}/cmdline" ]]; then
    return 1
  fi
  if ! tr '\0' ' ' <"/proc/${pid}/cmdline" | grep -q -- ' supervise'; then
    return 1
  fi
  SUP_PID="${pid}"
}

supervise() {
  local lock child="" started rc times=() t kept
  need_root
  exec {lock}>>"${PRIVATE_DIR}/lock"
  if ! flock -n "${lock}"; then
    exit 0
  fi
  write_atomic "${PRIVATE_DIR}/supervisor.pid" "$$" 0600
  SUP_STOP=0
  trap 'SUP_STOP=1; if [[ -n "${child}" ]]; then kill -TERM "${child}" 2>/dev/null || true; fi' TERM INT
  while true; do
    # Die Sperre gehört nur der Überwachungsschleife und wird nicht an den Wächter vererbt.
    /bin/bash "${SELF}" run {lock}>&- &
    child=$!
    rc=0
    while true; do
      wait "${child}" || rc=$?
      if ! kill -0 "${child}" 2>/dev/null; then
        break
      fi
    done
    child=""
    if ((SUP_STOP)); then
      break
    fi
    now
    started="${UP}"
    kept=()
    for t in "${times[@]+"${times[@]}"}"; do
      if ((started - t < RESTART_WINDOW)); then
        kept+=("${t}")
      fi
    done
    times=("${kept[@]+"${kept[@]}"}" "${started}")
    if ((${#times[@]} > RESTART_LIMIT)); then
      log "worker ended too often, giving up"
      break
    fi
    log "worker ended (code ${rc}), restarting in ${RESTART_DELAY} s"
    sleep "${RESTART_DELAY}" &
    wait $! || true
    if ((SUP_STOP)); then
      break
    fi
  done
  rm -f -- "${PRIVATE_DIR}/supervisor.pid"
}

cmd_start() {
  local lock
  need_root
  ensure_dir "${BASE_DIR}" 0755 || die "${BASE_DIR} ist nicht nutzbar"
  ensure_dir "${PRIVATE_DIR}" 0700 || die "${PRIVATE_DIR} ist nicht nutzbar"
  exec {lock}>>"${PRIVATE_DIR}/lock"
  if ! flock -n "${lock}"; then
    echo "Der Wächter läuft bereits."
    exit 0
  fi
  flock -u "${lock}"
  exec {lock}>&-
  setsid /bin/bash "${SELF}" supervise </dev/null >/dev/null 2>&1 &
  echo "Der Wächter wurde gestartet."
}

cmd_stop() {
  local i
  need_root
  if ! supervisor_pid; then
    echo "Der Wächter läuft nicht."
    return 0
  fi
  : >"${PRIVATE_DIR}/stop.request"
  rm -f -- "${PRIVATE_DIR}/state_lost"
  kill -TERM "${SUP_PID}" 2>/dev/null || true
  # Ein offener Lauf startet vorher seine Container; das kann dauern.
  for ((i = 0; i < 600; i++)); do
    if [[ ! -e "/proc/${SUP_PID}" ]]; then
      break
    fi
    sleep 0.5
  done
  rm -f -- "${PRIVATE_DIR}/stop.request"
  if [[ -e "/proc/${SUP_PID}" ]]; then
    die "Der Wächter hat sich nicht beendet."
  fi
  echo "Der Wächter wurde gestoppt."
}

cmd_status() {
  need_root
  if supervisor_pid; then
    echo "Der Wächter läuft (PID ${SUP_PID})."
  else
    echo "Der Wächter läuft nicht."
  fi
  if [[ -f "${STATUS_FILE}" ]]; then
    cat -- "${STATUS_FILE}"
  fi
}

cmd_check_config() {
  local i
  need_root
  if ! load_config; then
    echo "Konfiguration ungültig: ${CONFIG_DETAIL} (Zeile ${CONFIG_LINE})."
    exit 1
  fi
  if docker_check; then
    refresh_containers
    for i in "${!ALLOW[@]}"; do
      if [[ "${ALLOWED_STATES[i]}" == self ]]; then
        echo "Konfiguration ungültig: allow enthält TwinRaid (${ALLOW[i]})."
        exit 1
      fi
    done
  else
    echo "Hinweis: Docker ist nicht erreichbar; die Container wurden nicht geprüft."
  fi
  echo "Konfiguration in Ordnung: ${#ALLOW[@]} Container freigegeben, ${#DATASETS[@]} Datasets."
}

# --- Selbsttest über die Auftragsdatei ---------------------------------------------------

status_value() {
  local text pattern="$1"
  text="$(<"${STATUS_FILE}")" || return 1
  if [[ "${text}" =~ ${pattern} ]]; then
    VALUE="${BASH_REMATCH[1]}"
    return 0
  fi
  return 1
}

# Schreibt einen Auftrag wie TwinRaid und wartet auf das Ergebnis. Setzt RESULT.
send_order() {
  local order="$1" container="${2:-}" wait_seconds="$3" seq payload sum line i
  status_value '"last_seq":([0-9]+)' || die "status.json fehlt"
  seq=$((VALUE + 1))
  payload="TRSG1 ${seq} ${order}${container:+ ${container}}"
  sum="$(printf '%s' "${payload}" | sha256sum)"
  printf -v line '%-255s\n' "${payload} ${sum:0:16}"
  printf '%s' "${line}" >"${REQUEST}"
  for ((i = 0; i < wait_seconds * 2; i++)); do
    if status_value '"last":\{"seq":([0-9]+)' && ((VALUE == seq)); then
      status_value '"last":\{"seq":[0-9]+,"order":"[a-z]+","container":[^,]+,"result":"([a-z_]+)"' ||
        die "status.json ist unlesbar"
      RESULT="${VALUE}"
      return 0
    fi
    sleep 0.5
  done
  die "keine Antwort auf Auftrag ${seq} (${order})"
}

container_state() {
  local rc=0
  lookup_container "$1" "$1" || rc=$?
  case "${rc}" in
    0) STATE="${C_RUNNING}" ;;
    1) STATE=missing ;;
    *) STATE=unknown ;;
  esac
}

selftest_ready() {
  need_root
  [[ "$1" =~ ${CONTAINER_RE} ]] || die "ungültiger Container-Name"
  load_config || die "Konfiguration ungültig: ${CONFIG_DETAIL} (Zeile ${CONFIG_LINE})"
  supervisor_pid || die "Der Wächter läuft nicht."
  status_value '"error":(null|"[a-z_]+")' || die "status.json fehlt"
  [[ "${VALUE}" == null ]] || die "Der Wächter meldet einen Fehler: ${VALUE}"
  status_value '"run":(null)' || die "Es ist schon ein Lauf offen."
  if ! status_value '"run_allowed_at":(null)'; then
    die "Der letzte Lauf ist noch keine ${MIN_RUN_INTERVAL} Minuten her. Warten oder: source-guard.sh reset-run-interval"
  fi
  echo "Hinweis: Während des Selbsttests darf TwinRaid keine Aufträge schreiben."
}

cmd_selftest() {
  local name="$1" before after stop_result
  selftest_ready "${name}"
  container_state "${name}"
  before="${STATE}"
  echo "Container ${name} vorher: ${before}"
  send_order begin "" 10
  [[ "${RESULT}" == opened ]] || die "begin: ${RESULT}"
  send_order stop "${name}" $((STOP_TIMEOUT + 30))
  stop_result="${RESULT}"
  container_state "${name}"
  echo "stop: ${stop_result} (Container jetzt: ${STATE})"
  sleep 3
  send_order finish "" 120
  [[ "${RESULT}" == finished ]] || die "finish: ${RESULT}"
  container_state "${name}"
  after="${STATE}"
  echo "Container ${name} nachher: ${after}"
  case "${stop_result}" in
    stopped)
      [[ "${after}" == true ]] || die "Der Container läuft nach finish nicht wieder."
      echo "OK: angehalten und wieder gestartet."
      ;;
    not_allowed | denied_self | not_found | not_running)
      [[ "${after}" == "${before}" ]] || die "Der Container wurde verändert."
      echo "OK: abgelehnt (${stop_result}), Container unverändert."
      ;;
    *)
      die "stop: ${stop_result}"
      ;;
  esac
}

cmd_selftest_deadman() {
  local name="$1" i
  selftest_ready "${name}"
  send_order begin "" 10
  [[ "${RESULT}" == opened ]] || die "begin: ${RESULT}"
  send_order stop "${name}" $((STOP_TIMEOUT + 30))
  [[ "${RESULT}" == stopped ]] || die "stop: ${RESULT} (der Container muss freigegeben sein und laufen)"
  echo "Angehalten. Ohne ping greift der Totmannschalter nach ${HEARTBEAT_TIMEOUT} s."
  for ((i = 0; i < (HEARTBEAT_TIMEOUT + 60) * 2; i++)); do
    if status_value '"last_run":\{"opened_at":[0-9]+,"closed_at":[0-9]+,"reason":"([a-z_]+)"' &&
      [[ "${VALUE}" == deadman_heartbeat ]] && status_value '"run":(null)'; then
      container_state "${name}"
      [[ "${STATE}" == true ]] || die "Totmannschalter ausgelöst, aber der Container läuft nicht."
      echo "OK: Totmannschalter ausgelöst, Container läuft wieder."
      return 0
    fi
    sleep 0.5
  done
  die "Der Totmannschalter hat nicht ausgelöst."
}

# Hebt die Pause zwischen zwei Läufen auf (Konzept 3.6). Nur root auf Klein.
cmd_reset_run_interval() {
  local i
  need_root
  ensure_dir "${BASE_DIR}" 0755 || die "${BASE_DIR} ist nicht nutzbar"
  ensure_dir "${PRIVATE_DIR}" 0700 || die "${PRIVATE_DIR} ist nicht nutzbar"
  : >"${PRIVATE_DIR}/reset-run-interval"
  if ! supervisor_pid; then
    echo "Der Wächter läuft nicht; die Pause entfällt beim nächsten Start."
    return 0
  fi
  for ((i = 0; i < 20; i++)); do
    if [[ ! -e "${PRIVATE_DIR}/reset-run-interval" ]]; then
      echo "Die Pause zwischen zwei Läufen ist aufgehoben."
      return 0
    fi
    sleep 0.5
  done
  die "Der Wächter hat die Anfrage nicht übernommen."
}

usage() {
  cat >&2 <<'EOF'
Aufruf: source-guard.sh start | stop | restart | status | check-config | reset-run-interval
        source-guard.sh selftest <Container> | selftest-deadman <Container>
EOF
  exit 2
}

command="${1:-}"
case "${command}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart)
    cmd_stop
    cmd_start
    ;;
  status) cmd_status ;;
  check-config) cmd_check_config ;;
  reset-run-interval) cmd_reset_run_interval ;;
  selftest)
    [[ $# -eq 2 ]] || usage
    cmd_selftest "$2"
    ;;
  selftest-deadman)
    [[ $# -eq 2 ]] || usage
    cmd_selftest_deadman "$2"
    ;;
  supervise) supervise ;;
  run) run_guard ;;
  *) usage ;;
esac
