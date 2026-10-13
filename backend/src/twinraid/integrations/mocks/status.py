"""Simulierter Status.

Phase 1: Alles ist „Unbekannt“. Die Simulation echter Zustandswechsel folgt in Phase 3.
"""

from twinraid.shared.status import ResourceState, ResourceStatus


class MockStatusProvider:
    async def get_status(self) -> ResourceStatus:
        return ResourceStatus(
            source=ResourceState.UNKNOWN,
            target=ResourceState.UNKNOWN,
            vm=ResourceState.UNKNOWN,
            checked_at=None,
        )
