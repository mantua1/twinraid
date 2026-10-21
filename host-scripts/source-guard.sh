#!/bin/bash
# TwinRaid: Wächter auf dem Host von Unraid Klein, Version 2 (Konzept 3.6).
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
#   source-guard.sh init-key [--new] | trust-target [--fingerprint SHA256:…]
#
# Version 1 (TwinRaid v0.5.0) kannte die Aufträge begin, stop, ping und finish.
# Version 2 (TwinRaid v0.5.1) nimmt dazu snapshot, prepare, confirm, send und close an:
# ZFS-Snapshots der Datasets aus "dataset", Prüfen gegen die Basis und guard.sh auf Groß,
# Senden mit zfs send über SSH mit dem Key „sync“. prepare und send laufen in einem
# Kindprozess; der Status zeigt Job und Fortschritt (Konzept 3.6, „Job, Prüfen und
# Senden“). Snapshots löscht nur der Wächter selbst nach seiner Regel, nie auf Auftrag.
#
# Ins Syslog (Kennung twinraid-source-guard) kommen nur feste Texte, Codes, Zahlen
# und geprüfte Container-Namen, nie der Inhalt der Auftragsdatei.
set -euo pipefail
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

readonly VERSION=2
readonly PROTOCOL=2
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
readonly SSH=/usr/bin/ssh
readonly SSH_KEYGEN=/usr/bin/ssh-keygen
readonly SSH_KEYSCAN=/usr/bin/ssh-keyscan
readonly IP=/sbin/ip
# Key „sync“ und bestätigter Hostkey von Groß (Konzept 3.6, ab Version 2).
readonly SYNC_KEY=/boot/config/twinraid/ssh-sync
readonly KNOWN_HOSTS=/boot/config/twinraid/target-known-hosts
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
# Snapshots und Job (ab Version 2), Zeiten in Sekunden nach Laufzeit: Mindestabstand zweier
# Snapshots, Fristen für prepare, für das Warten danach und für eine Übertragung,
# Aufbewahrung einmal täglich, Zeitlimit für guard.sh check.
readonly SNAPSHOT_PREFIX=twinraid-
readonly CREATED_BY=twinraid:created-by
readonly HOLD_TAG=twinraid-base
readonly SNAPSHOT_GAP=1800
readonly SNAPSHOT_LIMIT=50
readonly PREPARE_LIMIT=1800
readonly CONFIRM_LIMIT=1800
readonly SEND_LIMIT=43200
readonly RETENTION_INTERVAL=86400
readonly CHECK_TIMEOUT=60
readonly DAY=86400
# Gründe, mit denen guard.sh einen Empfang ablehnt (Konzept 3.7); jeder andere wird refused_other.
readonly SEND_REFUSALS=(name-denied receive-busy mirror-root-missing receiver-id-in-use
  target-exists delegation-mismatch props-mismatch props-not-applied clone-active space-low
  too-soon received-properties mount-mismatch)

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
readonly GUID_RE='^[0-9]{1,20}$'
readonly SNAPNAME_RE='^twinraid-[0-9]{8}T[0-9]{6}Z$'
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
      elif job_is_open; then
        RESULT=job_open
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
    snapshot) order_snapshot ;;
    prepare) order_prepare ;;
    confirm) order_confirm ;;
    send) order_send ;;
    close) order_close ;;
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
# Ab Version 2: eigene Snapshots (Anzahl, ältester), Basis mit Hold, eigenes Lesezeichen.
DS_SNAPSHOTS=()
DS_OLDEST=()
DS_BASE_GUID=()
DS_BASE_CREATED=()
DS_BOOKMARK=()
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
  DS_SNAPSHOTS=()
  DS_OLDEST=()
  DS_BASE_GUID=()
  DS_BASE_CREATED=()
  DS_BOOKMARK=()
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
    DS_SNAPSHOTS+=("")
    DS_OLDEST+=("")
    DS_BASE_GUID+=("")
    DS_BASE_CREATED+=("")
    DS_BOOKMARK+=("")
    if out="$(zfs_call "${ZFS}" list -H -p -o name,type,referenced,compression -- "${POOL}/${ds}" 2>/dev/null)"; then
      IFS=$'\t' read -r name type referenced compression <<<"${out}"
      if [[ "${name}" == "${POOL}/${ds}" && "${type}" == filesystem ]]; then
        DS_PRESENT[-1]=true
        DS_REFERENCED[-1]="${referenced}"
        DS_COMPRESSION[-1]="${compression}"
        if own_snapshots "${POOL}/${ds}"; then
          DS_SNAPSHOTS[-1]="${#OS_NAMES[@]}"
          DS_OLDEST[-1]="${OS_CREATED[0]:-}"
          find_held_base "${POOL}/${ds}"
          DS_BASE_GUID[-1]="${BASE_GUID}"
          DS_BASE_CREATED[-1]="${BASE_CREATED}"
        fi
        if own_bookmarks "${POOL}/${ds}"; then
          DS_BOOKMARK[-1]=false
          if ((${#OB_NAMES[@]} > 0)); then
            DS_BOOKMARK[-1]=true
          fi
        fi
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

# --- Snapshots, Job und Senden (ab Version 2, Konzept 3.6) --------------------------------

# Ein Job zur Zeit (Konzept 3.6, „Job, Prüfen und Senden“). Der Zustand liegt in
# private/job; prepare und send bearbeiten ihn in einem Kindprozess, der ihn am Ende als
# private/child/job zurückgibt. Werte je Dataset stehen in parallelen Listen (JD_*).
JOB_ID=0
JOB_STATE=""
JOB_RESULT=""
JOB_REASON=""
JOB_SNAPSHOT=""
JOB_CREATED=0
# Frist in Laufzeit (UP); 0: keine.
JOB_DEADLINE=0
JOB_CONFIRMED=0
JOB_SEND_READY=""
JOB_CONFIRM_REASONS=()
JOB_BLOCKERS=()
readonly JD_FIELDS=(name mode base_guid base_type problem deleted modified added renamed
  referenced base_referenced size sent result)
JD_NAME=()
JD_MODE=()
JD_BASE_GUID=()
JD_BASE_TYPE=()
JD_PROBLEM=()
JD_DELETED=()
JD_MODIFIED=()
JD_ADDED=()
JD_RENAMED=()
JD_REFERENCED=()
JD_BASE_REFERENCED=()
JD_SIZE=()
JD_SENT=()
JD_RESULT=()
# Laufzeit beim letzten Snapshot dieses Systemstarts (Mindestabstand); 0: noch keiner.
LAST_SNAPSHOT_UP=0
# Letzte tägliche Aufbewahrung in Laufzeit.
LAST_RETENTION_UP=0
CHILD_PID=""
RUN_LOCK=""

# Zeichenregel je Feld eines Datasets im Job.
jd_pattern() {
  case "$1" in
    name) PATTERN="${SHARE_RE}" ;;
    mode) PATTERN='^(full|incremental)$' ;;
    base_guid) PATTERN="${GUID_RE}" ;;
    base_type) PATTERN='^(snapshot|bookmark)$' ;;
    problem | result) PATTERN="${CODE_RE}" ;;
    *) PATTERN='^(0|[1-9][0-9]{0,18})$' ;;
  esac
}

job_is_open() {
  case "${JOB_STATE}" in
    snapshot | preparing | prepared | awaiting_confirm | sending) return 0 ;;
  esac
  return 1
}

clear_job() {
  local field
  JOB_ID=0
  JOB_STATE=""
  JOB_RESULT=""
  JOB_REASON=""
  JOB_SNAPSHOT=""
  JOB_CREATED=0
  JOB_DEADLINE=0
  JOB_CONFIRMED=0
  JOB_SEND_READY=""
  JOB_CONFIRM_REASONS=()
  JOB_BLOCKERS=()
  for field in "${JD_FIELDS[@]}"; do
    # shellcheck disable=SC2178 # Namensreferenz auf eine der Listen JD_*.
    local -n jd_ref="JD_${field^^}"
    jd_ref=()
    unset -n jd_ref
  done
}

# Schreibt den Job (Standard private/job); "-" steht für einen leeren Wert.
save_job() {
  local file="${1:-${PRIVATE_DIR}/job}" text i field value line
  if ((JOB_ID == 0)); then
    rm -f -- "${file}"
    return 0
  fi
  text="id ${JOB_ID}"$'\n'"state ${JOB_STATE}"$'\n'"result ${JOB_RESULT:--}"$'\n'
  text+="reason ${JOB_REASON:--}"$'\n'"snapshot ${JOB_SNAPSHOT}"$'\n'"created ${JOB_CREATED}"$'\n'
  text+="deadline ${JOB_DEADLINE}"$'\n'"confirmed ${JOB_CONFIRMED}"$'\n'"send_ready ${JOB_SEND_READY:--}"$'\n'
  text+="confirm_reasons ${JOB_CONFIRM_REASONS[*]:--}"$'\n'"blockers ${JOB_BLOCKERS[*]:--}"$'\n'
  for i in "${!JD_NAME[@]}"; do
    line="dataset"
    for field in "${JD_FIELDS[@]}"; do
      # shellcheck disable=SC2178 # Namensreferenz auf eine der Listen JD_*.
      local -n jd_ref="JD_${field^^}"
      value="${jd_ref[i]:-}"
      unset -n jd_ref
      line+=" ${value:--}"
    done
    text+="${line}"$'\n'
  done
  write_atomic "${file}" "${text}" 0600
}

# Liest einen Job und prüft jeden Wert. Rückgabe 1, wenn die Datei fehlt oder ungültig ist
# (dann ist kein Job geladen).
load_job_from() {
  local file="$1" key rest value i field code
  local -a values=() words=()
  clear_job
  if [[ ! -f "${file}" || -L "${file}" ]]; then
    return 1
  fi
  while read -r key rest; do
    case "${key}" in
      id) [[ "${rest}" =~ ^[1-9][0-9]{0,8}$ ]] && JOB_ID="${rest}" ;;
      state)
        case "${rest}" in
          snapshot | preparing | prepared | awaiting_confirm | sending | done | failed | closed)
            JOB_STATE="${rest}"
            ;;
        esac
        ;;
      result | reason)
        value=""
        if [[ "${rest}" != - ]]; then
          [[ "${rest}" =~ ${CODE_RE} ]] || { clear_job && return 1; }
          value="${rest}"
        fi
        if [[ "${key}" == result ]]; then JOB_RESULT="${value}"; else JOB_REASON="${value}"; fi
        ;;
      snapshot) [[ "${rest}" =~ ${SNAPNAME_RE} ]] && JOB_SNAPSHOT="${rest}" ;;
      created) [[ "${rest}" =~ ^[0-9]{1,12}$ ]] && JOB_CREATED="${rest}" ;;
      deadline) [[ "${rest}" =~ ^[0-9]{1,12}$ ]] && JOB_DEADLINE="${rest}" ;;
      confirmed) [[ "${rest}" =~ ^[01]$ ]] && JOB_CONFIRMED="${rest}" ;;
      send_ready)
        case "${rest}" in
          true | false) JOB_SEND_READY="${rest}" ;;
          -) JOB_SEND_READY="" ;;
        esac
        ;;
      confirm_reasons | blockers)
        read -r -a words <<<"${rest}"
        values=()
        for code in "${words[@]}"; do
          [[ "${code}" == - ]] && continue
          [[ "${code}" =~ ${CODE_RE} ]] || { clear_job && return 1; }
          values+=("${code}")
        done
        if [[ "${key}" == blockers ]]; then
          JOB_BLOCKERS=("${values[@]+"${values[@]}"}")
        else
          JOB_CONFIRM_REASONS=("${values[@]+"${values[@]}"}")
        fi
        ;;
      dataset)
        read -r -a words <<<"${rest}"
        if ((${#words[@]} != ${#JD_FIELDS[@]})); then
          clear_job
          return 1
        fi
        i="${#JD_NAME[@]}"
        for field in "${!JD_FIELDS[@]}"; do
          value="${words[field]}"
          [[ "${value}" == - ]] && value=""
          jd_pattern "${JD_FIELDS[field]}"
          if [[ -n "${value}" && ! "${value}" =~ ${PATTERN} ]] ||
            [[ "${JD_FIELDS[field]}" == name && -z "${value}" ]]; then
            clear_job
            return 1
          fi
          # shellcheck disable=SC2178 # Namensreferenz auf eine der Listen JD_*.
          local -n jd_ref="JD_${JD_FIELDS[field]^^}"
          jd_ref[i]="${value}"
          unset -n jd_ref
        done
        ;;
    esac
  done <"${file}"
  if ((JOB_ID == 0)) || [[ -z "${JOB_STATE}" || -z "${JOB_SNAPSHOT}" ]]; then
    clear_job
    return 1
  fi
}

add_unique() {
  local -n target="$1"
  if ! in_list "$2" "${target[@]+"${target[@]}"}"; then
    target+=("$2")
  fi
}

# Ist der Pool da? (Für Aufträge; refresh_zfs prüft nur alle ZFS_INTERVAL Sekunden.)
zfs_ready() {
  local out
  [[ -x "${ZFS}" && -x "${ZPOOL}" ]] || return 1
  out="$(zfs_call "${ZPOOL}" list -H -o name -- "${POOL}" 2>/dev/null)" || return 1
  [[ "${out}" == "${POOL}" ]]
}

# Eigene Snapshots eines Datasets (Name twinraid-…, twinraid:created-by=source-guard mit
# Quelle local), nach dem Entstehen sortiert. Setzt OS_NAMES (Teil nach "@"), OS_GUIDS,
# OS_CREATED und OS_HOLDS (Anzahl Holds). Rückgabe 1, wenn nicht lesbar.
own_snapshots() {
  local ds="$1" out name property value source line
  local -A own=() guid=() created=() holds=() txg=()
  OS_NAMES=()
  OS_GUIDS=()
  OS_CREATED=()
  OS_HOLDS=()
  out="$(zfs_call "${ZFS}" get -H -p -r -t snapshot -o name,property,value,source \
    -- "${CREATED_BY},guid,creation,userrefs,createtxg" "${ds}" 2>/dev/null)" || return 1
  while IFS=$'\t' read -r name property value source; do
    if [[ "${name}" != "${ds}@"* || ! "${name#"${ds}@"}" =~ ${SNAPNAME_RE} ]]; then
      continue
    fi
    case "${property}" in
      "${CREATED_BY}") [[ "${value}" == source-guard && "${source}" == local ]] && own["${name}"]=1 ;;
      guid) [[ "${value}" =~ ${GUID_RE} ]] && guid["${name}"]="${value}" ;;
      creation) [[ "${value}" =~ ^[0-9]{1,12}$ ]] && created["${name}"]="${value}" ;;
      userrefs) [[ "${value}" =~ ^[0-9]{1,9}$ ]] && holds["${name}"]="${value}" ;;
      createtxg) [[ "${value}" =~ ^[0-9]{1,19}$ ]] && txg["${name}"]="${value}" ;;
    esac
  done <<<"${out}"
  while IFS=$'\t' read -r value name; do
    [[ -n "${name}" ]] || continue
    if [[ -z "${guid[${name}]:-}" || -z "${created[${name}]:-}" || -z "${holds[${name}]:-}" ]]; then
      continue
    fi
    OS_NAMES+=("${name#"${ds}@"}")
    OS_GUIDS+=("${guid[${name}]}")
    OS_CREATED+=("${created[${name}]}")
    OS_HOLDS+=("${holds[${name}]}")
  done < <(for line in "${!own[@]}"; do
    printf '%s\t%s\n' "${txg[${line}]:-0}" "${line}"
  done | sort -n)
}

# Eigene Lesezeichen (#twinraid-…). Setzt OB_NAMES (Teil nach "#") und OB_GUIDS.
own_bookmarks() {
  local ds="$1" out name guid txg
  OB_NAMES=()
  OB_GUIDS=()
  out="$(zfs_call "${ZFS}" list -H -p -d 1 -t bookmark -o name,guid,createtxg -s createtxg \
    -- "${ds}" 2>/dev/null)" || return 1
  while IFS=$'\t' read -r name guid txg; do
    if [[ "${name}" == "${ds}#"* && "${name#"${ds}#"}" =~ ${SNAPNAME_RE} && "${guid}" =~ ${GUID_RE} ]]; then
      OB_NAMES+=("${name#"${ds}#"}")
      OB_GUIDS+=("${guid}")
    fi
  done <<<"${out}"
}

# Die Basis: der eigene Snapshot mit dem Hold twinraid-base (der neueste, falls mehrere).
# Setzt BASE_GUID und BASE_CREATED (leer, wenn es keine gibt). Nach own_snapshots.
find_held_base() {
  local ds="$1" i out name tag
  BASE_GUID=""
  BASE_CREATED=""
  for ((i = ${#OS_NAMES[@]} - 1; i >= 0; i--)); do
    if ((OS_HOLDS[i] == 0)); then
      continue
    fi
    out="$(zfs_call "${ZFS}" holds -H -- "${ds}@${OS_NAMES[i]}" 2>/dev/null)" || continue
    while IFS=$'\t' read -r name tag _; do
      if [[ "${name}" == "${ds}@${OS_NAMES[i]}" && "${tag}" == "${HOLD_TAG}" ]]; then
        BASE_GUID="${OS_GUIDS[i]}"
        BASE_CREATED="${OS_CREATED[i]}"
        return 0
      fi
    done <<<"${out}"
  done
}

# --- SSH zu guard.sh auf Groß --------------------------------------------------------------

# Den Key „sync“ nach private/ kopieren (tmpfs, 0600): ssh lehnt Keys mit zu weiten Rechten
# ab, und /boot speichert als FAT keine Dateirechte. Rückgabe 1, wenn er fehlt.
ssh_key_ready() {
  if [[ ! -f "${SYNC_KEY}" || -L "${SYNC_KEY}" ]]; then
    return 1
  fi
  install -m 0600 -- "${SYNC_KEY}" "${PRIVATE_DIR}/ssh-sync" 2>/dev/null
}

# Feste Argumentliste (Konzept 3.6, „SSH“). Setzt SSH_ARGS; die Anfrage folgt dahinter.
ssh_args() {
  SSH_ARGS=(-F /dev/null -i "${PRIVATE_DIR}/ssh-sync" -o BatchMode=yes -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=${KNOWN_HOSTS}"
    -o GlobalKnownHostsFile=/dev/null -o HostKeyAlgorithms=ssh-ed25519 -o ConnectTimeout=5
    -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -p "${TARGET_PORT}" -l root
    -- "${TARGET_HOST}")
}

# Ordnet einen gescheiterten Aufruf nach Exit-Code und festen Meldungen von OpenSSH (LC_ALL=C)
# einem Code zu; die Meldungen selbst gehen nirgends hin. Setzt SSH_CODE.
ssh_failure() {
  local rc="$1" err="$2"
  SSH_CODE=ssh_failed
  if ((rc == 255)); then
    case "${err}" in
      *"REMOTE HOST IDENTIFICATION HAS CHANGED"*) SSH_CODE=host_key_mismatch ;;
      *"Host key verification failed"* | *"No ED25519 host key is known"*) SSH_CODE=host_untrusted ;;
      *"Permission denied"*) SSH_CODE=auth_failed ;;
      *"Connection refused"* | *"timed out"* | *"No route to host"* | *"Network is unreachable"* | *"Could not resolve"*)
        SSH_CODE=unreachable
        ;;
    esac
  elif ((rc == 2)) && [[ "${err}" == *"twinraid-guard: denied"* ]]; then
    SSH_CODE=denied
  elif ((rc == 124 || rc == 137)); then
    SSH_CODE=unreachable
  fi
}

# Zeilen von guard.sh sync check (Konzept 3.7), streng.
readonly CHECK_FIRST_RE='^twinraid-guard ([0-9]{1,4}) sync ok$'
readonly CHECK_MIRROR_RE='^mirror ([A-Za-z0-9][A-Za-z0-9._-]{0,63}) state=(absent|mirror|foreign) props=(ok|mismatch|-) count=([0-9]{1,9}|-|\?) newest_guid=([0-9]{1,20}|-|\?) newest_created=([0-9]{1,12}|-|\?) newest_received=([0-9]{1,12}|-|\?) oldest_created=([0-9]{1,12}|-|\?)$'
readonly CHECK_SUMMARY_RE='^summary root=(ok|missing) conf=(ok|missing|invalid) deleg=(ok|mismatch|id-in-use) receive=(idle|busy) clones=([0-9]{1,9}|\?) available=([0-9]{1,19}|\?) size=([0-9]{1,19}|\?) reserve_percent=([0-9]{1,2}|-) keep_days=([0-9]{1,4}|-) min_interval_minutes=([0-9]{1,4}|-)$'
CHECK_AT=0
CHECK_RESULT=""
CHECK_LINES=()

# Prüft die Antwort von check; Rückgabe 1 bei einer Zeile außerhalb des Formats.
check_lines_valid() {
  local line count=0 summary=0
  ((${#CHECK_LINES[@]} >= 2)) || return 1
  [[ "${CHECK_LINES[0]}" =~ ${CHECK_FIRST_RE} ]] && ((10#${BASH_REMATCH[1]} >= 3)) || return 1
  for line in "${CHECK_LINES[@]:1}"; do
    if ((summary)); then
      return 1
    fi
    if [[ "${line}" =~ ${CHECK_MIRROR_RE} ]]; then
      count=$((count + 1))
      ((count <= 20)) || return 1
    elif [[ "${line}" =~ ${CHECK_SUMMARY_RE} ]]; then
      summary=1
    else
      return 1
    fi
  done
  ((summary))
}

# guard.sh sync check über SSH. Setzt CHECK_AT, CHECK_RESULT (ok oder ein Code) und CHECK_LINES.
guard_check() {
  local out rc=0 err=""
  now
  CHECK_AT="${NOW}"
  CHECK_LINES=()
  CHECK_RESULT=""
  if [[ -z "${TARGET_HOST}" ]]; then
    CHECK_RESULT=target_not_configured
  elif ! ssh_key_ready; then
    CHECK_RESULT=key_missing
  elif [[ ! -f "${KNOWN_HOSTS}" || -L "${KNOWN_HOSTS}" ]]; then
    CHECK_RESULT=host_untrusted
  fi
  if [[ -n "${CHECK_RESULT}" ]]; then
    return 1
  fi
  ssh_args
  out="$(timeout --kill-after=5 "${CHECK_TIMEOUT}" "${SSH}" "${SSH_ARGS[@]}" check \
    </dev/null 2>"${PRIVATE_DIR}/ssh.err" | head -c 65536)" || rc=$?
  err="$(head -c 4096 -- "${PRIVATE_DIR}/ssh.err" 2>/dev/null)" || err=""
  rm -f -- "${PRIVATE_DIR}/ssh.err"
  if ((rc != 0)); then
    ssh_failure "${rc}" "${err}"
    CHECK_RESULT="${SSH_CODE}"
    return 1
  fi
  mapfile -t CHECK_LINES <<<"${out}"
  if ! check_lines_valid; then
    CHECK_LINES=()
    CHECK_RESULT=bad_answer
    return 1
  fi
  CHECK_RESULT=ok
}

save_check() {
  local file="${1:-${PRIVATE_DIR}/check}" text line
  if [[ -z "${CHECK_RESULT}" ]]; then
    rm -f -- "${file}"
    return 0
  fi
  text="at ${CHECK_AT}"$'\n'"result ${CHECK_RESULT}"$'\n'
  for line in "${CHECK_LINES[@]+"${CHECK_LINES[@]}"}"; do
    text+="line ${line}"$'\n'
  done
  write_atomic "${file}" "${text}" 0600
}

load_check_from() {
  local file="$1" key rest
  CHECK_AT=0
  CHECK_RESULT=""
  CHECK_LINES=()
  if [[ ! -f "${file}" || -L "${file}" ]]; then
    return 1
  fi
  while IFS=' ' read -r key rest; do
    case "${key}" in
      at) [[ "${rest}" =~ ^[0-9]{1,12}$ ]] && CHECK_AT="${rest}" ;;
      result) [[ "${rest}" =~ ${CODE_RE} ]] && CHECK_RESULT="${rest}" ;;
      line) CHECK_LINES+=("${rest}") ;;
    esac
  done <"${file}"
  if [[ "${CHECK_RESULT}" == ok ]] && ! check_lines_valid; then
    CHECK_RESULT=bad_answer
    CHECK_LINES=()
  fi
  [[ -n "${CHECK_RESULT}" ]]
}

# Zerlegt CHECK_LINES in M_* (je Spiegel) und S_* (summary).
declare -A M_STATE=() M_PROPS=() M_GUID=()
parse_check() {
  local line
  M_STATE=()
  M_PROPS=()
  M_GUID=()
  S_ROOT=""
  S_CONF=""
  S_DELEG=""
  S_RECEIVE=""
  S_CLONES=""
  S_AVAILABLE=""
  S_SIZE=""
  S_RESERVE=""
  for line in "${CHECK_LINES[@]+"${CHECK_LINES[@]}"}"; do
    if [[ "${line}" =~ ${CHECK_MIRROR_RE} ]]; then
      M_STATE["${BASH_REMATCH[1]}"]="${BASH_REMATCH[2]}"
      M_PROPS["${BASH_REMATCH[1]}"]="${BASH_REMATCH[3]}"
      M_GUID["${BASH_REMATCH[1]}"]="${BASH_REMATCH[5]}"
    elif [[ "${line}" =~ ${CHECK_SUMMARY_RE} ]]; then
      S_ROOT="${BASH_REMATCH[1]}"
      S_CONF="${BASH_REMATCH[2]}"
      S_DELEG="${BASH_REMATCH[3]}"
      S_RECEIVE="${BASH_REMATCH[4]}"
      S_CLONES="${BASH_REMATCH[5]}"
      S_AVAILABLE="${BASH_REMATCH[6]}"
      S_SIZE="${BASH_REMATCH[7]}"
      S_RESERVE="${BASH_REMATCH[8]}"
    fi
  done
}

# --- Kindprozess: prepare und send ---------------------------------------------------------

# Läuft der Kindprozess noch? (Ein beendeter bleibt als Zombie, bis wait ihn abholt.)
child_alive() {
  local stat
  [[ -n "${CHILD_PID}" ]] || return 1
  stat="$(cat "/proc/${CHILD_PID}/stat" 2>/dev/null)" || return 1
  stat="${stat##*) }"
  [[ "${stat:0:1}" != Z ]]
}

# Startet "source-guard.sh child <Art> <PID des Wächters>" in einer eigenen Sitzung (setsid,
# Sitzung = seine PID); die Sperre des Wächters erbt er nicht.
start_child() {
  local kind="$1"
  rm -rf -- "${PRIVATE_DIR}/child"
  mkdir -m 0700 -- "${PRIVATE_DIR}/child" || return 1
  setsid /bin/bash "${SELF}" child "${kind}" "$$" {RUN_LOCK}>&- </dev/null >/dev/null 2>&1 &
  CHILD_PID=$!
  write_atomic "${PRIVATE_DIR}/child.pid" "${CHILD_PID}" 0600
}

# Signal an alle Prozesse der Sitzung des Kindprozesses (setsid; die Sitzung hat seine PID),
# außer an den aufrufenden Prozess selbst. Eine Prozessgruppe genügt nicht: timeout legt
# für zfs und ssh eigene Gruppen an. Die Sitzung steht in /proc/<PID>/stat im vierten Feld
# nach dem Namen.
signal_session() {
  local signal="$1" session="$2" stat line pid
  local -a fields=()
  for stat in /proc/[0-9]*/stat; do
    read -r line 2>/dev/null <"${stat}" || continue
    read -r -a fields <<<"${line##*) }"
    pid="${stat#/proc/}"
    pid="${pid%/stat}"
    if [[ "${fields[3]:-}" == "${session}" && "${pid}" != "${BASHPID}" ]]; then
      kill "-${signal}" "${pid}" 2>/dev/null || true
    fi
  done
}

# Beendet den Kindprozess mit seiner ganzen Sitzung (etwa zfs send und ssh).
stop_child() {
  local i
  if [[ -z "${CHILD_PID}" ]]; then
    return 0
  fi
  signal_session TERM "${CHILD_PID}"
  for ((i = 0; i < 50; i++)); do
    child_alive || break
    sleep 0.1
  done
  signal_session KILL "${CHILD_PID}"
  wait "${CHILD_PID}" 2>/dev/null || true
  CHILD_PID=""
  rm -f -- "${PRIVATE_DIR}/child.pid"
}

# Ein Kindprozess aus einem früheren Lauf des Wächters (nur nach kill -9 möglich) wird mit
# seiner Sitzung beendet. Als solcher gilt nur ein Prozess, dessen Befehlszeile genau so
# lautet, wie start_child ihn startet: /bin/bash <dieses Skript> child <Art> <PID>. Lebt unter
# der PID ein anderer Prozess (PID wiederverwendet), bleibt er unberührt; ist die PID frei,
# gehören übrige Prozesse der Sitzung zum Kindprozess.
stop_stale_child() {
  local pid
  local -a args=()
  if [[ ! -f "${PRIVATE_DIR}/child.pid" ]]; then
    return 0
  fi
  pid="$(<"${PRIVATE_DIR}/child.pid")"
  if [[ "${pid}" =~ ^[0-9]{1,9}$ ]]; then
    if [[ ! -e "/proc/${pid}" ]]; then
      signal_session KILL "${pid}"
    elif mapfile -d '' -t args 2>/dev/null <"/proc/${pid}/cmdline" && ((${#args[@]} == 5)) &&
      [[ "${args[0]}" == /bin/bash && "${args[1]}" == "${SELF}" && "${args[2]}" == child &&
        "${args[3]}" =~ ^(prepare|send)$ && "${args[4]}" =~ ^[0-9]{1,9}$ ]]; then
      signal_session KILL "${pid}"
    fi
  fi
  rm -f -- "${PRIVATE_DIR}/child.pid"
}

# Übernimmt das Ergebnis eines beendeten Kindprozesses.
adopt_child() {
  local id="${JOB_ID}" before="${JOB_STATE}" fallback=prepare_failed valid=1
  if [[ "${before}" == sending ]]; then
    fallback=send_failed
  fi
  if load_check_from "${PRIVATE_DIR}/child/check"; then
    save_check
  else
    load_check_from "${PRIVATE_DIR}/check" || true
  fi
  if ! load_job_from "${PRIVATE_DIR}/child/job" || ((JOB_ID != id)); then
    valid=0
  elif [[ "${before}" == preparing ]]; then
    case "${JOB_STATE}" in prepared | awaiting_confirm | failed) ;; *) valid=0 ;; esac
  else
    case "${JOB_STATE}" in done | failed) ;; *) valid=0 ;; esac
  fi
  if ((valid == 0)); then
    load_job_from "${PRIVATE_DIR}/job" || true
    JOB_STATE=failed
    JOB_RESULT="${fallback}"
  fi
  now
  case "${JOB_STATE}" in
    prepared | awaiting_confirm) JOB_DEADLINE=$((UP + CONFIRM_LIMIT)) ;;
    *) JOB_DEADLINE=0 ;;
  esac
  save_job
  rm -rf -- "${PRIVATE_DIR}/child"
  log "job ${JOB_ID}: ${JOB_STATE}${JOB_RESULT:+ (${JOB_RESULT}${JOB_REASON:+ ${JOB_REASON}})}"
  STATUS_DIRTY=1
}

poll_child() {
  if [[ -z "${CHILD_PID}" ]] || child_alive; then
    return 0
  fi
  wait "${CHILD_PID}" 2>/dev/null || true
  # Reste seiner Sitzung (zfs send, ssh) enden mit ihm, auch nach kill -9.
  signal_session TERM "${CHILD_PID}"
  CHILD_PID=""
  rm -f -- "${PRIVATE_DIR}/child.pid"
  adopt_child
}

# Fristen des Jobs (nach Laufzeit): Danach schließt der Wächter ihn selbst (job_timeout).
check_job_deadline() {
  if ! job_is_open || ((JOB_DEADLINE == 0)); then
    return 0
  fi
  now
  if ((UP >= JOB_DEADLINE)); then
    stop_child
    JOB_STATE=closed
    JOB_RESULT=job_timeout
    JOB_DEADLINE=0
    save_job
    log "job ${JOB_ID}: closed (job_timeout)"
    STATUS_DIRTY=1
  fi
}

# Endet der Wächter, endet auch ein Kindprozess; der Job gilt dann als gescheitert.
interrupt_job() {
  stop_child
  case "${JOB_STATE}" in
    preparing | sending)
      JOB_STATE=failed
      JOB_RESULT=interrupted
      JOB_DEADLINE=0
      save_job
      log "job ${JOB_ID}: failed (interrupted)"
      ;;
  esac
}

# Der Kindprozess beendet sich selbst mit seiner Sitzung, wenn der Wächter fehlt.
watch_parent() {
  local parent="$1"
  while kill -0 "${parent}" 2>/dev/null; do
    sleep 1
  done
  signal_session TERM "$$"
}

# Liegen /config und /data von TwinRaid im eigenen Dataset (E18)? Nur dann wird appdata
# gesendet; sonst lägen Secrets von TwinRaid im Strom. Prüft nur, wenn appdata im Job ist.
self_dataset_ok() {
  SELF_DATASET_STATE=unknown
  in_list appdata "${JD_NAME[@]+"${JD_NAME[@]}"}" || return 0
  docker_check || true
  check_self_dataset
  [[ "${SELF_DATASET_STATE}" == found ]]
}

# Zählt die Zeilen von zfs diff nach dem ersten Zeichen. Setzt DIFF_*.
count_diff() {
  local out
  out="$("${ZFS}" diff -H -- "$1" "$2" | awk -F'\t' '
    { n[$1]++ }
    END { printf "%d %d %d %d\n", n["-"], n["M"], n["+"], n["R"] }')" || return 1
  read -r DIFF_DELETED DIFF_MODIFIED DIFF_ADDED DIFF_RENAMED <<<"${out}"
}

# Größe aus zfs send -nvP (Zeile "size<TAB><Byte>"): estimate_size <neu> [<Basis>].
# Setzt SEND_SIZE.
estimate_size() {
  local out line
  local -a increment=()
  SEND_SIZE=""
  if [[ -n "${2:-}" ]]; then
    increment=(-i "$2")
  fi
  out="$("${ZFS}" send -n -v -P -L -c -e "${increment[@]+"${increment[@]}"}" -- "$1" 2>/dev/null)" || return 1
  while IFS= read -r line; do
    if [[ "${line}" =~ ^size$'\t'([0-9]{1,19})$ ]]; then
      SEND_SIZE="${BASH_REMATCH[1]}"
    fi
  done <<<"${out}"
  [[ -n "${SEND_SIZE}" ]]
}

referenced_of() {
  local value
  value="$("${ZFS}" get -H -p -o value -- referenced "$1" 2>/dev/null)" || return 1
  [[ "${value}" =~ ^[0-9]{1,19}$ ]] || return 1
  REFERENCED="${value}"
}

# prepare im Kindprozess (Konzept 3.6): check auf Groß, je Dataset Basis, Zählungen, Größe,
# Freigabe und Bereitschaft zum Senden.
# shellcheck disable=SC2034 # JD_* liest save_job über Namensreferenzen.
child_prepare() {
  local i name ds new base guid deletions=0 total=0 j failed=0
  guard_check || true
  save_check "${PRIVATE_DIR}/child/check"
  parse_check
  JOB_BLOCKERS=()
  JOB_CONFIRM_REASONS=()
  if [[ "${CHECK_RESULT}" != ok ]]; then
    JOB_BLOCKERS+=(target_unreachable)
  else
    [[ "${S_ROOT}" == ok ]] || JOB_BLOCKERS+=(mirror_root_missing)
    [[ "${S_CONF}" == ok ]] || JOB_BLOCKERS+=(mirror_conf_invalid)
    [[ "${S_DELEG}" == ok ]] || JOB_BLOCKERS+=(delegation_mismatch)
    [[ "${S_RECEIVE}" == idle ]] || JOB_BLOCKERS+=(receive_busy)
    [[ "${S_CLONES}" == 0 ]] || JOB_BLOCKERS+=(clone_active)
  fi
  self_dataset_ok || true
  for i in "${!JD_NAME[@]}"; do
    name="${JD_NAME[i]}"
    ds="${POOL}/${name}"
    new="${ds}@${JOB_SNAPSHOT}"
    base=""
    JD_MODE[i]=""
    JD_BASE_GUID[i]=""
    JD_BASE_TYPE[i]=""
    JD_PROBLEM[i]=""
    if [[ "${CHECK_RESULT}" == ok ]]; then
      case "${M_STATE[${name}]:-}" in
        "") JD_PROBLEM[i]=not_in_mirror_conf ;;
        absent) JD_MODE[i]=full ;;
        foreign) JD_PROBLEM[i]=target_exists ;;
        mirror)
          [[ "${M_PROPS[${name}]}" == ok ]] || add_unique JOB_BLOCKERS props_mismatch
          guid="${M_GUID[${name}]}"
          JD_PROBLEM[i]=base_missing
          if [[ "${guid}" =~ ${GUID_RE} ]] && own_snapshots "${ds}"; then
            for j in "${!OS_NAMES[@]}"; do
              if [[ "${OS_GUIDS[j]}" == "${guid}" && "${OS_NAMES[j]}" != "${JOB_SNAPSHOT}" ]]; then
                base="${ds}@${OS_NAMES[j]}"
                JD_BASE_TYPE[i]=snapshot
              fi
            done
            if [[ -z "${base}" ]] && own_bookmarks "${ds}"; then
              for j in "${!OB_NAMES[@]}"; do
                if [[ "${OB_GUIDS[j]}" == "${guid}" ]]; then
                  base="${ds}#${OB_NAMES[j]}"
                  JD_BASE_TYPE[i]=bookmark
                fi
              done
            fi
            if [[ -n "${base}" ]]; then
              JD_MODE[i]=incremental
              JD_BASE_GUID[i]="${guid}"
              JD_PROBLEM[i]=""
            fi
          fi
          ;;
      esac
    fi
    if [[ "${name}" == appdata && "${SELF_DATASET_STATE}" != found && -z "${JD_PROBLEM[i]}" ]]; then
      JD_PROBLEM[i]=self_dataset_missing
    fi
    if [[ -n "${JD_PROBLEM[i]}" ]]; then
      add_unique JOB_BLOCKERS "${JD_PROBLEM[i]}"
    fi
    JD_DELETED[i]=""
    JD_MODIFIED[i]=""
    JD_ADDED[i]=""
    JD_RENAMED[i]=""
    JD_BASE_REFERENCED[i]=""
    JD_SIZE[i]=""
    if referenced_of "${new}"; then
      JD_REFERENCED[i]="${REFERENCED}"
    else
      failed=1
    fi
    if [[ "${JD_BASE_TYPE[i]}" == snapshot ]]; then
      if count_diff "${base}" "${new}" && referenced_of "${base}"; then
        JD_DELETED[i]="${DIFF_DELETED}"
        JD_MODIFIED[i]="${DIFF_MODIFIED}"
        JD_ADDED[i]="${DIFF_ADDED}"
        JD_RENAMED[i]="${DIFF_RENAMED}"
        JD_BASE_REFERENCED[i]="${REFERENCED}"
        deletions=$((deletions + DIFF_DELETED))
        if ((JD_REFERENCED[i] * 100 < JD_BASE_REFERENCED[i] * 50)); then
          add_unique JOB_CONFIRM_REASONS shrunk
        fi
      else
        failed=1
      fi
    elif [[ "${JD_BASE_TYPE[i]}" == bookmark ]]; then
      add_unique JOB_CONFIRM_REASONS no_diff
    fi
    if [[ "${JD_MODE[i]}" == full ]]; then
      estimate_size "${new}" || failed=1
    elif [[ -n "${base}" ]]; then
      estimate_size "${new}" "${base}" || failed=1
    else
      SEND_SIZE=""
    fi
    JD_SIZE[i]="${SEND_SIZE}"
    if [[ -n "${SEND_SIZE}" ]]; then
      total=$((total + SEND_SIZE))
    fi
  done
  if ((deletions > MAX_DELETIONS)); then
    add_unique JOB_CONFIRM_REASONS deletions
  fi
  # Platz auf Groß für die ganze Menge plus Reserve (wie space-low in guard.sh).
  if [[ "${CHECK_RESULT}" == ok ]]; then
    if [[ ! "${S_AVAILABLE}" =~ ^[0-9]+$ || ! "${S_SIZE}" =~ ^[0-9]+$ || ! "${S_RESERVE}" =~ ^[0-9]+$ ]] ||
      (((S_AVAILABLE - total) * 100 < S_RESERVE * S_SIZE)); then
      add_unique JOB_BLOCKERS space_low
    fi
  fi
  if ((failed)); then
    JOB_STATE=failed
    JOB_RESULT=prepare_failed
    return 0
  fi
  JOB_SEND_READY=false
  if ((${#JOB_BLOCKERS[@]} == 0)); then
    JOB_SEND_READY=true
  fi
  JOB_STATE=prepared
  if ((${#JOB_CONFIRM_REASONS[@]} > 0)); then
    JOB_STATE=awaiting_confirm
  fi
}

# Fortschritt aus den Zeilen von zfs send -v -P ("hh:mm:ss<TAB><Byte><TAB><Snapshot>").
readonly PROGRESS_RE=$'^[0-9]{2}:[0-9]{2}:[0-9]{2}\t([0-9]{1,19})\t'
progress_reader() {
  local index="$1" line
  while IFS= read -r line; do
    if [[ "${line}" =~ ${PROGRESS_RE} ]]; then
      write_atomic "${PRIVATE_DIR}/child/progress" "${index} ${BASH_REMATCH[1]}"$'\n' 0600 || true
    fi
  done
}

# Nach einem erfolgreichen Senden: Hold an den neuen Snapshot, Lesezeichen, alter Hold frei.
new_base() {
  local ds="$1" snapshot="$2" i
  if ! "${ZFS}" hold -- "${HOLD_TAG}" "${ds}@${snapshot}" >/dev/null 2>&1; then
    log "dataset ${ds#"${POOL}/"}: hold on the new base failed"
  fi
  if ! "${ZFS}" bookmark -- "${ds}@${snapshot}" "${ds}#${snapshot}" >/dev/null 2>&1; then
    log "dataset ${ds#"${POOL}/"}: bookmark failed"
  fi
  own_snapshots "${ds}" || return 0
  for i in "${!OS_NAMES[@]}"; do
    if [[ "${OS_NAMES[i]}" != "${snapshot}" ]] && ((OS_HOLDS[i] > 0)); then
      "${ZFS}" release -- "${HOLD_TAG}" "${ds}@${OS_NAMES[i]}" >/dev/null 2>&1 || true
    fi
  done
}

# send im Kindprozess (Konzept 3.6): Datasets nacheinander, Ergebnis aus der Antwort von guard.sh.
# shellcheck disable=SC2034 # JD_* liest save_job über Namensreferenzen.
child_send() {
  local i name ds new base err refused failed=0 j
  local -a statuses=() increment=()
  if ! ssh_key_ready || [[ -z "${TARGET_HOST}" ]]; then
    JOB_STATE=failed
    JOB_RESULT=send_failed
    JOB_REASON=key_missing
    return 0
  fi
  ssh_args
  for i in "${!JD_NAME[@]}"; do
    if ((failed)); then
      JD_RESULT[i]=skipped
      continue
    fi
    name="${JD_NAME[i]}"
    ds="${POOL}/${name}"
    new="${ds}@${JOB_SNAPSHOT}"
    base=""
    increment=()
    if [[ "${JD_MODE[i]}" == incremental ]]; then
      if [[ "${JD_BASE_TYPE[i]}" == snapshot ]] && own_snapshots "${ds}"; then
        for j in "${!OS_NAMES[@]}"; do
          [[ "${OS_GUIDS[j]}" == "${JD_BASE_GUID[i]}" ]] && base="${ds}@${OS_NAMES[j]}"
        done
      elif own_bookmarks "${ds}"; then
        for j in "${!OB_NAMES[@]}"; do
          [[ "${OB_GUIDS[j]}" == "${JD_BASE_GUID[i]}" ]] && base="${ds}#${OB_NAMES[j]}"
        done
      fi
      if [[ -z "${base}" ]]; then
        JD_RESULT[i]=base_missing
        JOB_RESULT=send_failed
        JOB_REASON=base_missing
        failed=1
        continue
      fi
      increment=(-i "${base}")
    elif [[ "${JD_MODE[i]}" != full ]]; then
      JD_RESULT[i]=not_ready
      JOB_RESULT=send_failed
      JOB_REASON=not_ready
      failed=1
      continue
    fi
    if [[ "${name}" == appdata ]] && ! self_dataset_ok; then
      JD_RESULT[i]=not_ready
      JOB_RESULT=send_failed
      JOB_REASON=self_dataset_missing
      failed=1
      continue
    fi
    log "sending ${name} (${JD_MODE[i]})"
    statuses=(0 0)
    if ! "${ZFS}" send -L -c -e -v -P "${increment[@]+"${increment[@]}"}" -- "${new}" \
      2> >(progress_reader "${i}") |
      "${SSH}" "${SSH_ARGS[@]}" "receive ${name}" >/dev/null 2>"${PRIVATE_DIR}/child/ssh.err"; then
      statuses=("${PIPESTATUS[@]}")
    fi
    err="$(head -c 4096 -- "${PRIVATE_DIR}/child/ssh.err" 2>/dev/null)" || err=""
    if ((statuses[0] == 0 && statuses[1] == 0)); then
      new_base "${ds}" "${JOB_SNAPSHOT}"
      JD_RESULT[i]=ok
      JD_SENT[i]="${JD_SIZE[i]}"
      log "sent ${name}"
      continue
    fi
    failed=1
    JOB_RESULT=send_failed
    if ((statuses[1] == 3)) && [[ "${err}" =~ twinraid-guard:\ refused\ ([a-z][a-z-]{0,39}) ]]; then
      refused="${BASH_REMATCH[1]}"
      if [[ "${refused}" == target-modified ]]; then
        JOB_RESULT=target_modified
      else
        JOB_RESULT=target_refused
        JOB_REASON=refused_other
        if in_list "${refused}" "${SEND_REFUSALS[@]}"; then
          JOB_REASON="${refused//-/_}"
        fi
      fi
    elif ((statuses[1] != 0)); then
      ssh_failure "${statuses[1]}" "${err}"
      JOB_REASON="${SSH_CODE}"
    else
      JOB_REASON=send_error
    fi
    JD_RESULT[i]="${JOB_RESULT}"
    log "sending ${name} failed (${JOB_RESULT}${JOB_REASON:+ ${JOB_REASON}})"
  done
  if ((failed)); then
    JOB_STATE=failed
    return 0
  fi
  JOB_STATE="done"
  JOB_RESULT=ok
  retention_klein
}

# Aufbewahrung auf Klein (Konzept 3.6, E25b): eigene Snapshots älter als snapshot_keep_days
# (creation, Uhr von Klein); nie mit Hold (die Basis), nie mit Klon, nie der Snapshot eines
# offenen Jobs, nie mit -r.
retention_klein() {
  local name ds i limit destroyed=0 failed=0 snap clones
  now
  limit=$((NOW - SNAPSHOT_KEEP_DAYS * DAY))
  for name in "${DATASETS[@]+"${DATASETS[@]}"}"; do
    ds="${POOL}/${name}"
    own_snapshots "${ds}" || continue
    for i in "${!OS_NAMES[@]}"; do
      if ((OS_HOLDS[i] > 0 || OS_CREATED[i] >= limit)); then
        continue
      fi
      if [[ "${OS_NAMES[i]}" == "${JOB_SNAPSHOT}" ]] && job_is_open; then
        continue
      fi
      snap="${ds}@${OS_NAMES[i]}"
      clones="$(zfs_call "${ZFS}" get -H -o value -- clones "${snap}" 2>/dev/null)" || continue
      if [[ -n "${clones}" && "${clones}" != - ]]; then
        continue
      fi
      if zfs_call "${ZFS}" destroy -- "${snap}" >/dev/null 2>&1; then
        destroyed=$((destroyed + 1))
      else
        failed=$((failed + 1))
      fi
    done
  done
  if ((destroyed > 0 || failed > 0)); then
    log "retention: ${destroyed} own snapshots destroyed, ${failed} failed"
  fi
}

run_child() {
  local kind="$1" parent="$2" watcher
  need_root
  [[ "${parent}" =~ ^[0-9]{1,9}$ ]] || exit 2
  load_config || exit 1
  load_job_from "${PRIVATE_DIR}/job" || exit 1
  watch_parent "${parent}" &
  watcher=$!
  case "${kind}" in
    prepare) child_prepare ;;
    send) child_send ;;
    *) exit 2 ;;
  esac
  save_job "${PRIVATE_DIR}/child/job"
  kill "${watcher}" 2>/dev/null || true
}

# --- Aufträge snapshot, prepare, confirm, send, close -------------------------------------

order_snapshot() {
  local name ds out used available stamp
  local -a snapshots=()
  if job_is_open; then
    RESULT=job_open
    return 0
  fi
  if ! zfs_ready; then
    RESULT=zfs_unavailable
    return 0
  fi
  if ((${#DATASETS[@]} == 0)); then
    RESULT=no_datasets
    return 0
  fi
  for name in "${DATASETS[@]}"; do
    out="$(zfs_call "${ZFS}" list -H -o name,type -- "${POOL}/${name}" 2>/dev/null)" || out=""
    if [[ "${out}" != "${POOL}/${name}"$'\t'filesystem ]]; then
      RESULT=dataset_missing
      return 0
    fi
  done
  now
  if ((LAST_SNAPSHOT_UP > 0 && LAST_SNAPSHOT_UP <= UP && UP - LAST_SNAPSHOT_UP < SNAPSHOT_GAP)); then
    RESULT=too_soon
    return 0
  fi
  out="$(zfs_call "${ZFS}" list -H -p -o used,available -- "${POOL}" 2>/dev/null)" || out=""
  if [[ ! "${out}" =~ ^([0-9]{1,19})$'\t'([0-9]{1,19})$ ]]; then
    RESULT=zfs_unavailable
    return 0
  fi
  used="${BASH_REMATCH[1]}"
  available="${BASH_REMATCH[2]}"
  if ((available * 100 < SNAPSHOT_RESERVE_PERCENT * (used + available))); then
    RESULT=snapshot_low_space
    return 0
  fi
  for name in "${DATASETS[@]}"; do
    ds="${POOL}/${name}"
    if ! own_snapshots "${ds}" || ((${#OS_NAMES[@]} >= SNAPSHOT_LIMIT)); then
      RESULT=snapshot_limit
      return 0
    fi
  done
  stamp="$(date -u -d "@${NOW}" +%Y%m%dT%H%M%SZ)"
  for name in "${DATASETS[@]}"; do
    snapshots+=("${POOL}/${name}@${SNAPSHOT_PREFIX}${stamp}")
  done
  # Ein Aufruf für alle Datasets: ein Pool, also atomar.
  if ! zfs_call "${ZFS}" snapshot -o "${CREATED_BY}=source-guard" -- "${snapshots[@]}" >/dev/null 2>&1; then
    RESULT=snapshot_failed
    return 0
  fi
  LAST_SNAPSHOT_UP="${UP}"
  write_atomic "${PRIVATE_DIR}/last_snapshot" "${UP}" 0600 || true
  clear_job
  JOB_ID="${O_SEQ}"
  JOB_STATE=snapshot
  JOB_SNAPSHOT="${SNAPSHOT_PREFIX}${stamp}"
  JOB_CREATED="${NOW}"
  # Ohne prepare, send oder close schließt der Wächter den Job nach CONFIRM_LIMIT.
  JOB_DEADLINE=$((UP + CONFIRM_LIMIT))
  for name in "${DATASETS[@]}"; do
    JD_NAME+=("${name}")
  done
  save_job
  log "snapshot ${JOB_SNAPSHOT} created for ${#DATASETS[@]} datasets (job ${JOB_ID})"
  RESULT=created
}

order_prepare() {
  if ! job_is_open; then
    RESULT=no_job
  elif [[ "${JOB_STATE}" != snapshot ]]; then
    RESULT=wrong_state
  elif ((RUN_OPEN)); then
    RESULT=run_open
  elif ! start_child prepare; then
    RESULT=wrong_state
  else
    now
    JOB_STATE=preparing
    JOB_DEADLINE=$((UP + PREPARE_LIMIT))
    save_job
    RESULT=preparing
  fi
}

order_confirm() {
  if ! job_is_open; then
    RESULT=no_job
  elif [[ "${JOB_STATE}" != awaiting_confirm ]]; then
    RESULT=not_needed
  else
    JOB_STATE=prepared
    JOB_CONFIRMED=1
    save_job
    RESULT=confirmed
  fi
}

order_send() {
  if ! job_is_open; then
    RESULT=no_job
  elif [[ "${JOB_STATE}" != prepared ]]; then
    RESULT=wrong_state
  elif [[ "${JOB_SEND_READY}" != true ]]; then
    RESULT=not_ready
  elif ! start_child send; then
    RESULT=wrong_state
  else
    now
    JOB_STATE=sending
    JOB_DEADLINE=$((UP + SEND_LIMIT))
    save_job
    RESULT=sending
  fi
}

order_close() {
  if ! job_is_open; then
    RESULT=no_job
    return 0
  fi
  stop_child
  JOB_STATE=closed
  JOB_RESULT=closed
  JOB_DEADLINE=0
  save_job
  RESULT=closed
}

# --- Key „sync“ und Hostkey von Groß (init-key, trust-target) -----------------------------

# IPv4-Adresse von target_host (Hostname über getent). Setzt TARGET_IP.
target_ip() {
  local out
  TARGET_IP=""
  if [[ "${TARGET_HOST}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
    TARGET_IP="${TARGET_HOST}"
    return 0
  fi
  out="$(getent ahostsv4 -- "${TARGET_HOST}" 2>/dev/null | head -1)" || return 1
  [[ "${out}" =~ ^([0-9]{1,3}(\.[0-9]{1,3}){3})[[:space:]] ]] || return 1
  TARGET_IP="${BASH_REMATCH[1]}"
}

cmd_init_key() {
  local replace=0 src="" out pub
  need_root
  if [[ "${1:-}" == --new ]]; then
    replace=1
  elif [[ -n "${1:-}" ]]; then
    usage
  fi
  load_config || die "Konfiguration ungültig: ${CONFIG_DETAIL} (Zeile ${CONFIG_LINE})"
  [[ -n "${TARGET_HOST}" ]] || die "target_host fehlt in source-guard.conf"
  target_ip || die "target_host lässt sich nicht auflösen"
  out="$("${IP}" -4 route get "${TARGET_IP}" 2>/dev/null)" || out=""
  if [[ "${out}" =~ [[:space:]]src[[:space:]]([0-9]{1,3}(\.[0-9]{1,3}){3}) ]]; then
    src="${BASH_REMATCH[1]}"
  fi
  [[ -n "${src}" ]] || die "die eigene Adresse auf dem Weg zu ${TARGET_IP} ist unbekannt"
  if ((replace)); then
    rm -f -- "${SYNC_KEY}" "${SYNC_KEY}.pub"
  fi
  if [[ ! -f "${SYNC_KEY}" ]]; then
    "${SSH_KEYGEN}" -q -t ed25519 -N '' -C twinraid-sync -f "${SYNC_KEY}" </dev/null >/dev/null ||
      die "ssh-keygen ist gescheitert"
    echo "Der Key sync wurde angelegt: ${SYNC_KEY}"
  else
    echo "Der Key sync ist schon vorhanden: ${SYNC_KEY}"
  fi
  pub="$(<"${SYNC_KEY}.pub")"
  [[ "${pub}" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]{20,200}\ twinraid-sync$ ]] ||
    die "${SYNC_KEY}.pub hat nicht das erwartete Format"
  echo "Auf Groß unter Users → root → SSH authorized keys eintragen:"
  echo
  echo "restrict,from=\"${src}\",command=\"/bin/bash /boot/config/twinraid/guard.sh sync\" ${pub}"
}

cmd_trust_target() {
  local expected="" scan line fingerprint answer tmp
  need_root
  if [[ "${1:-}" == --fingerprint && -n "${2:-}" ]]; then
    expected="$2"
    [[ "${expected}" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] || die "Fingerabdruck im Format SHA256:… angeben"
  elif [[ -n "${1:-}" ]]; then
    usage
  fi
  load_config || die "Konfiguration ungültig: ${CONFIG_DETAIL} (Zeile ${CONFIG_LINE})"
  [[ -n "${TARGET_HOST}" ]] || die "target_host fehlt in source-guard.conf"
  scan="$(timeout 15 "${SSH_KEYSCAN}" -t ed25519 -p "${TARGET_PORT}" -- "${TARGET_HOST}" 2>/dev/null)" || scan=""
  line=""
  while IFS= read -r answer; do
    if [[ "${answer}" =~ ^(\[[A-Za-z0-9.-]+\]:[0-9]{1,5}|[A-Za-z0-9.-]+)\ ssh-ed25519\ [A-Za-z0-9+/=]{20,200}$ ]]; then
      line="${answer}"
    fi
  done <<<"${scan}"
  [[ -n "${line}" ]] || die "Groß antwortet nicht mit einem ed25519-Hostkey (ssh-keyscan)"
  tmp="$(mktemp)"
  printf '%s\n' "${line}" >"${tmp}"
  fingerprint="$("${SSH_KEYGEN}" -l -f "${tmp}" 2>/dev/null | awk '{print $2}')" || fingerprint=""
  [[ "${fingerprint}" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] || {
    rm -f -- "${tmp}"
    die "Fingerabdruck nicht lesbar"
  }
  echo "Hostkey von Groß: ED25519 ${fingerprint}"
  echo "Auf Groß vergleichen mit: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub"
  if [[ -n "${expected}" ]]; then
    if [[ "${expected}" != "${fingerprint}" ]]; then
      rm -f -- "${tmp}"
      die "Der Fingerabdruck stimmt nicht überein."
    fi
  else
    printf 'Stimmt er überein? Mit yes bestätigen: '
    IFS= read -r answer || answer=""
    if [[ "${answer}" != yes ]]; then
      rm -f -- "${tmp}"
      die "Nicht bestätigt; nichts gespeichert."
    fi
  fi
  write_atomic "${KNOWN_HOSTS}" "${line}"$'\n' 0600 || {
    rm -f -- "${tmp}"
    die "${KNOWN_HOSTS} ließ sich nicht schreiben"
  }
  rm -f -- "${tmp}"
  echo "Hostkey gespeichert: ${KNOWN_HOSTS}"
  ensure_dir "${BASE_DIR}" 0755 || die "${BASE_DIR} ist nicht nutzbar"
  ensure_dir "${PRIVATE_DIR}" 0700 || die "${PRIVATE_DIR} ist nicht nutzbar"
  if guard_check; then
    echo "OK: guard.sh antwortet (${CHECK_LINES[0]})."
  else
    echo "Test mit check gescheitert: ${CHECK_RESULT}"
    exit 1
  fi
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

# Zahl oder null; GUID als Text oder null; Code als Text oder null. Setzen JSON.
json_value() {
  case "$2" in
    number) json_number "$1" ;;
    guid) json_token "$1" "${GUID_RE}" ;;
    code) json_token "$1" "${CODE_RE}" ;;
  esac
}

# Liste fester Codes als JSON-Array. Setzt JSON.
json_codes() {
  local code list=""
  for code in "$@"; do
    if [[ "${code}" =~ ${CODE_RE} ]]; then
      list+="${list:+,}\"${code}\""
    fi
  done
  JSON="[${list}]"
}

# Ergebnis von guard.sh check aus den streng geprüften Zeilen (Konzept 3.6). Setzt JSON.
# Die Treffer kommen zuerst nach match, weil json_number und json_token BASH_REMATCH
# mit ihren eigenen Vergleichen überschreiben.
check_json() {
  local line item list="" summary=null field
  local -a match=()
  if [[ -z "${CHECK_RESULT}" ]]; then
    JSON=null
    return 0
  fi
  for line in "${CHECK_LINES[@]+"${CHECK_LINES[@]}"}"; do
    if [[ "${line}" =~ ${CHECK_MIRROR_RE} ]]; then
      match=("${BASH_REMATCH[@]}")
      item='{"name":"'"${match[1]}"'","state":"'"${match[2]}"'"'
      if [[ "${match[3]}" == - ]]; then
        item+=',"props":null'
      else
        item+=',"props":"'"${match[3]}"'"'
      fi
      json_number "${match[4]}"
      item+=',"count":'"${JSON}"
      json_token "${match[5]}" "${GUID_RE}"
      item+=',"newest_guid":'"${JSON}"
      json_number "${match[6]}"
      item+=',"newest_created":'"${JSON}"
      json_number "${match[7]}"
      item+=',"newest_received":'"${JSON}"
      json_number "${match[8]}"
      item+=',"oldest_created":'"${JSON}"'}'
      list+="${list:+,}${item}"
    elif [[ "${line}" =~ ${CHECK_SUMMARY_RE} ]]; then
      match=("${BASH_REMATCH[@]}")
      summary='{"root":"'"${match[1]}"'","conf":"'"${match[2]}"'"'
      summary+=',"deleg":"'"${match[3]//-/_}"'","receive":"'"${match[4]}"'"'
      for field in 5:clones 6:available 7:size 8:reserve_percent 9:keep_days 10:min_interval_minutes; do
        json_number "${match[${field%%:*}]}"
        summary+=',"'"${field#*:}"'":'"${JSON}"
      done
      summary+='}'
    fi
  done
  json_token "${CHECK_RESULT}" "${CODE_RE}"
  JSON='{"at":'"${CHECK_AT}"',"result":'"${JSON}"',"summary":'"${summary}"',"mirrors":['"${list}"']}'
}

# Der Job für den Status; beim Senden mit dem Fortschritt aus private/child/progress.
job_json() {
  local item list="" i field kind current=-1 bytes="" key value
  if ((JOB_ID == 0)); then
    JSON=null
    return 0
  fi
  if [[ "${JOB_STATE}" == sending && -f "${PRIVATE_DIR}/child/progress" ]]; then
    read -r key value <"${PRIVATE_DIR}/child/progress" 2>/dev/null || true
    if [[ "${key:-}" =~ ^[0-9]{1,2}$ && "${value:-}" =~ ^[0-9]{1,19}$ ]]; then
      current="${key}"
      bytes="${value}"
    fi
  fi
  item='{"id":'"${JOB_ID}"',"state":"'"${JOB_STATE}"'"'
  json_token "${JOB_RESULT}" "${CODE_RE}"
  item+=',"result":'"${JSON}"
  json_token "${JOB_REASON}" "${CODE_RE}"
  item+=',"reason":'"${JSON}"
  json_token "${JOB_SNAPSHOT}" "${SNAPNAME_RE}"
  item+=',"snapshot":'"${JSON}"',"created_at":'"${JOB_CREATED}"
  if ((JOB_DEADLINE > 0)); then
    item+=',"deadline":'"$((NOW + JOB_DEADLINE - UP))"
  else
    item+=',"deadline":null'
  fi
  if ((JOB_CONFIRMED)); then item+=',"confirmed":true'; else item+=',"confirmed":false'; fi
  json_codes "${JOB_CONFIRM_REASONS[@]+"${JOB_CONFIRM_REASONS[@]}"}"
  item+=',"confirm_reasons":'"${JSON}"
  case "${JOB_SEND_READY}" in
    true | false) item+=',"send_ready":'"${JOB_SEND_READY}" ;;
    *) item+=',"send_ready":null' ;;
  esac
  json_codes "${JOB_BLOCKERS[@]+"${JOB_BLOCKERS[@]}"}"
  item+=',"blockers":'"${JSON}"
  for i in "${!JD_NAME[@]}"; do
    local -a fields=()
    for field in "${JD_FIELDS[@]}"; do
      # shellcheck disable=SC2178 # Namensreferenz auf eine der Listen JD_*.
      local -n jd_ref="JD_${field^^}"
      value="${jd_ref[i]:-}"
      unset -n jd_ref
      if [[ "${field}" == sent && "${JOB_STATE}" == sending ]]; then
        value=""
        if ((current >= 0 && i < current)); then
          value="${JD_SIZE[i]:-}"
        elif ((i == current)); then
          value="${bytes}"
        fi
      fi
      case "${field}" in
        base_guid) kind=guid ;;
        name | mode | base_type | problem | result) kind=text ;;
        *) kind=number ;;
      esac
      if [[ "${kind}" == text ]]; then
        jd_pattern "${field}"
        json_token "${value}" "${PATTERN}"
      else
        json_value "${value}" "${kind}"
      fi
      fields+=("\"${field}\":${JSON}")
    done
    list+="${list:+,}{$(IFS=,; printf '%s' "${fields[*]}")}"
  done
  item+=',"datasets":['"${list}"']}'
  JSON="${item}"
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
  # Ziel für das Senden: ob eingetragen, Port, Key und Hostkey vorhanden, letztes check;
  # keine Adresse.
  present=false
  if [[ -n "${TARGET_HOST}" ]]; then present=true; fi
  json+=',"target":{"configured":'"${present}"',"port":'"${TARGET_PORT}"
  present=false
  if [[ -f "${SYNC_KEY}" && ! -L "${SYNC_KEY}" ]]; then present=true; fi
  json+=',"key":'"${present}"
  present=false
  if [[ -f "${KNOWN_HOSTS}" && ! -L "${KNOWN_HOSTS}" ]]; then present=true; fi
  json+=',"known_host":'"${present}"
  check_json
  json+=',"check":'"${JSON}"'}'
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
    item+=',"compression":'"${JSON}"
    json_number "${DS_SNAPSHOTS[i]:-}"
    item+=',"snapshots":'"${JSON}"
    json_number "${DS_OLDEST[i]:-}"
    item+=',"oldest_snapshot_at":'"${JSON}"
    if [[ "${DS_BASE_GUID[i]:-}" =~ ${GUID_RE} && "${DS_BASE_CREATED[i]:-}" =~ ^[0-9]{1,12}$ ]]; then
      item+=',"base":{"guid":"'"${DS_BASE_GUID[i]}"'","created_at":'"${DS_BASE_CREATED[i]}"'}'
    else
      item+=',"base":null'
    fi
    case "${DS_BOOKMARK[i]:-}" in
      true | false) item+=',"bookmark":'"${DS_BOOKMARK[i]}"'}' ;;
      *) item+=',"bookmark":null}' ;;
    esac
    list+="${list:+,}${item}"
  done
  json+=',"datasets":['"${list}"'],"self_dataset":"'"${SELF_DATASET_STATE}"'"}'
  job_json
  json+=',"job":'"${JSON}"
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
  local was_docker=-1 sleeper="" first_parent value
  need_root
  now
  STARTED_AT="${NOW}"
  parent_pid
  first_parent="${PARENT}"
  prepare_layout || true
  ensure_dir "${PRIVATE_DIR}" 0700 || die "private/ ist nicht nutzbar"
  # Eigene Sperre: Nie lesen zwei Wächter gleichzeitig Aufträge, auch nicht kurz nach
  # einem Neustart, während ein alter Wächter noch endet.
  exec {RUN_LOCK}>>"${PRIVATE_DIR}/run.lock"
  if ! flock -w 10 "${RUN_LOCK}"; then
    log "another guard process still runs, ending"
    exit 1
  fi
  init_private
  if ! load_config; then
    log "config invalid: ${CONFIG_DETAIL} in line ${CONFIG_LINE}"
  fi
  # Job und letztes check (ab Version 2). Ein Kindprozess eines früheren Wächters lebt nur
  # nach kill -9 noch; er wird beendet, sein Job gilt als gescheitert.
  stop_stale_child
  if [[ -f "${PRIVATE_DIR}/job" ]] && ! load_job_from "${PRIVATE_DIR}/job"; then
    log "saved job is unreadable and was dropped"
    rm -f -- "${PRIVATE_DIR}/job"
  fi
  case "${JOB_STATE}" in
    preparing | sending)
      JOB_STATE=failed
      JOB_RESULT=interrupted
      JOB_DEADLINE=0
      save_job
      log "job ${JOB_ID}: failed (interrupted)"
      ;;
  esac
  load_check_from "${PRIVATE_DIR}/check" || true
  if [[ -f "${PRIVATE_DIR}/last_snapshot" ]]; then
    value="$(<"${PRIVATE_DIR}/last_snapshot")"
    if [[ "${value}" =~ ^[0-9]{1,12}$ ]]; then
      LAST_SNAPSHOT_UP="${value}"
    fi
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
    poll_child
    check_job_deadline
    # Aufbewahrung auf Klein einmal täglich, wenn kein Job offen ist (nach dem Senden
    # erledigt sie der Kindprozess).
    if [[ "${ZFS_STATE}" == ok && -z "${CONFIG_ERROR}" ]] && ! job_is_open &&
      ((LAST_RETENTION_UP == 0 || UP - LAST_RETENTION_UP >= RETENTION_INTERVAL)); then
      retention_klein
      LAST_RETENTION_UP="${UP}"
      next_zfs=0
    fi
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
  # Ein laufendes prepare oder send endet mit dem Wächter (Konzept 3.6).
  interrupt_job
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
  local lock i
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
  # Höchstens 5 s warten, bis die Überwachungsschleife ihre PID geschrieben hat, damit
  # status direkt danach stimmt (D2).
  for ((i = 0; i < 50; i++)); do
    if supervisor_pid; then
      echo "Der Wächter wurde gestartet."
      return 0
    fi
    sleep 0.1
  done
  echo "Der Wächter wurde gestartet, meldet sich aber noch nicht; in einigen Sekunden status aufrufen."
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
        source-guard.sh init-key [--new] | trust-target [--fingerprint SHA256:…]
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
  init-key)
    [[ $# -le 2 ]] || usage
    cmd_init_key "${2:-}"
    ;;
  trust-target)
    [[ $# -le 3 ]] || usage
    cmd_trust_target "${2:-}" "${3:-}"
    ;;
  supervise) supervise ;;
  run) run_guard ;;
  child)
    [[ $# -eq 3 ]] || usage
    run_child "$2" "$3"
    ;;
  *) usage ;;
esac
