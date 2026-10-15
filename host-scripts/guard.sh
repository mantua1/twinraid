#!/bin/bash
# TwinRaid: SSH-Wächter auf Unraid Groß (Konzept 5.6).
#
# Ablage auf Groß: /boot/config/twinraid/guard.sh. /boot ist FAT32 und nicht
# ausführbar; authorized_keys ruft das Skript deshalb über /bin/bash auf:
#
#   restrict,from="<IP von TwinRaid>",command="/bin/bash /boot/config/twinraid/guard.sh power" ssh-ed25519 … twinraid-power
#
# Die gewünschte Aktion steht in SSH_ORIGINAL_COMMAND. Sie wird nur mit festen
# Mustern verglichen und nie als Befehl ausgewertet.
#
#   guard.sh power   Key „power“: check | poweroff | vm-shutdown <UUID>
#   guard.sh sync    Key „sync“ (Phase 5); bis dahin wird alles abgelehnt
#
# Exit-Code 2: Anfrage abgelehnt. Jede Anfrage steht im Syslog von Groß
# (Tools → System Log, Kennung twinraid-guard).
set -euo pipefail

readonly VERSION=1
readonly POWEROFF=/sbin/poweroff
readonly VIRSH=/usr/bin/virsh
readonly LOGGER=/usr/bin/logger
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

mode="${1:-}"
request="${SSH_ORIGINAL_COMMAND:-}"

case "${mode}" in
  power)
    case "${request}" in
      check)
        echo "twinraid-guard ${VERSION} power ok"
        ;;
      poweroff)
        log "poweroff requested by TwinRaid"
        exec "${POWEROFF}"
        ;;
      "vm-shutdown "*)
        uuid="${request#vm-shutdown }"
        [[ "${uuid}" =~ ${UUID_PATTERN} ]] || deny "invalid VM id"
        log "ACPI shutdown of VM ${uuid} requested by TwinRaid"
        exec "${VIRSH}" shutdown --domain "${uuid}"
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
