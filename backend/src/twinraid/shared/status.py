"""Statusmodelle, die der Worker liefert und die Oberfläche anzeigt (Konzept 4.1 und 3.5)."""

from datetime import datetime
from enum import StrEnum

from twinraid.shared.actions import ActionsSnapshot
from twinraid.shared.base import StrictModel
from twinraid.shared.source_guard import SourceGuardStatus
from twinraid.shared.sync import SyncStatus


class ResourceState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    STARTING = "starting"
    STOPPING = "stopping"
    UNKNOWN = "unknown"


class StatusReason(StrEnum):
    """Feste Liste von Ursachen. Kein Text aus fremden Antworten gelangt in den Status.

    Einzige Ausnahme sind die bereinigten Namen weiterer laufender VMs (Konzept 3.5).
    """

    # Server: Der TCP-Check auf den WebGUI-Port ist gescheitert.
    UNREACHABLE = "unreachable"
    # Klein: TwinRaid läuft auf diesem Server, er gilt ohne Prüfung als online (Konzept 4.1).
    RUNS_HERE = "runs_here"
    # VM: Groß ist offline, die VM kann nicht laufen.
    TARGET_OFFLINE = "target_offline"
    # VM: Groß wurde geweckt und ist noch nicht bereit (Konzept 4.2).
    TARGET_STARTING = "target_starting"
    # Sitzung vm-watch mit dem Key „status“ (Konzept 3.5, ab v0.4.3).
    WATCH_CONNECTING = "watch_connecting"
    WATCH_INTERRUPTED = "watch_interrupted"
    SSH_KEY_MISSING = "ssh_key_missing"
    SSH_HOST_UNTRUSTED = "ssh_host_untrusted"
    SSH_HOST_KEY_MISMATCH = "ssh_host_key_mismatch"
    SSH_AUTH_FAILED = "ssh_auth_failed"
    SSH_UNREACHABLE = "ssh_unreachable"
    SSH_DENIED = "ssh_denied"
    SSH_FAILED = "ssh_failed"
    GUARD_OUTDATED = "guard_outdated"
    # Meldungen von guard.sh zur VM
    VM_NOT_CONFIGURED = "vm_not_configured"
    VM_NOT_FOUND = "vm_not_found"
    VM_SERVICE_UNAVAILABLE = "vm_service_unavailable"
    # VM-Zustände mit Hinweis
    VM_PAUSED = "vm_paused"
    VM_SUSPENDED = "vm_suspended"
    VM_CRASHED = "vm_crashed"


class StatusNotice(StrEnum):
    """Feste Hinweise zu einer Kachel; sie ändern weder Zustand noch Aktionen (Konzept 4.1)."""

    # Auf Groß ist das Plugin Unraid Connect installiert (Konzept 3.5).
    TARGET_CONNECT_PLUGIN = "target_connect_plugin"
    # Auf Groß ist Flash Backup von Unraid Connect aktiv.
    TARGET_FLASH_BACKUP = "target_flash_backup"
    # Die VM aus power.conf heißt anders als gaming_vm.unraid_vm_name.
    VM_NAME_MISMATCH = "vm_name_mismatch"
    # Auf Klein ist Unraid Connect installiert bzw. Flash Backup aktiv (Wächter, Konzept 3.6).
    SOURCE_CONNECT_PLUGIN = "source_connect_plugin"
    SOURCE_FLASH_BACKUP = "source_flash_backup"
    # Die eigene Container-ID steht in der Erlaubnisliste des Wächters (Gegenprüfung).
    SOURCE_GUARD_ALLOWS_SELF = "source_guard_allows_self"
    # Auf Groß ist keine Grenze für den ZFS-Cache eingetragen (arc_max 0, Konzept 3.5, ab v0.5.1).
    TARGET_ARC_UNLIMITED = "target_arc_unlimited"


class ResourceInfo(StrictModel):
    state: ResourceState
    reason: StatusReason | None = None


class TargetZfs(StrictModel):
    """Grenze des ZFS-Caches auf Groß aus vm-watch (Konzept 3.5, ab v0.5.1), in Byte.

    None: ZFS ist auf Groß nicht geladen oder der Wert ist nicht lesbar.
    """

    arc_max: int | None
    c_max: int | None


class WatchRetry(StrictModel):
    """Nächster Versuch der Statusverbindung vm-watch (Konzept 3.5, ab v0.4.4)."""

    # Zeitpunkt des nächsten Versuchs, auf volle Sekunden aufgerundet.
    at: datetime
    # Pause nach einem dauerhaften Fehler; ein erfolgreiches trust-host-key beendet sie sofort.
    paused: bool


UNKNOWN = ResourceInfo(state=ResourceState.UNKNOWN)


class ResourceStatus(StrictModel):
    """Zustand der überwachten Ressourcen, geliefert von der Status-Integration."""

    source: ResourceInfo
    target: ResourceInfo
    vm: ResourceInfo
    # Zeitpunkt der letzten Prüfung; None, solange noch nie geprüft wurde.
    checked_at: datetime | None
    # Weitere laufende VMs auf Groß außer der Gaming-VM, bereinigt (Konzept 3.5 und 4.2).
    # None: unbekannt, weil Groß aus ist oder die VM-Liste nicht lesbar war.
    other_vms_running: tuple[str, ...] | None = None
    # Hinweise zu den Kacheln (ab v0.4.3).
    notices: tuple[StatusNotice, ...] = ()
    # Der Worker wartet nach einem Fehler der Statusverbindung (ab v0.4.4); sonst None.
    watch_retry: WatchRetry | None = None
    # Werte des ARC auf Groß (ab v0.5.1); None, solange keine Zeile watch 3 vorliegt.
    target_zfs: TargetZfs | None = None


NOT_CHECKED = ResourceStatus(source=UNKNOWN, target=UNKNOWN, vm=UNKNOWN, checked_at=None)


class StatusSnapshot(ResourceStatus):
    """Antwort auf get_status: Ressourcen, Kopierjob, Anmeldung und Aktionen."""

    # Kopierjob läuft (Konzept 4.1 und 5.3). Ab Phase 5 vom echten Sync.
    job_running: bool
    password_set: bool
    # Verfügbarkeit, laufende Aktionen und letzte Ergebnisse (Konzept 4.2).
    actions: ActionsSnapshot
    # Wächter auf Klein (Konzept 3.6, ab v0.5.0).
    source_guard: SourceGuardStatus
    # Probelauf und Sync (Konzept 4.3, ab v0.5.1).
    sync: SyncStatus
