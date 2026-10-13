"""Statusmodelle, die der Worker liefert und die Oberfläche anzeigt (Konzept 4.1)."""

from datetime import datetime
from enum import StrEnum

from twinraid.shared.base import StrictModel


class ResourceState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    STARTING = "starting"
    STOPPING = "stopping"
    UNKNOWN = "unknown"


class StatusSnapshot(StrictModel):
    """Momentaufnahme aller überwachten Ressourcen."""

    source: ResourceState
    target: ResourceState
    vm: ResourceState
    # Zeitpunkt der letzten Prüfung; None, solange noch nie geprüft wurde.
    checked_at: datetime | None
