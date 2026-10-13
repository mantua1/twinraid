"""Schnittstelle für Statusabfragen (Konzept 3.4 und 4.1)."""

from typing import Protocol

from twinraid.shared.status import ResourceStatus


class StatusProvider(Protocol):
    async def get_status(self) -> ResourceStatus: ...
