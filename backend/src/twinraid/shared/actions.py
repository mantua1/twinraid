"""Aktionen mit Außenwirkung: Namen, Sperrgründe, Schritte und Ergebnisse (Konzept 4.2 und 5.3).

Alle Werte stammen aus festen Listen. Kein Text aus fremden Antworten gelangt
in den Status, ins Aktionslog oder an die Oberfläche.
"""

from datetime import datetime
from enum import StrEnum

from twinraid.shared.base import StrictModel


class ActionName(StrEnum):
    """Die Aktionen aus der festen Befehlsliste des Workers (Konzept 3.2)."""

    WAKE_TARGET = "wake_target"
    SHUTDOWN_TARGET = "shutdown_target"
    # VM erzwingen und danach Groß herunterfahren (ab v0.4.1, Ergänzung zu E10).
    SHUTDOWN_TARGET_FORCE_VM = "shutdown_target_force_vm"
    VM_START = "vm_start"
    VM_STOP = "vm_stop"
    VM_FORCE_STOP = "vm_force_stop"


class ActionGroup(StrEnum):
    """Freischaltung im Modus real über actions.* in twinraid.yaml (Konzept 4.2, E9)."""

    WAKE = "wake"
    VM = "vm"
    SHUTDOWN = "shutdown"


class ActionBlocker(StrEnum):
    """Warum eine Aktion gerade nicht möglich ist."""

    # Im Modus real nicht in actions.* freigeschaltet.
    NOT_ENABLED = "not_enabled"
    # Eine andere Aktion belegt Groß oder die VM.
    BUSY = "busy"
    SYNC_RUNNING = "sync_running"
    TARGET_ONLINE = "target_online"
    TARGET_OFFLINE = "target_offline"
    TARGET_UNKNOWN = "target_unknown"
    VM_RUNNING = "vm_running"
    VM_OFF = "vm_off"
    VM_UNKNOWN = "vm_unknown"
    # Erzwingen erst shutdown_timeout_minutes nach dem letzten Herunterfahren-Versuch.
    FORCE_NOT_YET = "force_not_yet"
    # Kein gültiges Angebot für „Erzwingen und danach Server herunterfahren“.
    NOT_OFFERED = "not_offered"
    # Simulationsmodus: Der feste Rundlauf ist eingeschaltet (twinraid-admin mock-tour).
    TOUR = "tour"


class ActionStep(StrEnum):
    """Was eine laufende Aktion gerade tut (Zwischenzustände, Konzept 4.2)."""

    WAKING = "waking"
    WAITING_FOR_API = "waiting_for_api"
    STARTING_VM = "starting_vm"
    STOPPING_VM = "stopping_vm"
    FORCING_VM = "forcing_vm"
    SHUTTING_DOWN = "shutting_down"


class ActionOutcome(StrEnum):
    """Ergebnis einer Aktion; dieselben Werte stehen im Aktionslog (Konzept 5.3)."""

    SUCCESS = "success"
    WOL_FAILED = "wol_failed"
    BOOT_TIMEOUT = "boot_timeout"
    # Ursachen der Unraid-API, gleichnamig mit StatusReason (Konzept 3.5).
    API_UNREACHABLE = "api_unreachable"
    API_REDIRECTED = "api_redirected"
    API_KEY_MISSING = "api_key_missing"
    API_KEY_INVALID = "api_key_invalid"
    API_FORBIDDEN = "api_forbidden"
    API_CERT_UNTRUSTED = "api_cert_untrusted"
    API_CERT_CHANGED = "api_cert_changed"
    API_ERROR = "api_error"
    VM_NOT_FOUND = "vm_not_found"
    VM_START_TIMEOUT = "vm_start_timeout"
    VM_STOP_TIMEOUT = "vm_stop_timeout"
    # SSH mit dem Key „power“ (Konzept 5.6).
    SSH_KEY_MISSING = "ssh_key_missing"
    SSH_HOST_UNTRUSTED = "ssh_host_untrusted"
    SSH_HOST_KEY_MISMATCH = "ssh_host_key_mismatch"
    SSH_AUTH_FAILED = "ssh_auth_failed"
    SSH_UNREACHABLE = "ssh_unreachable"
    SSH_DENIED = "ssh_denied"
    SSH_FAILED = "ssh_failed"
    # Der Befehl lief auf Groß, ist dort aber gescheitert (z. B. virsh).
    REMOTE_FAILED = "remote_failed"
    SHUTDOWN_TIMEOUT = "shutdown_timeout"
    # Der Worker wurde beendet, während die Aktion lief (nur im Aktionslog).
    INTERRUPTED = "interrupted"
    INTERNAL_ERROR = "internal_error"


class Trigger(StrEnum):
    """Wer eine Aktion ausgelöst hat (Aktionslog, Konzept 5.3)."""

    DASHBOARD = "dashboard"
    # Teil einer anderen Aktion, z. B. das Wecken beim VM-Start.
    AUTOMATIC = "automatic"


class RunningAction(StrictModel):
    action: ActionName
    step: ActionStep
    started_at: datetime


class ActionResult(StrictModel):
    action: ActionName
    outcome: ActionOutcome
    finished_at: datetime


class ResourceActivity(StrictModel):
    """Laufende Aktion und letztes Ergebnis für Groß bzw. die VM."""

    running: RunningAction | None = None
    last: ActionResult | None = None


class ActionsSnapshot(StrictModel):
    """Teil von get_status: Was ist gerade möglich, was läuft, wie ging es aus?"""

    # None: verfügbar; sonst der Grund, warum nicht.
    availability: dict[ActionName, ActionBlocker | None]
    target: ResourceActivity
    vm: ResourceActivity
    # Ein Herunterfahren der VM läuft oder ist gescheitert: ab dann ist Erzwingen möglich.
    force_available_at: datetime | None = None
    # Bis wann „Erzwingen und danach Server herunterfahren“ angeboten wird (Konzept 4.2).
    forced_shutdown_until: datetime | None = None

    @classmethod
    def blocked(cls, blocker: ActionBlocker) -> ActionsSnapshot:
        """Alle Aktionen mit demselben Grund gesperrt, nichts läuft."""
        return cls(
            availability=dict.fromkeys(ActionName, blocker),
            target=ResourceActivity(),
            vm=ResourceActivity(),
        )
