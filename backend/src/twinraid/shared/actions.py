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
    # Ab v0.5.1 (Konzept 4.3, „Probelauf in TwinRaid“): Probelauf über den Wächter; der
    # echte Sync ist in v0.5.1 noch gesperrt (next_version).
    SYNC_DRY_RUN = "sync_dry_run"
    SYNC_START = "sync_start"


# Aktionen der Ressource „Sync“ (Konzept 4.2); die übrigen betreffen Groß und die VM.
SYNC_ACTIONS: frozenset[ActionName] = frozenset({ActionName.SYNC_DRY_RUN, ActionName.SYNC_START})


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
    # Auf Groß laufen außer der Gaming-VM weitere VMs (ab v0.4.2).
    OTHER_VMS_RUNNING = "other_vms_running"
    # Simulationsmodus: Der feste Rundlauf ist eingeschaltet (twinraid-admin mock-tour).
    TOUR = "tour"
    # Ab v0.5.1, Sync und Probelauf (Konzept 4.2 und 4.3): Wächter nicht aktiv oder sein
    # ZFS-Teil nicht bereit; sync.shares passt nicht zu den Datasets des Wächters; appdata
    # ohne eigenes Dataset von TwinRaid; der echte Sync kommt erst mit v0.5.2.
    SOURCE_GUARD_INACTIVE = "source_guard_inactive"
    LEVELS_MISMATCH = "levels_mismatch"
    SELF_DATASET_MISSING = "self_dataset_missing"
    NEXT_VERSION = "next_version"


class ActionStep(StrEnum):
    """Was eine laufende Aktion gerade tut (Zwischenzustände, Konzept 4.2)."""

    WAKING = "waking"
    # Groß ist erreichbar; es fehlt noch eine Antwort des VM-Dienstes über vm-watch.
    WAITING_FOR_VM_SERVICE = "waiting_for_vm_service"
    STARTING_VM = "starting_vm"
    STOPPING_VM = "stopping_vm"
    FORCING_VM = "forcing_vm"
    SHUTTING_DOWN = "shutting_down"


class ActionOutcome(StrEnum):
    """Ergebnis einer Aktion; dieselben Werte stehen im Aktionslog (Konzept 5.3)."""

    SUCCESS = "success"
    WOL_FAILED = "wol_failed"
    BOOT_TIMEOUT = "boot_timeout"
    # Meldungen von guard.sh zur VM, gleichnamig mit StatusReason (Konzept 3.5).
    VM_NOT_FOUND = "vm_not_found"
    VM_NOT_CONFIGURED = "vm_not_configured"
    VM_SERVICE_UNAVAILABLE = "vm_service_unavailable"
    VM_START_TIMEOUT = "vm_start_timeout"
    VM_STOP_TIMEOUT = "vm_stop_timeout"
    # SSH mit den Keys „status“ und „power“ (Konzept 5.6).
    SSH_KEY_MISSING = "ssh_key_missing"
    SSH_HOST_UNTRUSTED = "ssh_host_untrusted"
    SSH_HOST_KEY_MISMATCH = "ssh_host_key_mismatch"
    SSH_AUTH_FAILED = "ssh_auth_failed"
    SSH_UNREACHABLE = "ssh_unreachable"
    SSH_DENIED = "ssh_denied"
    SSH_FAILED = "ssh_failed"
    GUARD_OUTDATED = "guard_outdated"
    # Die Sitzung vm-watch lieferte gerade keinen Stand.
    WATCH_INTERRUPTED = "watch_interrupted"
    # Der Befehl lief auf Groß, ist dort aber gescheitert (z. B. virsh).
    REMOTE_FAILED = "remote_failed"
    SHUTDOWN_TIMEOUT = "shutdown_timeout"
    # guard.sh lehnte poweroff ab, weil die Gaming-VM lief (ab v0.4.3).
    VM_RUNNING = "vm_running"
    # Groß wurde nicht ausgeschaltet, weil weitere VMs liefen (ab v0.4.2).
    OTHER_VMS_RUNNING = "other_vms_running"
    # Ab v0.5.1 (guard.sh Version 3, Konzept 3.5): VM-Start abgelehnt, weil zu wenig
    # Arbeitsspeicher frei ist oder ein durchgereichtes Gerät belegt ist; andere Fehler von
    # virsh start; poweroff abgelehnt, weil ein Empfang läuft.
    VM_MEMORY_LOW = "vm_memory_low"
    VM_DEVICE_BUSY = "vm_device_busy"
    VM_START_FAILED = "vm_start_failed"
    SYNC_RUNNING = "sync_running"
    # Probelauf ab v0.5.1 (Konzept 4.3, „Probelauf in TwinRaid“, und 5.3). Bei
    # target_refused, target_check_failed und source_guard_refused nennt reason den Grund.
    DRY_RUN_SUCCESS = "dry_run_success"
    CANCELLED = "cancelled"
    SOURCE_GUARD_INACTIVE = "source_guard_inactive"
    SOURCE_GUARD_LOST = "source_guard_lost"
    SOURCE_GUARD_REFUSED = "source_guard_refused"
    TOO_SOON = "too_soon"
    SNAPSHOT_LOW_SPACE = "snapshot_low_space"
    SNAPSHOT_LIMIT = "snapshot_limit"
    PREPARE_FAILED = "prepare_failed"
    JOB_TIMEOUT = "job_timeout"
    TARGET_CHECK_FAILED = "target_check_failed"
    TARGET_REFUSED = "target_refused"
    BASE_MISSING = "base_missing"
    SELF_DATASET_MISSING = "self_dataset_missing"
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
