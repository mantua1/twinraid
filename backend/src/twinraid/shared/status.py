"""Statusmodelle, die der Worker liefert und die Oberfläche anzeigt (Konzept 4.1 und 3.5)."""

from datetime import datetime
from enum import StrEnum

from twinraid.shared.actions import ActionsSnapshot
from twinraid.shared.base import StrictModel


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
    # Unraid-API (Konzept 3.5)
    API_UNREACHABLE = "api_unreachable"
    API_REDIRECTED = "api_redirected"
    API_KEY_MISSING = "api_key_missing"
    API_KEY_INVALID = "api_key_invalid"
    API_FORBIDDEN = "api_forbidden"
    API_CERT_UNTRUSTED = "api_cert_untrusted"
    API_CERT_CHANGED = "api_cert_changed"
    API_ERROR = "api_error"
    # VM-Zustände mit Hinweis
    VM_NOT_FOUND = "vm_not_found"
    VM_PAUSED = "vm_paused"
    VM_SUSPENDED = "vm_suspended"
    VM_CRASHED = "vm_crashed"


class ResourceInfo(StrictModel):
    state: ResourceState
    reason: StatusReason | None = None


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


NOT_CHECKED = ResourceStatus(source=UNKNOWN, target=UNKNOWN, vm=UNKNOWN, checked_at=None)


class StatusSnapshot(ResourceStatus):
    """Antwort auf get_status: Ressourcen, Kopierjob, Anmeldung und Aktionen."""

    # Kopierjob läuft (Konzept 4.1 und 5.3). Ab Phase 5 vom echten Sync.
    job_running: bool
    password_set: bool
    # Verfügbarkeit, laufende Aktionen und letzte Ergebnisse (Konzept 4.2).
    actions: ActionsSnapshot
